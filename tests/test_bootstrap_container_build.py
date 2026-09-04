# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

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
    xodus, _revision = git_source_fixture(tmp_path / "xodus-fixture")
    runtime, _revision = git_source_fixture(tmp_path / "runtime-fixture")
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
            )
        )


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
