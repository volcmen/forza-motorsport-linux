# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Finalize the immutable bootstrap trust manifest from a verified release bundle."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .container_build import (
    ARCH_SNAPSHOT,
    BASE_IMAGE,
    BUNDLE_ARCHIVE,
    BUNDLE_ROOT,
    LLVM_MINGW_SHA256,
    SOURCE_DATE_EPOCH,
)
from .model import ArtifactSpec, BootstrapError, canonical_json

RELEASE = "v0.2.0"
RELEASE_URL = (
    "https://github.com/volcmen/forza-motorsport-linux/releases/download/"
    f"{RELEASE}/{BUNDLE_ARCHIVE}"
)
BUILDER_REPOSITORY = "ghcr.io/volcmen/forza-motorsport-builder"
XODUS_REPOSITORY = "https://github.com/volcmen/xodus"
XODUS_REVISION = "7b236772297b3475ea4f3cb830feb5b224f3064a"
XGAMERUNTIME_REPOSITORY = "https://github.com/volcmen/wine-forza-motorsport"
XGAMERUNTIME_REVISION = "a1548b1cf57371715d10b608bc81a77a188e40d4"
GE_URL = (
    "https://github.com/GloriousEggroll/proton-ge-custom/releases/download/"
    "GE-Proton11-3/GE-Proton11-3.tar.gz"
)
GE_SIZE = 532_524_366
GE_SHA256 = "861c2edc8d40d051fb1e7a692deb953be52bd339c46d90f2b7dde50ddad91266"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_BUILDER_IMAGE = re.compile(
    rf"{re.escape(BUILDER_REPOSITORY)}@sha256:[0-9a-f]{{64}}\Z"
)
_MAX_ARCHIVE_SIZE = 2 * 1024 * 1024 * 1024
_MAX_METADATA_SIZE = 1024 * 1024
_ARTIFACT_LAYOUT = (
    ("xodus-service", "bin/xodus-service", 0o755),
    ("xodus-cli", "bin/xodus-cli", 0o755),
    ("xodus-overlay", "bin/xodus-overlay", 0o755),
    ("xgameruntime-pe64", "runtime/xgameruntime.dll", 0o644),
    ("xgameruntime-unix64", "runtime/xgameruntime.so", 0o755),
)
_DIRECTORIES = {
    BUNDLE_ROOT,
    f"{BUNDLE_ROOT}/bin",
    f"{BUNDLE_ROOT}/runtime",
    f"{BUNDLE_ROOT}/licenses",
    f"{BUNDLE_ROOT}/provenance",
}
_METADATA = {
    "bundle-manifest.json",
    "licenses/xodus.txt",
    "licenses/xgameruntime.txt",
    "provenance/sources.json",
    "provenance/build-environment.json",
    "provenance/build-commands.txt",
    "SHA256SUMS",
}
_BUILD_COMMANDS = b"""cargo build --release --locked --offline -p xodus-service -p xodus-cli -p xodus-overlay
autoreconf -f
uv run --offline ./dlls/winevulkan/make_vulkan --xml /opt/vulkan-registry/vk.xml --video-xml /opt/vulkan-registry/video.xml
./tools/make_specfiles
./tools/make_requests
./configure --enable-win64 --without-ffmpeg --without-opencl
make include/hstring.h
make -C dlls/xgameruntime -j2
tar --sort=name --format=ustar --mtime=@0 --owner=0 --group=0 --numeric-owner
zstd --threads=1 -19
"""


@dataclass(frozen=True)
class ReleaseBundle:
    size: int
    sha256: str
    manifest_sha256: str
    artifacts: tuple[ArtifactSpec, ...]


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        stat.S_IFMT(info.st_mode),
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _open_archive(path: Path) -> tuple[int, os.stat_result]:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise BootstrapError("release bundle path must be absolute")
    try:
        fd = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(fd)
    except OSError:
        raise BootstrapError("release bundle is unsafe") from None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or not 0 < info.st_size <= _MAX_ARCHIVE_SIZE
    ):
        os.close(fd)
        raise BootstrapError("release bundle is unsafe")
    return fd, info


def _hash_fd(fd: int, size: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while block := os.pread(fd, 1024 * 1024, offset):
        offset += len(block)
        if offset > size:
            raise BootstrapError("release bundle changed while hashing")
        digest.update(block)
    if offset != size:
        raise BootstrapError("release bundle changed while hashing")
    return digest.hexdigest()


def _copy_decompressed(fd: int, destination: BinaryIO) -> None:
    stream = os.fdopen(os.dup(fd), "rb")
    try:
        os.lseek(stream.fileno(), 0, os.SEEK_SET)
        subprocess.run(
            ("zstd", "-q", "-d", "-c"),
            check=True,
            stdin=stream,
            stdout=destination,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, OSError):
        raise BootstrapError("release bundle decompression failed") from None
    finally:
        stream.close()


def _read_member(archive: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    if member.size > _MAX_METADATA_SIZE:
        raise BootstrapError("release bundle metadata is too large")
    stream = archive.extractfile(member)
    if stream is None:
        raise BootstrapError("release bundle metadata is unavailable")
    data = stream.read(_MAX_METADATA_SIZE + 1)
    if len(data) != member.size:
        raise BootstrapError("release bundle metadata is truncated")
    return data


def _json(data: bytes, label: str) -> object:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BootstrapError(f"{label} is invalid") from None
    if canonical_json(value) != data:
        raise BootstrapError(f"{label} is not canonical JSON")
    return value


def _artifact_records(value: object) -> tuple[ArtifactSpec, ...]:
    if not isinstance(value, list) or len(value) != len(_ARTIFACT_LAYOUT):
        raise BootstrapError("bundle artifact manifest is invalid")
    records: list[ArtifactSpec] = []
    for raw, expected in zip(value, _ARTIFACT_LAYOUT, strict=True):
        if not isinstance(raw, dict) or set(raw) != {
            "logical_name",
            "relative_path",
            "size",
            "sha256",
            "mode",
            "private",
        }:
            raise BootstrapError("bundle artifact manifest is invalid")
        logical_name, relative_path, mode = expected
        if (
            raw["logical_name"] != logical_name
            or raw["relative_path"] != relative_path
            or raw["mode"] != mode
            or raw["private"] is not False
            or type(raw["size"]) is not int
            or raw["size"] <= 0
            or not isinstance(raw["sha256"], str)
            or _SHA256.fullmatch(raw["sha256"]) is None
        ):
            raise BootstrapError("bundle artifact manifest is invalid")
        records.append(
            ArtifactSpec(
                logical_name,
                relative_path,
                raw["size"],
                raw["sha256"],
                mode,
                False,
            )
        )
    return tuple(records)


def _inspect_tar(raw_tar: Path, builder_image: str) -> tuple[str, tuple[ArtifactSpec, ...]]:
    expected_files = _METADATA | {relative for _, relative, _ in _ARTIFACT_LAYOUT}
    expected_names = _DIRECTORIES | {
        f"{BUNDLE_ROOT}/{relative}" for relative in expected_files
    }
    try:
        archive = tarfile.open(raw_tar, mode="r:")  # noqa: SIM115
    except (OSError, tarfile.TarError):
        raise BootstrapError("release bundle tar stream is invalid") from None
    with archive:
        members = archive.getmembers()
        names = [member.name.rstrip("/") for member in members]
        if len(names) != len(set(names)) or set(names) != expected_names:
            raise BootstrapError("release bundle member set is invalid")
        by_name = dict(zip(names, members, strict=True))
        for name in _DIRECTORIES:
            member = by_name[name]
            if not member.isdir() or member.mode != 0o755:
                raise BootstrapError("release bundle directory is invalid")
        file_data: dict[str, bytes] = {}
        file_digests: dict[str, str] = {}
        for relative in expected_files:
            member = by_name[f"{BUNDLE_ROOT}/{relative}"]
            if not member.isfile() or member.uid != 0 or member.gid != 0 or member.mtime != 0:
                raise BootstrapError("release bundle file metadata is invalid")
            if relative in _METADATA:
                data = _read_member(archive, member)
                if not data:
                    raise BootstrapError("release bundle metadata is empty")
                file_data[relative] = data
                file_digests[relative] = hashlib.sha256(data).hexdigest()
        manifest_data = file_data["bundle-manifest.json"]
        internal = _json(manifest_data, "bundle manifest")
        expected_sources = {
            "xgameruntime": XGAMERUNTIME_REVISION,
            "xodus": XODUS_REVISION,
        }
        if (
            not isinstance(internal, dict)
            or set(internal) != {"artifacts", "profile", "schema", "sources"}
            or internal["schema"] != 1
            or internal["profile"] != "legacy-v0.1"
            or internal["sources"] != expected_sources
        ):
            raise BootstrapError("bundle manifest is invalid")
        artifacts = _artifact_records(internal["artifacts"])
        for artifact in artifacts:
            member = by_name[f"{BUNDLE_ROOT}/{artifact.relative_path}"]
            stream = archive.extractfile(member)
            if stream is None:
                raise BootstrapError("bundle artifact is unavailable")
            digest = hashlib.sha256()
            size = 0
            while block := stream.read(1024 * 1024):
                size += len(block)
                if size > artifact.size:
                    raise BootstrapError("bundle artifact differs from its manifest")
                digest.update(block)
            if (
                size != artifact.size
                or digest.hexdigest() != artifact.sha256
                or member.mode != artifact.mode
            ):
                raise BootstrapError("bundle artifact differs from its manifest")
            file_digests[artifact.relative_path] = digest.hexdigest()
        expected_sources_json = canonical_json(expected_sources)
        if file_data["provenance/sources.json"] != expected_sources_json:
            raise BootstrapError("bundle source provenance is invalid")
        environment = _json(
            file_data["provenance/build-environment.json"],
            "bundle build environment",
        )
        if environment != {
            "arch_snapshot": ARCH_SNAPSHOT,
            "base_image": BASE_IMAGE,
            "builder_image": builder_image,
            "llvm_mingw_sha256": LLVM_MINGW_SHA256,
            "source_date_epoch": SOURCE_DATE_EPOCH,
        }:
            raise BootstrapError("bundle build environment is invalid")
        if file_data["provenance/build-commands.txt"] != _BUILD_COMMANDS:
            raise BootstrapError("bundle build commands are invalid")
        checksums = b"".join(
            f"{file_digests[path]}  {path}\n".encode("ascii")
            for path in sorted(expected_files - {"SHA256SUMS"})
        )
        if file_data["SHA256SUMS"] != checksums:
            raise BootstrapError("bundle checksum manifest is invalid")
        return hashlib.sha256(manifest_data).hexdigest(), artifacts


def inspect_release_bundle(
    bundle: Path, matching_bundle: Path, builder_image: str
) -> ReleaseBundle:
    """Verify two byte-identical production bundles and return pinned metadata."""
    if not isinstance(builder_image, str) or _BUILDER_IMAGE.fullmatch(builder_image) is None:
        raise BootstrapError("builder image must use the approved immutable GHCR digest")
    first_fd, first_info = _open_archive(bundle)
    second_fd: int | None = None
    try:
        second_fd, second_info = _open_archive(matching_bundle)
        first_digest = _hash_fd(first_fd, first_info.st_size)
        second_digest = _hash_fd(second_fd, second_info.st_size)
        if first_info.st_size != second_info.st_size or first_digest != second_digest:
            raise BootstrapError("clean release bundles differ")
        with tempfile.NamedTemporaryFile(prefix="forza-release-", suffix=".tar") as raw:
            _copy_decompressed(first_fd, raw)
            raw.flush()
            manifest_sha256, artifacts = _inspect_tar(Path(raw.name), builder_image)
        if _identity(os.fstat(first_fd)) != _identity(first_info):
            raise BootstrapError("release bundle changed during verification")
        return ReleaseBundle(
            first_info.st_size,
            first_digest,
            manifest_sha256,
            artifacts,
        )
    finally:
        if second_fd is not None:
            os.close(second_fd)
        os.close(first_fd)


def render_bootstrap_manifest(bundle: ReleaseBundle, builder_image: str) -> bytes:
    """Render the reviewed production values as deterministic TOML."""
    lines = [
        "# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors",
        "# SPDX-License-Identifier: GPL-3.0-or-later",
        "",
        "schema = 1",
        'app_id = "2440510"',
        'host = ["arch", "x86_64", "native-steam"]',
        'profile = "legacy-v0.1"',
        "",
        "[protonup]",
        'version = "0.1.5"',
        'project_relative = "tools/protonup"',
        "",
        "[ge]",
        'name = "GE-Proton11-3"',
        'release = "GE-Proton11-3"',
        f'url = "{GE_URL}"',
        'filename = "GE-Proton11-3.tar.gz"',
        f"size = {GE_SIZE}",
        f'sha256 = "{GE_SHA256}"',
        'expected_root = "GE-Proton11-3"',
        'allowed_redirect_hosts = ["github.com", "release-assets.githubusercontent.com"]',
        "",
        "[bundle]",
        'name = "forza-bootstrap-bundle"',
        f'release = "{RELEASE}"',
        f'url = "{RELEASE_URL}"',
        f'filename = "{BUNDLE_ARCHIVE}"',
        f"size = {bundle.size}",
        f'sha256 = "{bundle.sha256}"',
        f'expected_root = "{BUNDLE_ROOT}"',
        'allowed_redirect_hosts = ["github.com", "release-assets.githubusercontent.com"]',
        "",
        "[[sources]]",
        'name = "xodus"',
        f'repository = "{XODUS_REPOSITORY}"',
        f'revision = "{XODUS_REVISION}"',
        "",
        "[[sources]]",
        'name = "xgameruntime"',
        f'repository = "{XGAMERUNTIME_REPOSITORY}"',
        f'revision = "{XGAMERUNTIME_REVISION}"',
        "",
        "[builder]",
        f'image = "{builder_image}"',
        f'base_image = "{BASE_IMAGE}"',
        f'arch_snapshot = "{ARCH_SNAPSHOT}"',
        f"source_date_epoch = {SOURCE_DATE_EPOCH}",
    ]
    for artifact in bundle.artifacts:
        lines.extend(
            (
                "",
                "[[artifacts]]",
                f'logical_name = "{artifact.logical_name}"',
                f'relative_path = "{artifact.relative_path}"',
                f"size = {artifact.size}",
                f'sha256 = "{artifact.sha256}"',
                f"mode = {artifact.mode}",
                "private = false",
            )
        )
    return ("\n".join(lines) + "\n").encode("ascii")


def require_clean_repository(root: Path) -> None:
    try:
        result = subprocess.run(
            ("git", "status", "--porcelain=v1", "--untracked-files=all"),
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, OSError):
        raise BootstrapError("cannot verify repository cleanliness") from None
    if result.stdout:
        raise BootstrapError("repository must be clean before manifest finalization")


def atomic_write_manifest(path: Path, payload: bytes) -> None:
    destination = Path(path)
    if not destination.is_absolute():
        raise BootstrapError("manifest output path must be absolute")
    try:
        parent = destination.parent.resolve(strict=True)
        if parent != destination.parent or destination.is_symlink():
            raise BootstrapError("manifest output path is unsafe")
        fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=parent)
        try:
            os.fchmod(fd, 0o644)
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(temporary, destination)
            parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    except BootstrapError:
        raise
    except OSError:
        raise BootstrapError("cannot publish bootstrap manifest") from None
