# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

import forza_bootstrap.container_build as container_build_module
from forza_bootstrap.container_build import (
    ARCH_SNAPSHOT,
    BASE_IMAGE,
    LLVM_MINGW_SHA256,
    BuildInputs,
    DockerCapability,
    build_fixture_bundle,
    builder_build_argv,
    builder_manifest_argv,
    check_docker,
    dependency_fetch_argv,
    fetch_source,
    offline_build_argv,
    prepare_container_stage,
    publish_builder_image,
    publish_container_output,
    validate_offline_build_argv,
    validate_source,
    verify_fixture_archive,
    verify_output_bundle,
)
from forza_bootstrap.model import BootstrapError, SourceSpec

ROOT = Path(__file__).parents[1]
ROOTLESS_INFO = ROOT / "tests/fixtures/bootstrap/docker-info-rootless.txt"
BUNDLE_NAME = "forza-bootstrap-bundle-v1.tar.zst"
BUNDLE_TOOL = ROOT / "tools/build-bootstrap-bundle"
BUILDER_TOOL = ROOT / "tools/build-bootstrap-builder"


def test_builder_declares_native_xodus_build_dependencies() -> None:
    dockerfile = (ROOT / "containers/bootstrap-builder/Dockerfile").read_text()
    packages = dockerfile.split("pacman -Syu", 1)[1].split("&&", 1)[0].split()
    assert {"gtk4", "libadwaita", "protobuf", "sdl3", "webkit2gtk-4.1"} <= set(packages)


def run(*argv: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def git_source_fixture(root: Path) -> tuple[Path, str]:
    source = root / "source"
    source.mkdir(parents=True)
    run("git", "init", "-q", str(source))
    run("git", "config", "user.name", "Fixture", cwd=source)
    run("git", "config", "user.email", "fixture@example.invalid", cwd=source)
    (source / "tracked.txt").write_text("fixture\n", encoding="ascii")
    run("git", "add", "tracked.txt", cwd=source)
    run("git", "commit", "-q", "-m", "fixture", cwd=source)
    revision = run("git", "rev-parse", "HEAD", cwd=source).stdout.strip()
    run("git", "checkout", "-q", "--detach", revision, cwd=source)
    return source, revision


def build_inputs(tmp_path: Path, *, image: str | None = None) -> BuildInputs:
    tmp_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp_path.chmod(0o700)
    xodus, xodus_revision = git_source_fixture(tmp_path / "xodus-fixture")
    runtime, runtime_revision = git_source_fixture(tmp_path / "runtime-fixture")
    dependency_root = tmp_path / "dependencies"
    output_root = tmp_path / "output"
    dependency_root.mkdir()
    output_root.mkdir()
    return BuildInputs(
        xodus_source=xodus,
        xgameruntime_source=runtime,
        dependency_root=dependency_root,
        output_root=output_root,
        builder_image=(
            image
            if image is not None
            else "registry.example.invalid/builder@sha256:" + "a" * 64
        ),
        xodus_revision=xodus_revision,
        xgameruntime_revision=runtime_revision,
        build_root=tmp_path,
    )


class DockerRunner:
    def __init__(self, stdout: str = "", error: Exception | None = None) -> None:
        self.stdout = stdout
        self.error = error
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def __call__(self, argv: tuple[str, ...], **kwargs: object) -> object:
        self.calls.append((argv, kwargs))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(argv, 0, self.stdout, "")


class BuilderPublishRunner:
    def __init__(self, verified_image: str, registry_digest: str) -> None:
        self.verified_image = verified_image
        self.registry_digest = registry_digest
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: tuple[str, ...], **_kwargs: object) -> object:
        self.calls.append(argv)
        if argv[:2] in (("docker", "tag"), ("docker", "push")):
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[-2:] == ("{{.Id}}", "registry.example.invalid/forza:release"):
            return subprocess.CompletedProcess(argv, 0, self.verified_image + "\n", "")
        if argv[-2:] == ("{{json .RepoDigests}}", self.verified_image):
            payload = json.dumps(
                ["registry.example.invalid/forza@" + self.registry_digest]
            )
            return subprocess.CompletedProcess(argv, 0, payload + "\n", "")
        if argv[:3] == ("docker", "manifest", "inspect"):
            payload = json.dumps({"config": {"digest": self.verified_image}})
            return subprocess.CompletedProcess(argv, 0, payload + "\n", "")
        raise AssertionError(f"unexpected Docker call: {argv!r}")


def test_rootless_amd64_daemon_is_accepted() -> None:
    runner = DockerRunner(ROOTLESS_INFO.read_text(encoding="ascii"))

    capability = check_docker(runner)

    assert capability == DockerCapability("28.3.3", "x86_64", True)
    assert runner.calls == [
        (
            ("docker", "info", "--format", "{{json .}}"),
            {"check": True, "capture_output": True, "text": True},
        )
    ]


def test_container_writable_stage_is_narrow_and_privately_anchored(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)

    stage = prepare_container_stage(private, "dependencies")

    assert stage == private / "dependencies"
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert stat.S_IMODE(stage.stat().st_mode) == 0o733
    assert stage.stat().st_uid == os.getuid()


@pytest.mark.skipif(
    os.environ.get("FORZA_RUN_ROOTLESS_DOCKER_TEST") != "1",
    reason="requires explicit real rootless Docker opt-in",
)
def test_real_rootless_builder_uid_can_write_container_stage(tmp_path: Path) -> None:
    check_docker()
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    stage = prepare_container_stage(private, "dependencies")
    source = private / "xodus"
    source.mkdir()
    (source / "Cargo.lock").write_text("fixture\n", encoding="ascii")
    fake_bin = private / "fake-bin"
    fake_bin.mkdir()
    fake_cargo = fake_bin / "cargo"
    fake_cargo.write_text(
        "#!/bin/sh\n"
        'test "$1" = vendor\n'
        'mkdir -p "$4/nested" /deps/cargo-home/index\n'
        "printf 'crate\\n' > \"$4/nested/file\"\n"
        "printf 'cache\\n' > /deps/cargo-home/index/cache\n"
        "printf '[source.crates-io]\\n'\n",
        encoding="ascii",
    )
    fake_cargo.chmod(0o755)
    fetch_script = private / "fetch-xodus-deps"
    shutil.copyfile(
        ROOT / "containers/bootstrap-builder/fetch-xodus-deps.sh", fetch_script
    )
    fetch_script.chmod(0o755)

    subprocess.run(
        (
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user=1000",
            "--env=PATH=/fake-bin:/usr/bin",
            "--mount",
            f"type=bind,src={source},dst=/src/xodus,readonly",
            "--mount",
            f"type=bind,src={stage},dst=/deps",
            "--mount",
            f"type=bind,src={fake_bin},dst=/fake-bin,readonly",
            "--mount",
            f"type=bind,src={fetch_script},dst=/usr/local/bin/fetch-xodus-deps,readonly",
            BASE_IMAGE,
            "/usr/local/bin/fetch-xodus-deps",
        ),
        check=True,
    )

    assert (stage / "vendor/nested/file").read_text(encoding="ascii") == "crate\n"
    assert (stage / "cargo-home/index/cache").read_text(encoding="ascii") == "cache\n"
    shutil.rmtree(stage)
    assert not stage.exists()


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (
            {"ServerVersion": "28.3.3", "Architecture": "amd64", "SecurityOptions": []},
            "rootless Docker daemon",
        ),
        (
            {
                "ServerVersion": "28.3.3",
                "Architecture": "aarch64",
                "SecurityOptions": ["name=rootless"],
            },
            "x86_64",
        ),
        (
            {
                "ServerVersion": "",
                "Architecture": "amd64",
                "SecurityOptions": ["name=rootless"],
            },
            "invalid Docker capability",
        ),
    ),
)
def test_source_mode_rejects_rootful_wrong_arch_or_malformed_daemon(
    payload: dict[str, object], message: str
) -> None:
    runner = DockerRunner(json.dumps(payload))

    with pytest.raises(BootstrapError, match=message):
        check_docker(runner)


@pytest.mark.parametrize(
    "error",
    (
        FileNotFoundError("docker"),
        subprocess.CalledProcessError(1, ("docker", "info")),
    ),
)
def test_source_mode_normalizes_unreachable_docker(error: Exception) -> None:
    with pytest.raises(BootstrapError, match="Docker daemon is unavailable"):
        check_docker(DockerRunner(error=error))


def test_source_mode_rejects_dirty_or_wrong_revision(tmp_path: Path) -> None:
    source, revision = git_source_fixture(tmp_path)
    (source / "dirty").write_text("change", encoding="ascii")
    with pytest.raises(BootstrapError, match="source tree is not clean"):
        validate_source(source, revision)
    (source / "dirty").unlink()

    with pytest.raises(BootstrapError, match="source revision mismatch"):
        validate_source(source, "1" * 40)


def test_offline_build_rechecks_the_declared_source_revisions(tmp_path: Path) -> None:
    xodus, xodus_revision = git_source_fixture(tmp_path / "xodus")
    runtime, runtime_revision = git_source_fixture(tmp_path / "runtime")
    dependencies = tmp_path / "dependencies"
    output = tmp_path / "output"
    dependencies.mkdir()
    output.mkdir()
    inputs = BuildInputs(
        xodus_source=xodus,
        xgameruntime_source=runtime,
        dependency_root=dependencies,
        output_root=output,
        builder_image="registry.example.invalid/builder@sha256:" + "a" * 64,
        xodus_revision=xodus_revision,
        xgameruntime_revision=runtime_revision,
        build_root=tmp_path,
    )
    run("git", "checkout", "-q", "--detach", "HEAD^{}", cwd=xodus)
    (xodus / "second.txt").write_text("second\n", encoding="ascii")
    run("git", "add", "second.txt", cwd=xodus)
    run("git", "commit", "-q", "-m", "second", cwd=xodus)

    with pytest.raises(BootstrapError, match="source revision mismatch"):
        offline_build_argv(inputs, output)


def test_source_validation_requires_detached_regular_checkout(tmp_path: Path) -> None:
    source, revision = git_source_fixture(tmp_path)
    run("git", "switch", "-q", "-c", "mutable", cwd=source)

    with pytest.raises(BootstrapError, match="detached HEAD"):
        validate_source(source, revision)

    outside = tmp_path / "outside"
    source.rename(outside)
    source.symlink_to(outside, target_is_directory=True)
    with pytest.raises(BootstrapError, match="source tree is unsafe"):
        validate_source(source, revision)


def test_fetch_source_checks_out_exact_clean_revision_and_reuses_only_exact_tree(
    tmp_path: Path,
) -> None:
    origin, revision = git_source_fixture(tmp_path / "origin-fixture")
    cache = tmp_path / "cache"
    spec = SourceSpec("component", origin.as_uri(), revision)

    fetched = fetch_source(spec, cache)

    assert fetched == cache / f"component-{revision}"
    assert validate_source(fetched, revision) == fetched
    assert fetch_source(spec, cache) == fetched

    run("git", "checkout", "-q", "--detach", "HEAD^{}", cwd=fetched)
    (fetched / "untracked").write_text("wrong", encoding="ascii")
    with pytest.raises(BootstrapError, match="source tree is not clean"):
        fetch_source(spec, cache)


def test_fetch_source_never_publishes_or_removes_a_swapped_stage(
    tmp_path: Path,
) -> None:
    origin, revision = git_source_fixture(tmp_path / "origin")
    cache = tmp_path / "cache"
    spec = SourceSpec("component", origin.as_uri(), revision)
    swapped: Path | None = None
    validated: Path | None = None

    def swap_after_validation(
        argv: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal swapped, validated
        check = bool(kwargs.pop("check"))
        result = subprocess.run(argv, check=check, **kwargs)
        if "status" in argv and swapped is None:
            stage = Path(argv[argv.index("-C") + 1])
            validated = cache / "validated-tree"
            stage.rename(validated)
            stage.mkdir()
            swapped = stage / "foreign"
            swapped.write_text("preserve\n", encoding="ascii")
        return result

    with pytest.raises(BootstrapError, match="source tree changed before publication"):
        fetch_source(spec, cache, swap_after_validation)

    assert swapped is not None and swapped.read_text(encoding="ascii") == "preserve\n"
    assert validated is not None and (validated / "tracked.txt").is_file()
    assert not (cache / f"component-{revision}").exists()


@pytest.mark.parametrize(
    "image",
    (
        "registry.example.invalid/builder:latest",
        "registry.example.invalid/builder:v1",
        "registry.example.invalid/builder@sha256:ABCDEF",
        "sha256:short",
        "",
    ),
)
def test_builder_image_without_complete_lowercase_digest_is_rejected(
    tmp_path: Path, image: str
) -> None:
    inputs = build_inputs(tmp_path, image=image)

    with pytest.raises(BootstrapError, match="immutable digest"):
        dependency_fetch_argv(inputs)


def test_dependency_fetch_is_only_networked_stage_and_mounts_source_and_cache(
    tmp_path: Path,
) -> None:
    inputs = build_inputs(tmp_path)

    argv = dependency_fetch_argv(inputs)
    joined = "\0".join(argv)

    assert argv[:3] == ("docker", "run", "--rm")
    assert "--network=bridge" in argv
    assert "--read-only" in argv
    assert "--cap-drop=ALL" in argv
    assert "no-new-privileges" in joined
    assert f"type=bind,src={inputs.xodus_source},dst=/src/xodus,readonly" in argv
    assert f"type=bind,src={inputs.dependency_root},dst=/deps" in argv
    assert str(inputs.xgameruntime_source) not in joined
    assert str(inputs.output_root) not in joined
    assert "/home" not in joined
    assert "Steam" not in joined
    assert argv[-2:] == (inputs.builder_image, "/usr/local/bin/fetch-xodus-deps")


def test_offline_build_has_only_declared_mounts_and_no_network(tmp_path: Path) -> None:
    inputs = build_inputs(tmp_path)

    argv = offline_build_argv(inputs, inputs.output_root)
    joined = "\0".join(argv)

    assert "--network=none" in argv
    assert "--read-only" in argv
    assert "--cap-drop=ALL" in argv
    assert "no-new-privileges" in joined
    assert "--user=builder" in argv
    assert "--tmpfs=/tmp:rw,exec,nosuid,nodev,mode=1777" in argv
    assert "SOURCE_DATE_EPOCH=1756684800" in joined
    assert "LC_ALL=C.UTF-8" in joined
    assert "TZ=UTC" in joined
    mounts = [argv[index + 1] for index, value in enumerate(argv) if value == "--mount"]
    assert mounts == [
        f"type=bind,src={inputs.xodus_source},dst=/src/xodus,readonly",
        f"type=bind,src={inputs.xgameruntime_source},dst=/src/xgameruntime,readonly",
        f"type=bind,src={inputs.dependency_root},dst=/deps,readonly",
        f"type=bind,src={inputs.output_root},dst=/output",
    ]
    assert str(Path.home()) not in joined
    assert "Steam" not in joined
    assert "xodus.sock" not in joined
    assert "xgameruntime.dll.threading" not in joined
    assert argv[-2:] == (inputs.builder_image, "/usr/local/bin/build-bundle")


def test_output_must_be_exact_empty_declared_directory(tmp_path: Path) -> None:
    inputs = build_inputs(tmp_path)
    (inputs.output_root / BUNDLE_NAME).write_bytes(b"old")

    with pytest.raises(BootstrapError, match="output directory is not empty"):
        offline_build_argv(inputs, inputs.output_root)
    with pytest.raises(BootstrapError, match="declared output root"):
        offline_build_argv(inputs, tmp_path / "other")


def test_offline_invocation_rejects_network_or_unknown_mount_injection(
    tmp_path: Path,
) -> None:
    inputs = build_inputs(tmp_path)
    expected = offline_build_argv(inputs, inputs.output_root)
    networked = tuple(
        "--network=bridge" if value == "--network=none" else value for value in expected
    )
    extra_mount = (
        *expected[:-2],
        "--mount",
        "type=bind,src=/home,dst=/host,readonly",
        *expected[-2:],
    )

    with pytest.raises(BootstrapError, match="offline Docker invocation mismatch"):
        validate_offline_build_argv(networked, inputs, inputs.output_root)
    with pytest.raises(BootstrapError, match="offline Docker invocation mismatch"):
        validate_offline_build_argv(extra_mount, inputs, inputs.output_root)
    assert validate_offline_build_argv(expected, inputs, inputs.output_root) == expected


def test_fixture_invocation_allows_only_the_exact_fixture_suffix(
    tmp_path: Path,
) -> None:
    inputs = build_inputs(tmp_path)
    base = offline_build_argv(inputs, inputs.output_root)
    fixture = (*base, "--fixture")

    assert (
        validate_offline_build_argv(fixture, inputs, inputs.output_root, fixture=True)
        == fixture
    )
    with pytest.raises(BootstrapError, match="offline Docker invocation mismatch"):
        validate_offline_build_argv(
            (*fixture, "--unexpected"), inputs, inputs.output_root, fixture=True
        )


def test_mount_input_rejects_symlinks_and_overlapping_roots(tmp_path: Path) -> None:
    inputs = build_inputs(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    inputs.dependency_root.rmdir()
    inputs.dependency_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(BootstrapError, match="mount root is unsafe"):
        dependency_fetch_argv(inputs)

    clean = build_inputs(tmp_path / "overlap")
    nested_output = clean.dependency_root / "output"
    nested_output.mkdir()
    overlapping = BuildInputs(
        clean.xodus_source,
        clean.xgameruntime_source,
        clean.dependency_root,
        nested_output,
        clean.builder_image,
        clean.xodus_revision,
        clean.xgameruntime_revision,
        clean.build_root,
    )
    with pytest.raises(BootstrapError, match="mount roots overlap"):
        offline_build_argv(overlapping, nested_output)


def test_mount_input_rejects_home_and_docker_mount_delimiters(tmp_path: Path) -> None:
    inputs = build_inputs(tmp_path)
    with pytest.raises(BootstrapError, match="forbidden host root"):
        dependency_fetch_argv(
            BuildInputs(
                Path.home(),
                inputs.xgameruntime_source,
                inputs.dependency_root,
                inputs.output_root,
                inputs.builder_image,
                inputs.xodus_revision,
                inputs.xgameruntime_revision,
                inputs.build_root,
            )
        )

    with pytest.raises(BootstrapError, match="forbidden host root"):
        dependency_fetch_argv(
            BuildInputs(
                inputs.xodus_source,
                inputs.xgameruntime_source,
                Path.home() / "Development",
                inputs.output_root,
                inputs.builder_image,
                inputs.xodus_revision,
                inputs.xgameruntime_revision,
                inputs.build_root,
            )
        )

    comma_source, _revision = git_source_fixture(tmp_path / "comma,source")
    with pytest.raises(BootstrapError, match="Docker mount delimiter"):
        dependency_fetch_argv(
            BuildInputs(
                comma_source,
                inputs.xgameruntime_source,
                inputs.dependency_root,
                inputs.output_root,
                inputs.builder_image,
                inputs.xodus_revision,
                inputs.xgameruntime_revision,
                inputs.build_root,
            )
        )


def test_argv_constructors_reject_roots_outside_declared_build_root(
    tmp_path: Path,
) -> None:
    inputs = build_inputs(tmp_path / "inputs")
    declared = tmp_path / "declared"
    declared.mkdir(mode=0o700)
    outside = BuildInputs(
        inputs.xodus_source,
        inputs.xgameruntime_source,
        inputs.dependency_root,
        inputs.output_root,
        inputs.builder_image,
        inputs.xodus_revision,
        inputs.xgameruntime_revision,
        declared,
    )

    with pytest.raises(BootstrapError, match="declared build root"):
        dependency_fetch_argv(outside)
    with pytest.raises(BootstrapError, match="declared build root"):
        offline_build_argv(outside, outside.output_root)


def test_builder_driver_binds_base_snapshot_and_llvm_digest() -> None:
    argv = builder_build_argv("forza-builder:test")
    joined = "\0".join(argv)

    assert f"BASE_IMAGE={BASE_IMAGE}" in joined
    assert f"ARCH_SNAPSHOT={ARCH_SNAPSHOT}" in joined
    assert f"LLVM_MINGW_SHA256={LLVM_MINGW_SHA256}" in joined
    assert "--platform=linux/amd64" in argv
    assert argv[-1] == str(ROOT / "containers/bootstrap-builder")
    assert argv[argv.index("--file") + 1] == str(
        ROOT / "containers/bootstrap-builder/Dockerfile"
    )
    assert "tools" not in argv[-1]
    assert ".git" not in argv[-1]


def test_builder_driver_uses_options_supported_by_legacy_docker_builder() -> None:
    argv = builder_build_argv("forza-builder:test")

    assert "--provenance=false" not in argv


def test_builder_publication_uses_captured_image_id_not_a_mutable_tag() -> None:
    verified = "sha256:" + "b" * 64
    registry_digest = "sha256:" + "d" * 64
    runner = BuilderPublishRunner(verified, registry_digest)

    published = publish_builder_image(
        verified,
        "registry.example.invalid/forza:release",
        runner,
    )

    assert published == "registry.example.invalid/forza@" + registry_digest
    assert runner.calls == [
        (
            "docker",
            "tag",
            verified,
            "registry.example.invalid/forza:release",
        ),
        ("docker", "push", "registry.example.invalid/forza:release"),
        (
            "docker",
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            "registry.example.invalid/forza:release",
        ),
        (
            "docker",
            "image",
            "inspect",
            "--format",
            "{{json .RepoDigests}}",
            verified,
        ),
        (
            "docker",
            "manifest",
            "inspect",
            "registry.example.invalid/forza@" + registry_digest,
        ),
    ]


def test_builder_reproducibility_uses_clean_builds_and_sandboxed_manifest() -> None:
    clean = builder_build_argv("forza-builder:test", no_cache=True)
    manifest = builder_manifest_argv("sha256:" + "b" * 64)
    joined = "\0".join(manifest)

    assert "--no-cache" in clean
    assert "--network=none" in manifest
    assert "--read-only" in manifest
    assert "--cap-drop=ALL" in manifest
    assert "no-new-privileges" in joined
    assert manifest[-3:] == (
        "sha256:" + "b" * 64,
        "/usr/bin/cat",
        "/usr/local/share/forza-builder/rootfs-manifest.txt",
    )


def decompress_bundle(archive: Path, destination: Path) -> Path:
    raw = destination / "bundle.tar"
    with raw.open("wb") as stream:
        subprocess.run(["zstd", "-q", "-d", "-c", archive], check=True, stdout=stream)
    return raw


def test_two_fixture_builds_are_byte_identical_with_sorted_normalized_tar(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    first_archive = build_fixture_bundle(first)
    second_archive = build_fixture_bundle(second)

    assert first_archive.read_bytes() == second_archive.read_bytes()
    raw = decompress_bundle(first_archive, tmp_path)
    with tarfile.open(raw, "r:") as archive:
        members = archive.getmembers()
    assert [member.name for member in members] == sorted(
        member.name for member in members
    )
    assert {member.mtime for member in members} == {0}
    assert {member.uid for member in members} == {0}
    assert {member.gid for member in members} == {0}
    assert {member.uname for member in members} == {""}
    assert {member.gname for member in members} == {""}
    assert all(member.isfile() or member.isdir() for member in members)


def test_fixture_bundle_contains_only_declared_open_source_layout(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    archive = build_fixture_bundle(output)
    raw = decompress_bundle(archive, tmp_path)

    with tarfile.open(raw, "r:") as bundle:
        names = {member.name for member in bundle.getmembers() if member.isfile()}
        internal = json.loads(
            bundle.extractfile("forza-bootstrap-bundle-v1/bundle-manifest.json").read()
        )

    assert names == {
        "forza-bootstrap-bundle-v1/SHA256SUMS",
        "forza-bootstrap-bundle-v1/bin/xodus-cli",
        "forza-bootstrap-bundle-v1/bin/xodus-overlay",
        "forza-bootstrap-bundle-v1/bin/xodus-service",
        "forza-bootstrap-bundle-v1/bundle-manifest.json",
        "forza-bootstrap-bundle-v1/licenses/xgameruntime.txt",
        "forza-bootstrap-bundle-v1/licenses/xodus.txt",
        "forza-bootstrap-bundle-v1/provenance/build-commands.txt",
        "forza-bootstrap-bundle-v1/provenance/build-environment.json",
        "forza-bootstrap-bundle-v1/provenance/sources.json",
        "forza-bootstrap-bundle-v1/runtime/xgameruntime.dll",
        "forza-bootstrap-bundle-v1/runtime/xgameruntime.so",
    }
    assert internal["schema"] == 1
    assert [item["logical_name"] for item in internal["artifacts"]] == [
        "xodus-service",
        "xodus-cli",
        "xodus-overlay",
        "xgameruntime-pe64",
        "xgameruntime-unix64",
    ]
    assert all(item["private"] is False for item in internal["artifacts"])


def test_wrong_output_hash_is_reported_without_replacing_or_adopting_it(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    archive = build_fixture_bundle(output)
    before = archive.read_bytes()

    with pytest.raises(BootstrapError, match="bundle output SHA-256 mismatch"):
        verify_output_bundle(archive, "0" * 64)

    assert archive.read_bytes() == before
    assert verify_output_bundle(archive, hashlib.sha256(before).hexdigest()) == archive


def test_fixture_producer_refuses_preexisting_archive(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    archive = output / BUNDLE_NAME
    archive.write_bytes(b"preserve")

    with pytest.raises(BootstrapError, match="bundle output already exists"):
        build_fixture_bundle(output)

    assert archive.read_bytes() == b"preserve"


def test_fixture_bundle_passes_standard_bundle_verifier(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    archive = build_fixture_bundle(output)

    assert verify_fixture_archive(archive) == archive


def test_container_output_is_copied_to_new_owner_held_archive(tmp_path: Path) -> None:
    private = tmp_path / "private"
    output = tmp_path / "output"
    private.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    stage = private / "stage"
    stage.mkdir(mode=0o733)
    stage.chmod(0o733)
    source = stage / BUNDLE_NAME
    source.write_bytes(b"container bytes")
    source.chmod(0o644)

    published = publish_container_output(source, output)

    assert published == output / BUNDLE_NAME
    assert published.read_bytes() == b"container bytes"
    assert published.stat().st_uid == os.getuid()
    assert stat.S_IMODE(published.stat().st_mode) == 0o644
    assert source.read_bytes() == b"container bytes"


def test_container_output_publication_never_adopts_or_replaces(tmp_path: Path) -> None:
    private = tmp_path / "private"
    output = tmp_path / "output"
    private.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    stage = private / "stage"
    stage.mkdir(mode=0o733)
    stage.chmod(0o733)
    source = stage / BUNDLE_NAME
    source.write_bytes(b"container bytes")
    destination = output / BUNDLE_NAME
    destination.write_bytes(b"preserve")

    with pytest.raises(BootstrapError, match="bundle output already exists"):
        publish_container_output(source, output)

    assert destination.read_bytes() == b"preserve"

    destination.unlink()
    source.unlink()
    source.symlink_to(tmp_path / "missing")
    with pytest.raises(BootstrapError, match="container bundle output is unsafe"):
        publish_container_output(source, output)


def test_container_output_rejects_concurrent_path_replacement_without_adoption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private = tmp_path / "private"
    output = tmp_path / "output"
    private.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    stage = private / "stage"
    stage.mkdir(mode=0o733)
    stage.chmod(0o733)
    source = stage / BUNDLE_NAME
    source.write_bytes(b"container bytes")
    source.chmod(0o644)
    destination = output / BUNDLE_NAME
    held = output / "held-container-output"
    real_fsync = container_build_module.os.fsync
    replaced = False

    def replace_after_destination_sync(fd: int) -> None:
        nonlocal replaced
        real_fsync(fd)
        if not replaced and destination.exists() and stat.S_ISREG(os.fstat(fd).st_mode):
            destination.rename(held)
            destination.write_bytes(b"foreign current")
            replaced = True

    monkeypatch.setattr(
        container_build_module.os, "fsync", replace_after_destination_sync
    )

    with pytest.raises(BootstrapError, match="destination changed during publication"):
        publish_container_output(source, output)

    assert replaced is True
    assert destination.read_bytes() == b"foreign current"
    assert held.read_bytes() == b"container bytes"


def test_container_output_failure_cleanup_never_unlinks_foreign_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private = tmp_path / "private"
    output = tmp_path / "output"
    private.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    stage = private / "stage"
    stage.mkdir(mode=0o733)
    stage.chmod(0o733)
    source = stage / BUNDLE_NAME
    source.write_bytes(b"container bytes")
    source.chmod(0o644)
    destination = output / BUNDLE_NAME
    held = output / "held-partial-output"
    real_read = container_build_module.os.read
    replaced = False

    def mutate_source_and_replace_destination(fd: int, size: int) -> bytes:
        nonlocal replaced
        data = real_read(fd, size)
        if data and not replaced:
            destination.rename(held)
            destination.write_bytes(b"foreign current")
            source.write_bytes(b"changed source")
            replaced = True
        return data

    monkeypatch.setattr(
        container_build_module.os, "read", mutate_source_and_replace_destination
    )

    with pytest.raises(BootstrapError, match="container bundle output changed"):
        publish_container_output(source, output)

    assert replaced is True
    assert destination.read_bytes() == b"foreign current"
    assert held.read_bytes() == b"container bytes"


def test_fixture_archive_files_have_declared_modes(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    raw = decompress_bundle(build_fixture_bundle(output), tmp_path)
    with tarfile.open(raw, "r:") as archive:
        modes = {
            member.name: stat.S_IMODE(member.mode)
            for member in archive.getmembers()
            if member.isfile()
        }

    assert modes["forza-bootstrap-bundle-v1/bin/xodus-service"] == 0o755
    assert modes["forza-bootstrap-bundle-v1/runtime/xgameruntime.dll"] == 0o644
    assert all(mode in {0o644, 0o755} for mode in modes.values())


def test_bundle_driver_refuses_occupied_output_before_contacting_docker(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    marker = output / "preserve"
    marker.write_bytes(b"untouched")
    isolated_path = tmp_path / "bin"
    isolated_path.mkdir()
    (isolated_path / "python3").symlink_to(sys.executable)

    result = subprocess.run(
        (str(BUNDLE_TOOL), "--fixture", "--output", str(output)),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": str(isolated_path)},
    )

    assert result.returncode == 2
    assert "output directory is not empty" in result.stderr
    assert marker.read_bytes() == b"untouched"


def test_builder_driver_dry_run_prints_pinned_build_command() -> None:
    result = subprocess.run(
        (str(BUILDER_TOOL), "--dry-run", "--tag", "forza-builder:test"),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert f"BASE_IMAGE={BASE_IMAGE}" in result.stdout
    assert f"ARCH_SNAPSHOT={ARCH_SNAPSHOT}" in result.stdout
    assert f"LLVM_MINGW_SHA256={LLVM_MINGW_SHA256}" in result.stdout
    assert "--platform=linux/amd64" in result.stdout


def test_builder_cli_publishes_with_its_checked_runner_contract(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "python3").symlink_to(sys.executable)
    docker = fake_bin / "docker"
    image = "sha256:" + "b" * 64
    digest = "sha256:" + "d" * 64
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json\n"
        "import sys\n"
        f"image = {image!r}\n"
        f"digest = {digest!r}\n"
        "args = sys.argv[1:]\n"
        "if args[:1] == ['info']:\n"
        "    print(json.dumps({'ServerVersion': '29.7.2', "
        "'Architecture': 'amd64', 'SecurityOptions': ['name=rootless']}))\n"
        "elif args[:2] == ['image', 'inspect'] and args[-1] == "
        "'registry.example.invalid/forza:release':\n"
        "    print(image)\n"
        "elif args[:2] == ['image', 'inspect'] and args[-1] == image and "
        "'RepoDigests' in args[-2]:\n"
        "    print(json.dumps(['registry.example.invalid/forza@' + digest]))\n"
        "elif args[:2] == ['image', 'inspect']:\n"
        "    print(image)\n"
        "elif args[:2] == ['manifest', 'inspect']:\n"
        "    print(json.dumps({'config': {'digest': image}}))\n"
        "elif args[:1] == ['run']:\n"
        "    print('schema=1')\n",
        encoding="ascii",
    )
    docker.chmod(0o755)

    result = subprocess.run(
        (
            str(BUILDER_TOOL),
            "--tag",
            "forza-builder:mutable",
            "--push",
            "registry.example.invalid/forza:release",
        ),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": str(fake_bin)},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "registry.example.invalid/forza@" + digest
