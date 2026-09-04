# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import io
import os
import subprocess
import tarfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Self

import pytest

import forza_bootstrap.artifacts as artifacts_module
from forza_bootstrap.artifacts import (
    ArchivePolicy,
    download_once,
    extract_bundle,
    extract_ge,
    inspect_tar,
    verify_bundle_root,
    verify_cached_file,
)
from forza_bootstrap.model import (
    ArtifactSpec,
    BootstrapError,
    BootstrapManifest,
    BuilderSpec,
    DownloadSpec,
    ProtonUpSpec,
    SourceSpec,
    canonical_json,
    sha256_bytes,
)


@dataclass(frozen=True)
class TarEntry:
    name: str
    kind: str
    data: bytes = b""
    mode: int = 0o644
    link_target: str = ""


class FakeResponse(io.BytesIO):
    def __init__(
        self,
        payload: bytes,
        final_url: str = "https://downloads.example.invalid/artifact.bin",
        on_enter: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(payload)
        self.final_url = final_url
        self.on_enter = on_enter
        self.read_sizes: list[int] = []

    def geturl(self) -> str:
        return self.final_url

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return super().read(size)

    def __enter__(self) -> Self:
        if self.on_enter is not None:
            self.on_enter()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def private_directory(path: Path) -> Path:
    path.mkdir()
    path.chmod(0o700)
    return path


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def download_spec(payload: bytes, **updates: object) -> DownloadSpec:
    values = {
        "name": "fixture",
        "release": "v1",
        "url": "https://downloads.example.invalid/artifact.bin",
        "filename": "artifact.bin",
        "size": len(payload),
        "sha256": sha256_bytes(payload),
        "expected_root": "fixture-root",
        "allowed_redirect_hosts": ("downloads.example.invalid",),
    }
    values.update(updates)
    return DownloadSpec(**values)  # type: ignore[arg-type]


def write_private_cache(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    path.chmod(0o600)
    return path


def tar_bytes(entries: list[TarEntry]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for entry in entries:
            info = tarfile.TarInfo(entry.name)
            info.mode = entry.mode
            info.uid = 0
            info.gid = 0
            info.mtime = 0
            if entry.kind == "file":
                info.type = tarfile.REGTYPE
                info.size = len(entry.data)
                archive.addfile(info, io.BytesIO(entry.data))
            elif entry.kind == "directory":
                info.type = tarfile.DIRTYPE
                info.size = 0
                archive.addfile(info)
            elif entry.kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = entry.link_target
                archive.addfile(info)
            elif entry.kind == "hardlink":
                info.type = tarfile.LNKTYPE
                info.linkname = entry.link_target
                archive.addfile(info)
            elif entry.kind == "fifo":
                info.type = tarfile.FIFOTYPE
                archive.addfile(info)
            elif entry.kind == "device":
                info.type = tarfile.CHRTYPE
                info.devmajor = 1
                info.devminor = 3
                archive.addfile(info)
            else:
                raise AssertionError(f"unknown fixture kind: {entry.kind}")
    return output.getvalue()


def make_tar(
    tmp_path: Path,
    entries: list[TarEntry],
    *,
    suffix: str = ".tar",
    name: str = "fixture",
) -> Path:
    raw = tar_bytes(entries)
    path = tmp_path / f"{name}{suffix}"
    if suffix == ".tar.zst":
        compressed = subprocess.run(
            ["zstd", "-q", "-c", "-"],
            input=raw,
            stdout=subprocess.PIPE,
            check=True,
        ).stdout
        path.write_bytes(compressed)
    elif suffix == ".tar.gz":
        with tarfile.open(path, mode="w:gz", format=tarfile.USTAR_FORMAT) as archive:
            for entry in entries:
                info = tarfile.TarInfo(entry.name)
                info.mode = entry.mode
                info.uid = 0
                info.gid = 0
                info.mtime = 0
                if entry.kind == "file":
                    info.type = tarfile.REGTYPE
                    info.size = len(entry.data)
                    archive.addfile(info, io.BytesIO(entry.data))
                elif entry.kind == "directory":
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
                elif entry.kind == "symlink":
                    info.type = tarfile.SYMTYPE
                    info.linkname = entry.link_target
                    archive.addfile(info)
                elif entry.kind == "hardlink":
                    info.type = tarfile.LNKTYPE
                    info.linkname = entry.link_target
                    archive.addfile(info)
                elif entry.kind == "fifo":
                    info.type = tarfile.FIFOTYPE
                    archive.addfile(info)
                elif entry.kind == "device":
                    info.type = tarfile.CHRTYPE
                    info.devmajor = 1
                    info.devminor = 3
                    archive.addfile(info)
                else:
                    raise AssertionError(f"unknown fixture kind: {entry.kind}")
    else:
        path.write_bytes(raw)
    return path


def policy(
    root: str = "root",
    *,
    members: int = 32,
    total: int = 1024 * 1024,
    links: bool = False,
) -> ArchivePolicy:
    return ArchivePolicy(root, members, total, links)


def artifact(
    payload: bytes = b"tool",
    *,
    relative_path: str = "bin/tool",
    mode: int = 0o755,
    digest: str | None = None,
    size: int | None = None,
) -> ArtifactSpec:
    return ArtifactSpec(
        logical_name="tool",
        relative_path=relative_path,
        size=len(payload) if size is None else size,
        sha256=sha256_bytes(payload) if digest is None else digest,
        mode=mode,
        private=False,
    )


def internal_manifest_bytes(artifacts: tuple[ArtifactSpec, ...]) -> bytes:
    return canonical_json(
        {
            "artifacts": [
                {
                    "logical_name": item.logical_name,
                    "mode": item.mode,
                    "private": item.private,
                    "relative_path": item.relative_path,
                    "sha256": item.sha256,
                    "size": item.size,
                }
                for item in artifacts
            ],
            "profile": "fixture",
            "schema": 1,
            "sources": {"xodus": "1" * 40},
        }
    )


def bootstrap_manifest(
    archive: Path,
    artifacts: tuple[ArtifactSpec, ...],
    *,
    root: str = "forza-bootstrap-bundle-v1",
) -> BootstrapManifest:
    return BootstrapManifest(
        schema=1,
        app_id="2440510",
        host=("arch", "x86_64", "native-steam"),
        profile="fixture",
        protonup=ProtonUpSpec(version="0.1.5", project_relative="tools/protonup"),
        ge=download_spec(b"ge", expected_root="GE-Proton11-3"),
        bundle=DownloadSpec(
            name="fixture-bundle",
            release="v1",
            url="https://downloads.example.invalid/bundle.tar.zst",
            filename="bundle.tar.zst",
            size=archive.stat().st_size,
            sha256=sha256_file(archive),
            expected_root=root,
            allowed_redirect_hosts=("downloads.example.invalid",),
        ),
        sources=(
            SourceSpec(
                name="xodus",
                repository="https://example.invalid/xodus",
                revision="1" * 40,
            ),
        ),
        builder=BuilderSpec(
            image="example.invalid/builder@sha256:" + "2" * 64,
            base_image="example.invalid/base@sha256:" + "3" * 64,
            arch_snapshot="https://example.invalid/archive",
            source_date_epoch=0,
        ),
        artifacts=artifacts,
        path=Path("bootstrap.toml"),
        sha256="4" * 64,
    )


def bundle_archive(
    tmp_path: Path,
    *,
    declared_artifact: ArtifactSpec | None = None,
    actual_payload: bytes = b"tool",
    actual_mode: int = 0o755,
    manifest_payload: bytes | None = None,
    suffix: str = ".tar.zst",
) -> tuple[Path, BootstrapManifest, bytes]:
    declared = artifact() if declared_artifact is None else declared_artifact
    manifest_data = (
        internal_manifest_bytes((declared,))
        if manifest_payload is None
        else manifest_payload
    )
    root = "forza-bootstrap-bundle-v1"
    entries = [
        TarEntry(root, "directory", mode=0o755),
        TarEntry(f"{root}/bundle-manifest.json", "file", manifest_data, 0o644),
        TarEntry(f"{root}/bin", "directory", mode=0o755),
        TarEntry(f"{root}/bin/tool", "file", actual_payload, actual_mode),
    ]
    archive = make_tar(tmp_path, entries, suffix=suffix, name="bundle")
    return archive, bootstrap_manifest(archive, (declared,)), manifest_data


def test_verify_cached_file_returns_false_only_for_absence(tmp_path: Path) -> None:
    cache = private_directory(tmp_path / "cache")

    assert verify_cached_file(cache / "missing.bin", 0, sha256_bytes(b"")) is False


def test_verify_cached_file_accepts_exact_owner_only_regular(tmp_path: Path) -> None:
    cache = private_directory(tmp_path / "cache")
    payload = b"accepted"
    cached = write_private_cache(cache / "artifact.bin", payload)

    assert verify_cached_file(cached, len(payload), sha256_bytes(payload)) is True


@pytest.mark.parametrize(
    ("expected_size", "expected_digest", "message"),
    [
        (7, sha256_bytes(b"accepted"), "cached artifact size mismatch"),
        (8, "0" * 64, "cached artifact digest mismatch"),
    ],
)
def test_verify_cached_file_raises_for_every_mismatch(
    tmp_path: Path, expected_size: int, expected_digest: str, message: str
) -> None:
    cache = private_directory(tmp_path / "cache")
    cached = write_private_cache(cache / "artifact.bin", b"accepted")

    with pytest.raises(BootstrapError, match=message):
        verify_cached_file(cached, expected_size, expected_digest)


@pytest.mark.parametrize("hostile", ("symlink", "directory", "fifo", "wrong-mode"))
def test_verify_cached_file_rejects_hostile_cache_objects(
    tmp_path: Path, hostile: str
) -> None:
    cache = private_directory(tmp_path / "cache")
    cached = cache / "artifact.bin"
    if hostile == "symlink":
        target = write_private_cache(cache / "target", b"accepted")
        cached.symlink_to(target)
    elif hostile == "directory":
        cached.mkdir()
    elif hostile == "fifo":
        os.mkfifo(cached, 0o600)
    else:
        write_private_cache(cached, b"accepted").chmod(0o644)

    with pytest.raises(BootstrapError, match="cached artifact"):
        verify_cached_file(cached, 8, sha256_bytes(b"accepted"))


def test_download_once_streams_one_attempt_into_private_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    payload = b"downloaded"
    response = FakeResponse(payload)
    calls = 0

    def opener(url: str) -> FakeResponse:
        nonlocal calls
        calls += 1
        assert url == "https://downloads.example.invalid/artifact.bin"
        return response

    result = download_once(download_spec(payload), cache, opener)

    assert calls == 1
    assert result.read_bytes() == payload
    assert result.stat().st_mode & 0o777 == 0o600
    assert cache.stat().st_mode & 0o777 == 0o700
    assert response.closed
    assert response.read_sizes == [1024 * 1024, 1024 * 1024]
    assert [path.name for path in cache.iterdir()] == ["artifact.bin"]


def test_download_once_reuses_verified_cache_without_opening_network(
    tmp_path: Path,
) -> None:
    cache = private_directory(tmp_path / "cache")
    payload = b"accepted"
    cached = write_private_cache(cache / "artifact.bin", payload)

    def opener(_url: str) -> FakeResponse:
        raise AssertionError("verified cache must not open the network")

    assert download_once(download_spec(payload), cache, opener) == cached


@pytest.mark.parametrize(
    "final_url",
    (
        "http://downloads.example.invalid/artifact.bin",
        "https://outside.example.invalid/artifact.bin",
    ),
)
def test_download_once_rejects_redirect_outside_https_allowlist(
    tmp_path: Path, final_url: str
) -> None:
    response = FakeResponse(b"accepted", final_url)

    with pytest.raises(BootstrapError, match="redirect is outside the HTTPS allowlist"):
        download_once(
            download_spec(b"accepted"), tmp_path / "cache", lambda _url: response
        )

    assert response.closed
    assert not list((tmp_path / "cache").iterdir())


def test_download_once_never_retries_failed_opener(tmp_path: Path) -> None:
    attempts = 0

    def failing_opener(_url: str) -> FakeResponse:
        nonlocal attempts
        attempts += 1
        raise OSError("controlled opener failure")

    with pytest.raises(BootstrapError, match="download failed"):
        download_once(download_spec(b"accepted"), tmp_path / "cache", failing_opener)

    assert attempts == 1
    assert not list((tmp_path / "cache").iterdir())


def test_failed_download_never_replaces_verified_cache_created_concurrently(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    accepted = b"accepted"
    cached = cache / "artifact.bin"

    def publish_accepted_cache() -> None:
        write_private_cache(cached, accepted)

    response = FakeResponse(b"wrong!!!", on_enter=publish_accepted_cache)

    with pytest.raises(BootstrapError, match="download digest mismatch"):
        download_once(download_spec(accepted), cache, lambda _url: response)

    assert cached.read_bytes() == accepted
    assert cached.stat().st_mode & 0o777 == 0o600
    assert [path.name for path in cache.iterdir()] == ["artifact.bin"]


def test_download_once_rejects_oversize_stream_before_publication(
    tmp_path: Path,
) -> None:
    response = FakeResponse(b"accepted-plus")

    with pytest.raises(BootstrapError, match="download exceeds pinned size"):
        download_once(
            download_spec(b"accepted"), tmp_path / "cache", lambda _url: response
        )

    assert response.closed
    assert not list((tmp_path / "cache").iterdir())


def test_download_once_verifies_staged_bytes_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write_all = artifacts_module.write_all

    def corrupt_staged_write(fd: int, data: bytes) -> None:
        real_write_all(fd, b"x" * len(data))

    monkeypatch.setattr(artifacts_module, "write_all", corrupt_staged_write)

    with pytest.raises(BootstrapError, match="download staged file mismatch"):
        download_once(
            download_spec(b"accepted"),
            tmp_path / "cache",
            lambda _url: FakeResponse(b"accepted"),
        )

    assert not list((tmp_path / "cache").iterdir())


def test_download_once_refuses_hostile_existing_cache_without_network(
    tmp_path: Path,
) -> None:
    cache = private_directory(tmp_path / "cache")
    (cache / "artifact.bin").symlink_to(cache / "missing")
    attempts = 0

    def opener(_url: str) -> FakeResponse:
        nonlocal attempts
        attempts += 1
        return FakeResponse(b"accepted")

    with pytest.raises(BootstrapError, match="cached artifact"):
        download_once(download_spec(b"accepted"), cache, opener)

    assert attempts == 0


def test_download_once_normalizes_malformed_direct_allowlist(tmp_path: Path) -> None:
    spec = download_spec(b"accepted", allowed_redirect_hosts=("[broken",))

    with pytest.raises(BootstrapError, match="redirect allowlist is invalid"):
        download_once(spec, tmp_path / "cache", lambda _url: FakeResponse(b"accepted"))

    assert not (tmp_path / "cache").exists()


def test_inspect_tar_returns_complete_bounded_graph(tmp_path: Path) -> None:
    archive = make_tar(
        tmp_path,
        [
            TarEntry("root", "directory", mode=0o755),
            TarEntry("root/bin", "directory", mode=0o755),
            TarEntry("root/bin/tool", "file", b"payload", 0o755),
        ],
        suffix=".tar.gz",
    )

    graph = inspect_tar(archive, policy())

    assert graph.root == "root"
    assert graph.total_regular_size == 7
    assert [(item.path, item.kind, item.size, item.mode) for item in graph.members] == [
        ("root", "directory", 0, 0o755),
        ("root/bin", "directory", 0, 0o755),
        ("root/bin/tool", "regular", 7, 0o755),
    ]


def test_inspect_tar_normalizes_missing_archive_error(tmp_path: Path) -> None:
    with pytest.raises(BootstrapError, match="archive access failed"):
        inspect_tar(tmp_path / "missing.tar", policy())


@pytest.mark.parametrize(
    "member",
    ("/absolute", "../escape", "root/../../escape", "root//duplicate"),
)
def test_bundle_rejects_unsafe_member_names(tmp_path: Path, member: str) -> None:
    archive = make_tar(tmp_path, [TarEntry(member, "file", b"payload")])

    with pytest.raises(BootstrapError, match="unsafe archive member"):
        inspect_tar(archive, policy())


def test_archive_rejects_duplicate_normalized_members(tmp_path: Path) -> None:
    archive = make_tar(
        tmp_path,
        [
            TarEntry("root/item", "file", b"first"),
            TarEntry("root/./item", "file", b"second"),
        ],
    )

    with pytest.raises(BootstrapError, match="duplicate archive member"):
        inspect_tar(archive, policy())


@pytest.mark.parametrize("kind", ("symlink", "hardlink", "fifo", "device"))
def test_bundle_rejects_links_and_special_files(tmp_path: Path, kind: str) -> None:
    entry = TarEntry(
        "forza-bootstrap-bundle-v1/member",
        kind,
        link_target="forza-bootstrap-bundle-v1/target",
    )
    archive = make_tar(tmp_path, [entry], suffix=".tar.zst")
    manifest = bootstrap_manifest(archive, (artifact(),))

    with pytest.raises(BootstrapError, match="regular files and directories"):
        extract_bundle(archive, tmp_path / "out", manifest)

    assert not (tmp_path / "out").exists()


def test_archive_rejects_too_many_members_before_extraction(tmp_path: Path) -> None:
    archive = make_tar(
        tmp_path,
        [TarEntry("root/one", "file", b"1"), TarEntry("root/two", "file", b"2")],
    )

    with pytest.raises(BootstrapError, match="too many members"):
        inspect_tar(archive, policy(members=1))


def test_archive_rejects_expanded_size_over_policy_cap(tmp_path: Path) -> None:
    archive = make_tar(tmp_path, [TarEntry("root/large", "file", b"12345")])

    with pytest.raises(BootstrapError, match="expanded size limit"):
        inspect_tar(archive, policy(total=4))


def test_archive_rejects_declared_payload_on_non_regular_member(tmp_path: Path) -> None:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        info = tarfile.TarInfo("root/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "target"
        info.mode = 0o777
        info.size = 4
        archive.addfile(info, io.BytesIO(b"data"))
    path = tmp_path / "link-with-payload.tar"
    path.write_bytes(output.getvalue())

    with pytest.raises(
        BootstrapError, match="non-regular archive member declares data"
    ):
        inspect_tar(path, policy(links=True))


@pytest.mark.parametrize("suffix", (".tar", ".tar.zst"))
def test_archive_rejects_truncated_stream_and_closes_resources(
    tmp_path: Path,
    suffix: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = make_tar(
        tmp_path,
        [TarEntry("root/payload", "file", b"x" * 4096)],
        suffix=suffix,
    )
    data = archive.read_bytes()
    archive.write_bytes(data[: max(1, len(data) // 3)])
    before_fds = len(os.listdir("/proc/self/fd"))
    processes: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def tracked_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(artifacts_module.subprocess, "Popen", tracked_popen)

    with pytest.raises(BootstrapError, match="archive stream is invalid"):
        inspect_tar(archive, policy(total=8192))

    assert len(os.listdir("/proc/self/fd")) == before_fds
    assert all(process.poll() is not None for process in processes)
    assert all(process.stdout is None or process.stdout.closed for process in processes)


def test_zstd_decoder_uses_argv_without_shell_and_is_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = make_tar(
        tmp_path,
        [TarEntry("root/payload", "file", b"payload")],
        suffix=".tar.zst",
    )
    calls: list[tuple[object, dict[str, object], subprocess.Popen[bytes]]] = []
    real_popen = subprocess.Popen

    def tracked_popen(argv: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = real_popen(argv, **kwargs)
        calls.append((argv, kwargs, process))
        return process

    monkeypatch.setattr(artifacts_module.subprocess, "Popen", tracked_popen)

    inspect_tar(archive, policy())

    assert len(calls) == 1
    argv, kwargs, process = calls[0]
    assert argv == ["zstd", "-dc", "--", os.fspath(archive)]
    assert kwargs.get("shell", False) is False
    assert process.poll() == 0
    assert process.stdout is not None and process.stdout.closed


def test_extract_bundle_verifies_then_atomically_publishes(tmp_path: Path) -> None:
    archive, manifest, manifest_data = bundle_archive(tmp_path)
    destination = tmp_path / "published"

    verified = extract_bundle(archive, destination, manifest)

    assert verified.root == destination
    assert verified.manifest_sha256 == sha256_bytes(manifest_data)
    assert verified.artifacts == manifest.artifacts
    assert (destination / "bin/tool").read_bytes() == b"tool"
    assert (destination / "bin/tool").stat().st_mode & 0o777 == 0o755
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "bundle.tar.zst",
        "published",
    ]


def test_extract_bundle_rejects_wrong_archive_size_or_digest_as_declared(
    tmp_path: Path,
) -> None:
    archive, manifest, _manifest_data = bundle_archive(tmp_path)
    destination = tmp_path / "published"

    for wrong_bundle in (
        replace(manifest.bundle, size=manifest.bundle.size + 1),
        replace(manifest.bundle, sha256="0" * 64),
    ):
        with pytest.raises(BootstrapError, match="bundle archive .* mismatch"):
            extract_bundle(archive, destination, replace(manifest, bundle=wrong_bundle))
        assert not destination.exists()


def test_extract_bundle_normalizes_archive_disappearance(tmp_path: Path) -> None:
    archive, manifest, _manifest_data = bundle_archive(tmp_path)
    archive.unlink()

    with pytest.raises(BootstrapError, match="bundle extraction failed"):
        extract_bundle(archive, tmp_path / "published", manifest)


def test_extract_bundle_rejects_wrong_internal_manifest_hash(tmp_path: Path) -> None:
    archive, manifest, _manifest_data = bundle_archive(
        tmp_path,
        manifest_payload=canonical_json(
            {
                "artifacts": [],
                "profile": "wrong-profile",
                "schema": 1,
                "sources": {},
            }
        ),
    )

    with pytest.raises(BootstrapError, match="bundle manifest mismatch"):
        extract_bundle(archive, tmp_path / "published", manifest)

    assert not (tmp_path / "published").exists()


@pytest.mark.parametrize(
    ("declared", "actual_payload", "actual_mode"),
    (
        (artifact(mode=0o700), b"tool", 0o755),
        (artifact(size=5), b"tool", 0o755),
        (artifact(digest="0" * 64), b"tool", 0o755),
    ),
)
def test_extract_bundle_rejects_wrong_member_mode_size_or_hash(
    tmp_path: Path,
    declared: ArtifactSpec,
    actual_payload: bytes,
    actual_mode: int,
) -> None:
    archive, manifest, _manifest_data = bundle_archive(
        tmp_path,
        declared_artifact=declared,
        actual_payload=actual_payload,
        actual_mode=actual_mode,
    )

    with pytest.raises(BootstrapError, match="bundle artifact mismatch"):
        extract_bundle(archive, tmp_path / "published", manifest)

    assert not (tmp_path / "published").exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["bundle.tar.zst"]


def test_extract_bundle_preserves_preexisting_destination(tmp_path: Path) -> None:
    archive, manifest, _manifest_data = bundle_archive(tmp_path)
    destination = tmp_path / "published"
    destination.mkdir()
    marker = destination / "keep"
    marker.write_bytes(b"untouched")

    with pytest.raises(BootstrapError, match="destination already exists"):
        extract_bundle(archive, destination, manifest)

    assert marker.read_bytes() == b"untouched"


def test_verify_bundle_root_rejects_any_link_or_special_object(tmp_path: Path) -> None:
    archive, manifest, _manifest_data = bundle_archive(tmp_path, suffix=".tar")
    destination = tmp_path / "published"
    extract_bundle(archive, destination, manifest)
    (destination / "licenses").symlink_to(destination / "bin", target_is_directory=True)

    with pytest.raises(BootstrapError, match="regular files and directories"):
        verify_bundle_root(destination, manifest)


def test_verify_bundle_root_normalizes_missing_root(tmp_path: Path) -> None:
    _archive, manifest, _manifest_data = bundle_archive(tmp_path)

    with pytest.raises(BootstrapError, match="bundle verification failed"):
        verify_bundle_root(tmp_path / "missing", manifest)


def test_extract_bundle_revalidates_stage_at_publication_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, manifest, _manifest_data = bundle_archive(tmp_path)
    destination = tmp_path / "published"
    real_verify_bundle_root = artifacts_module.verify_bundle_root
    mutated = False

    def mutate_after_verification(
        root: Path, trust_manifest: BootstrapManifest
    ) -> artifacts_module.VerifiedBundle:
        nonlocal mutated
        verified = real_verify_bundle_root(root, trust_manifest)
        (root / "bin/tool").write_bytes(b"evil")
        mutated = True
        return verified

    monkeypatch.setattr(
        artifacts_module, "verify_bundle_root", mutate_after_verification
    )

    with pytest.raises(BootstrapError, match="extracted archive regular file mismatch"):
        extract_bundle(archive, destination, manifest)

    assert mutated is True
    assert not destination.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["bundle.tar.zst"]


def test_extract_bundle_closes_descriptors_when_stage_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, manifest, _manifest_data = bundle_archive(
        tmp_path,
        manifest_payload=canonical_json(
            {"artifacts": [], "profile": "wrong", "schema": 1, "sources": {}}
        ),
    )
    before_fds = len(os.listdir("/proc/self/fd"))

    def failed_cleanup(_directory_fd: int) -> None:
        raise OSError("controlled cleanup failure")

    monkeypatch.setattr(artifacts_module, "_remove_directory_contents", failed_cleanup)

    with pytest.raises(BootstrapError, match="bundle extraction failed"):
        extract_bundle(archive, tmp_path / "published", manifest)

    assert len(os.listdir("/proc/self/fd")) == before_fds


def test_extract_bundle_removes_reserved_stage_when_stage_open_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, manifest, _manifest_data = bundle_archive(tmp_path)
    real_open = os.open

    def fail_stage_open(
        path: str | bytes,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if (
            isinstance(path, str)
            and path.startswith(".published.")
            and path.endswith(".stage")
            and flags & os.O_DIRECTORY
        ):
            raise OSError("controlled staging open failure")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(artifacts_module.os, "open", fail_stage_open)

    with pytest.raises(BootstrapError, match="bundle extraction failed"):
        extract_bundle(archive, tmp_path / "published", manifest)

    assert sorted(path.name for path in tmp_path.iterdir()) == ["bundle.tar.zst"]


@pytest.mark.parametrize(
    ("kind", "link_target"),
    (
        ("symlink", "../bin/target"),
        ("hardlink", "GE-Proton11-3/bin/target"),
    ),
)
def test_extract_ge_accepts_declared_relative_contained_links(
    tmp_path: Path, kind: str, link_target: str
) -> None:
    root = "GE-Proton11-3"
    archive = make_tar(
        tmp_path,
        [
            TarEntry(root, "directory", mode=0o755),
            TarEntry(f"{root}/bin", "directory", mode=0o755),
            TarEntry(f"{root}/lib", "directory", mode=0o755),
            TarEntry(f"{root}/bin/target", "file", b"target", 0o755),
            TarEntry(f"{root}/lib/link", kind, link_target=link_target),
        ],
        suffix=".tar.gz",
    )
    destination = tmp_path / "ge"

    assert extract_ge(archive, destination, root) == destination
    assert (destination / "lib/link").read_bytes() == b"target"
    if kind == "symlink":
        assert (destination / "lib/link").is_symlink()
        assert os.readlink(destination / "lib/link") == "../bin/target"
    else:
        assert not (destination / "lib/link").is_symlink()
        assert (destination / "lib/link").stat().st_ino == (
            destination / "bin/target"
        ).stat().st_ino


@pytest.mark.parametrize(
    ("entries", "message"),
    (
        (
            [TarEntry("GE-Proton11-3/link", "symlink", link_target="/absolute")],
            "absolute archive link",
        ),
        (
            [TarEntry("GE-Proton11-3/link", "symlink", link_target="../../escape")],
            "escapes expected root",
        ),
        (
            [TarEntry("GE-Proton11-3/link", "symlink", link_target="missing")],
            "undeclared archive link target",
        ),
        (
            [
                TarEntry("GE-Proton11-3/a", "symlink", link_target="b"),
                TarEntry("GE-Proton11-3/b", "symlink", link_target="a"),
            ],
            "archive link cycle",
        ),
        (
            [
                TarEntry(
                    "GE-Proton11-3/link",
                    "hardlink",
                    link_target="GE-Proton11-3/missing",
                )
            ],
            "undeclared archive link target",
        ),
    ),
)
def test_extract_ge_rejects_unsafe_dangling_or_cyclic_links(
    tmp_path: Path, entries: list[TarEntry], message: str
) -> None:
    root = "GE-Proton11-3"
    archive = make_tar(
        tmp_path,
        [TarEntry(root, "directory", mode=0o755), *entries],
        suffix=".tar.gz",
    )

    with pytest.raises(BootstrapError, match=message):
        extract_ge(archive, tmp_path / "ge", root)

    assert not (tmp_path / "ge").exists()


def test_extract_ge_rejects_special_files(tmp_path: Path) -> None:
    archive = make_tar(
        tmp_path,
        [
            TarEntry("GE-Proton11-3", "directory", mode=0o755),
            TarEntry("GE-Proton11-3/device", "device"),
        ],
        suffix=".tar.gz",
    )

    with pytest.raises(BootstrapError, match="unsupported archive member type"):
        extract_ge(archive, tmp_path / "ge", "GE-Proton11-3")

    assert not (tmp_path / "ge").exists()


def test_extract_ge_normalizes_missing_archive(tmp_path: Path) -> None:
    with pytest.raises(BootstrapError, match="GE extraction failed"):
        extract_ge(tmp_path / "missing.tar.gz", tmp_path / "ge", "GE-Proton11-3")


def test_verify_bundle_tool_runs_the_real_verifier(tmp_path: Path) -> None:
    payload = b"cli-tool"
    declared = ArtifactSpec(
        logical_name="xodus-service",
        relative_path="bin/xodus-service",
        size=len(payload),
        sha256=sha256_bytes(payload),
        mode=0o755,
        private=False,
    )
    manifest_data = canonical_json(
        {
            "artifacts": [
                {
                    "logical_name": "xodus-service",
                    "mode": 0o755,
                    "private": False,
                    "relative_path": "bin/xodus-service",
                    "sha256": sha256_bytes(payload),
                    "size": len(payload),
                }
            ],
            "profile": "legacy-v0.1",
            "schema": 1,
            "sources": {
                "xgameruntime": "a1548b1cf57371715d10b608bc81a77a188e40d4",
                "xodus": "7b236772297b3475ea4f3cb830feb5b224f3064a",
            },
        }
    )
    root = "forza-bootstrap-bundle-v1"
    archive = make_tar(
        tmp_path,
        [
            TarEntry(root, "directory", mode=0o755),
            TarEntry(f"{root}/bundle-manifest.json", "file", manifest_data, 0o644),
            TarEntry(f"{root}/bin", "directory", mode=0o755),
            TarEntry(f"{root}/bin/xodus-service", "file", payload, 0o755),
        ],
        suffix=".tar.zst",
        name="cli-bundle",
    )
    fixture = (
        Path(__file__).parent / "fixtures/bootstrap/valid-bootstrap.toml"
    ).read_text(encoding="utf-8")
    manifest_text = fixture.replace("size = 1234", f"size = {archive.stat().st_size}")
    manifest_text = manifest_text.replace(
        'sha256 = "1111111111111111111111111111111111111111111111111111111111111111"',
        f'sha256 = "{sha256_file(archive)}"',
    )
    manifest_text = manifest_text.replace("size = 100", f"size = {declared.size}")
    manifest_text = manifest_text.replace(
        'sha256 = "2222222222222222222222222222222222222222222222222222222222222222"',
        f'sha256 = "{declared.sha256}"',
    )
    manifest = tmp_path / "bootstrap.toml"
    manifest.write_text(manifest_text, encoding="utf-8")

    completed = subprocess.run(
        [
            str(Path(__file__).parents[1] / "tools/verify-bootstrap-bundle"),
            "--manifest",
            str(manifest),
            str(archive),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert (
        completed.stdout
        == f"verified bundle manifest sha256 {sha256_bytes(manifest_data)}\n"
    )
