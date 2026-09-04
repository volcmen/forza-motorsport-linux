# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pinned artifact downloads and link-safe archive extraction."""

from __future__ import annotations

import errno
import gzip
import hashlib
import os
import posixpath
import re
import secrets
import stat
import subprocess
import tarfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal
from urllib.parse import urlsplit
from urllib.request import urlopen

from .model import (
    ArtifactSpec,
    BootstrapError,
    BootstrapManifest,
    DownloadSpec,
    SourceSpec,
    canonical_json,
)
from .safeio import (
    ensure_private_directory,
    open_owned_root,
    rename_noreplace,
    write_all,
)

_READ_SIZE = 1024 * 1024
_PRIVATE_FILE_MODE = 0o600
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_HOSTILE_OPEN_ERRNOS = frozenset(
    {errno.ELOOP, errno.ENOTDIR, errno.ENXIO, errno.EISDIR, errno.ENODEV}
)
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_BUNDLE_MAX_MEMBERS = 16_384
_BUNDLE_MAX_TOTAL_SIZE = 2 * 1024 * 1024 * 1024
_GE_MAX_MEMBERS = 262_144
_GE_MAX_TOTAL_SIZE = 8 * 1024 * 1024 * 1024
_TAR_BLOCK_SIZE = 512
_MAX_TAR_EXTENSION_SIZE = 64 * 1024
_MAX_TAR_EXTENSION_TOTAL = 16 * 1024 * 1024
_MAX_TAR_EXTENSION_DEPTH = 8
_MAX_TAR_PATH_BYTES = 4096
_TAR_EXTENSION_TYPES = frozenset({b"x", b"g", b"X", b"L", b"K"})
_BUNDLE_TOP_LEVEL = frozenset(
    {"bundle-manifest.json", "bin", "runtime", "licenses", "provenance", "SHA256SUMS"}
)
_BUNDLE_FIXED_METADATA = frozenset(
    {
        "bundle-manifest.json",
        "provenance/sources.json",
        "provenance/build-environment.json",
        "provenance/build-commands.txt",
        "SHA256SUMS",
    }
)


@dataclass(frozen=True)
class ArchiveMember:
    path: str
    kind: Literal["directory", "regular", "symlink", "hardlink"]
    size: int
    mode: int
    link_target: str | None


@dataclass(frozen=True)
class ArchiveGraph:
    root: str
    members: tuple[ArchiveMember, ...]
    total_regular_size: int


@dataclass(frozen=True)
class VerifiedBundle:
    root: Path
    manifest_sha256: str
    artifacts: tuple[ArtifactSpec, ...]


@dataclass(frozen=True)
class ArchivePolicy:
    expected_root: str
    max_members: int
    max_total_size: int
    allow_contained_links: bool


@dataclass(frozen=True)
class _ArchiveScan:
    graph: ArchiveGraph
    regular_sha256: dict[str, str]
    resolved_links: dict[str, str]


_FileIdentity = tuple[int, int, int, int, int, int, int]


@dataclass(frozen=True)
class _BoundArchive:
    path: Path
    fd: int
    identity: _FileIdentity


@dataclass(frozen=True)
class _BoundRegular:
    fd: int
    identity: _FileIdentity
    size: int
    sha256: str


@dataclass(frozen=True)
class _TreeBinding:
    root_identity: _FileIdentity
    member_identities: tuple[tuple[str, _FileIdentity], ...]


def _file_identity(info: os.stat_result) -> _FileIdentity:
    return (
        stat.S_IFMT(info.st_mode),
        info.st_dev,
        info.st_ino,
        info.st_size,
        stat.S_IMODE(info.st_mode),
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _pinned_size_digest(
    expected_size: object, expected_sha256: object
) -> tuple[int, str]:
    if type(expected_size) is not int or expected_size < 0:
        raise BootstrapError("artifact size is invalid")
    if (
        not isinstance(expected_sha256, str)
        or _SHA256.fullmatch(expected_sha256) is None
    ):
        raise BootstrapError("artifact digest is invalid")
    return expected_size, expected_sha256


def _absolute_leaf(path: str | os.PathLike[str], label: str) -> Path:
    try:
        value = Path(path)
    except (TypeError, ValueError):
        raise BootstrapError(f"{label} path is invalid") from None
    if (
        not value.is_absolute()
        or value.name in {"", ".", ".."}
        or value.parent == value
    ):
        raise BootstrapError(f"{label} path is invalid")
    return value


def _open_cached(parent_fd: int, name: str) -> int:
    try:
        return os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_fd,
        )
    except OSError as error:
        if error.errno in _HOSTILE_OPEN_ERRNOS:
            raise BootstrapError(
                "cached artifact is not a regular no-follow file"
            ) from None
        raise


def _validate_cached_info(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise BootstrapError("cached artifact is not a regular no-follow file")
    if info.st_uid != os.getuid():
        raise BootstrapError("cached artifact has unsafe ownership")
    if stat.S_IMODE(info.st_mode) != _PRIVATE_FILE_MODE:
        raise BootstrapError("cached artifact has unsafe mode")


def verify_cached_file(
    path: str | os.PathLike[str], expected_size: int, expected_sha256: str
) -> bool:
    """Verify an owner-only cached file; return false only when it is absent."""
    expected_size, expected_sha256 = _pinned_size_digest(expected_size, expected_sha256)
    source = _absolute_leaf(path, "cached artifact")
    try:
        parent_fd = open_owned_root(source.parent)
    except FileNotFoundError:
        return False
    fd: int | None = None
    verification_fd: int | None = None
    try:
        try:
            fd = _open_cached(parent_fd, source.name)
        except FileNotFoundError:
            return False
        before = os.fstat(fd)
        _validate_cached_info(before)
        if before.st_size != expected_size:
            raise BootstrapError("cached artifact size mismatch")
        digest = hashlib.sha256()
        while block := os.read(fd, _READ_SIZE):
            digest.update(block)
        after = os.fstat(fd)
        _validate_cached_info(after)
        after_identity = _file_identity(after)
        if _file_identity(before) != after_identity:
            raise BootstrapError("cached artifact changed while hashing")
        verification_fd = _open_cached(parent_fd, source.name)
        verification = os.fstat(verification_fd)
        _validate_cached_info(verification)
        if _file_identity(verification) != after_identity:
            raise BootstrapError("cached artifact changed while hashing")
        if digest.hexdigest() != expected_sha256:
            raise BootstrapError("cached artifact digest mismatch")
        return True
    finally:
        if verification_fd is not None:
            os.close(verification_fd)
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


def _validated_download(spec: DownloadSpec) -> DownloadSpec:
    if not isinstance(spec, DownloadSpec):
        raise BootstrapError("download specification is invalid")
    _pinned_size_digest(spec.size, spec.sha256)
    if (
        not isinstance(spec.filename, str)
        or spec.filename in {"", ".", ".."}
        or "/" in spec.filename
        or "\x00" in spec.filename
    ):
        raise BootstrapError("download filename is invalid")
    if (
        type(spec.allowed_redirect_hosts) is not tuple
        or not spec.allowed_redirect_hosts
    ):
        raise BootstrapError("download redirect allowlist is invalid")
    seen_hosts: set[str] = set()
    for host in spec.allowed_redirect_hosts:
        try:
            parsed_host = urlsplit(f"https://{host}")
            parsed_port = parsed_host.port
        except (TypeError, ValueError):
            raise BootstrapError("download redirect allowlist is invalid") from None
        if (
            not isinstance(host, str)
            or not host
            or host != host.lower()
            or parsed_host.hostname != host
            or parsed_port is not None
            or host in seen_hosts
        ):
            raise BootstrapError("download redirect allowlist is invalid")
        seen_hosts.add(host)
    try:
        parsed = urlsplit(spec.url)
        port = parsed.port
    except (TypeError, ValueError):
        raise BootstrapError("download URL is invalid") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname not in spec.allowed_redirect_hosts
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        raise BootstrapError("download URL is outside the HTTPS allowlist")
    return spec


def _validated_response_url(response: object, allowed_hosts: tuple[str, ...]) -> None:
    try:
        final_url = response.geturl()  # type: ignore[attr-defined]
        parsed = urlsplit(final_url)
        port = parsed.port
    except (AttributeError, TypeError, ValueError):
        raise BootstrapError(
            "download redirect is outside the HTTPS allowlist"
        ) from None
    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        raise BootstrapError("download redirect is outside the HTTPS allowlist")


def _verify_download_stage(
    parent_fd: int, name: str, expected_size: int, expected_sha256: str
) -> _BoundRegular:
    fd: int | None = None
    verification_fd: int | None = None
    retained = False
    try:
        fd = _open_cached(parent_fd, name)
        before = os.fstat(fd)
        _validate_cached_info(before)
        if before.st_size != expected_size:
            raise BootstrapError("download staged file mismatch")
        digest = hashlib.sha256()
        while block := os.read(fd, _READ_SIZE):
            digest.update(block)
        after = os.fstat(fd)
        _validate_cached_info(after)
        after_identity = _file_identity(after)
        if _file_identity(before) != after_identity:
            raise BootstrapError("download staged file mismatch")
        verification_fd = _open_cached(parent_fd, name)
        verification = os.fstat(verification_fd)
        _validate_cached_info(verification)
        if (
            _file_identity(verification) != after_identity
            or digest.hexdigest() != expected_sha256
        ):
            raise BootstrapError("download staged file mismatch")
        retained = True
        return _BoundRegular(fd, after_identity, expected_size, expected_sha256)
    except (FileNotFoundError, OSError):
        raise BootstrapError("download staged file mismatch") from None
    finally:
        if verification_fd is not None:
            os.close(verification_fd)
        if fd is not None and not retained:
            os.close(fd)


def _hash_bound_regular(binding: _BoundRegular) -> tuple[_FileIdentity, str]:
    before = os.fstat(binding.fd)
    digest = hashlib.sha256()
    offset = 0
    while block := os.pread(binding.fd, _READ_SIZE, offset):
        offset += len(block)
        if offset > binding.size:
            raise BootstrapError("download publication verification failed")
        digest.update(block)
    after = os.fstat(binding.fd)
    after_identity = _file_identity(after)
    if (
        not stat.S_ISREG(after.st_mode)
        or after.st_uid != os.getuid()
        or offset != binding.size
        or _file_identity(before) != after_identity
    ):
        raise BootstrapError("download publication verification failed")
    return after_identity, digest.hexdigest()


def _verify_published_download(
    parent_fd: int, name: str, binding: _BoundRegular
) -> None:
    published_fd: int | None = None
    try:
        held_identity, held_digest = _hash_bound_regular(binding)
        if held_identity[:6] != binding.identity[:6] or held_digest != binding.sha256:
            raise BootstrapError("download publication verification failed")
        published_fd = _open_cached(parent_fd, name)
        published = os.fstat(published_fd)
        if _file_identity(published) != held_identity:
            raise BootstrapError("download publication verification failed")
        published_binding = _BoundRegular(
            published_fd, held_identity, binding.size, binding.sha256
        )
        published_identity, published_digest = _hash_bound_regular(published_binding)
        if published_identity != held_identity or published_digest != binding.sha256:
            raise BootstrapError("download publication verification failed")
    except (FileNotFoundError, OSError):
        raise BootstrapError("download publication verification failed") from None
    finally:
        if published_fd is not None:
            os.close(published_fd)


def _unlink_if_bound_file(parent_fd: int, name: str, binding: _BoundRegular) -> None:
    candidate_fd: int | None = None
    try:
        candidate_fd = _open_cached(parent_fd, name)
        candidate = os.fstat(candidate_fd)
        held = os.fstat(binding.fd)
        if (candidate.st_dev, candidate.st_ino) != (held.st_dev, held.st_ino):
            return
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) == (held.st_dev, held.st_ino):
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
    except (FileNotFoundError, BootstrapError):
        return
    finally:
        if candidate_fd is not None:
            os.close(candidate_fd)


def download_once(
    spec: DownloadSpec,
    cache_root: str | os.PathLike[str],
    opener: Callable[[str], object] = urlopen,
) -> Path:
    """Make one bounded download attempt and publish it without replacement."""
    spec = _validated_download(spec)
    try:
        cache = Path(cache_root)
    except (TypeError, ValueError):
        raise BootstrapError("cache path is invalid") from None
    if not cache.is_absolute():
        raise BootstrapError("cache path must be absolute")
    ensure_private_directory(cache)
    final_path = cache / spec.filename
    if verify_cached_file(final_path, spec.size, spec.sha256):
        return final_path

    parent_fd = open_owned_root(cache)
    temporary = f".{spec.filename}.{secrets.token_hex(12)}"
    fd: int | None = None
    binding: _BoundRegular | None = None
    renamed = False
    published = False
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            _PRIVATE_FILE_MODE,
            dir_fd=parent_fd,
        )
        os.fchmod(fd, _PRIVATE_FILE_MODE)
        created = os.fstat(fd)
        if (
            not stat.S_ISREG(created.st_mode)
            or created.st_uid != os.getuid()
            or stat.S_IMODE(created.st_mode) != _PRIVATE_FILE_MODE
        ):
            raise BootstrapError("download temporary file is unsafe")

        total = 0
        digest = hashlib.sha256()
        try:
            response_context = opener(spec.url)
            with response_context as response:  # type: ignore[attr-defined]
                _validated_response_url(response, spec.allowed_redirect_hosts)
                while block := response.read(_READ_SIZE):  # type: ignore[attr-defined]
                    if not isinstance(block, bytes):
                        raise BootstrapError("download returned invalid data")
                    total += len(block)
                    if total > spec.size:
                        raise BootstrapError("download exceeds pinned size")
                    digest.update(block)
                    write_all(fd, block)
            os.fsync(fd)
        except BootstrapError:
            raise
        except Exception as error:
            raise BootstrapError("download failed") from error

        if total != spec.size or digest.hexdigest() != spec.sha256:
            raise BootstrapError("download digest mismatch")
        os.close(fd)
        fd = None
        binding = _verify_download_stage(parent_fd, temporary, spec.size, spec.sha256)
        try:
            rename_noreplace(parent_fd, temporary, parent_fd, spec.filename)
        except FileExistsError:
            raise BootstrapError("download cache destination already exists") from None
        renamed = True
        _verify_published_download(parent_fd, spec.filename, binding)
        os.fsync(parent_fd)
        published = True
        return final_path
    finally:
        try:
            if fd is not None:
                os.close(fd)
        finally:
            try:
                if renamed and not published and binding is not None:
                    _unlink_if_bound_file(parent_fd, spec.filename, binding)
                if not published:
                    try:
                        os.unlink(temporary, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
            finally:
                try:
                    if binding is not None:
                        os.close(binding.fd)
                finally:
                    os.close(parent_fd)


def _safe_root(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\x00" in value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise BootstrapError("archive expected root is invalid")
    try:
        os.fsencode(value)
    except (TypeError, ValueError):
        raise BootstrapError("archive expected root is invalid") from None
    return value


def _validated_policy(policy: ArchivePolicy) -> ArchivePolicy:
    if not isinstance(policy, ArchivePolicy):
        raise BootstrapError("archive policy is invalid")
    _safe_root(policy.expected_root)
    if type(policy.max_members) is not int or policy.max_members <= 0:
        raise BootstrapError("archive member limit is invalid")
    if type(policy.max_total_size) is not int or policy.max_total_size < 0:
        raise BootstrapError("archive size limit is invalid")
    if type(policy.allow_contained_links) is not bool:
        raise BootstrapError("archive link policy is invalid")
    return policy


def _path_components(path: str | os.PathLike[str], label: str) -> tuple[str, ...]:
    try:
        value = os.fspath(path)
    except (TypeError, ValueError):
        raise BootstrapError(f"{label} path is invalid") from None
    if not isinstance(value, str) or not value.startswith("/") or value == "/":
        raise BootstrapError(f"{label} path is invalid")
    parts = tuple(value.split("/")[1:])
    if any(part in {"", ".", ".."} or "\x00" in part for part in parts):
        raise BootstrapError(f"{label} path is invalid")
    try:
        os.fsencode(value)
    except (TypeError, ValueError):
        raise BootstrapError(f"{label} path is invalid") from None
    return parts


def _open_directory_components(parts: tuple[str, ...], label: str) -> int:
    fd = os.open("/", _DIRECTORY_FLAGS)
    try:
        for part in parts:
            try:
                next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            except OSError as error:
                if error.errno in _HOSTILE_OPEN_ERRNOS:
                    raise BootstrapError(
                        f"{label} path contains a non-directory"
                    ) from None
                raise
            os.close(fd)
            fd = next_fd
        result = fd
        fd = -1
        return result
    finally:
        if fd >= 0:
            os.close(fd)


def _open_absolute_regular(
    path: str | os.PathLike[str], label: str
) -> tuple[int, int, str]:
    parts = _path_components(path, label)
    parent_fd = _open_directory_components(parts[:-1], label)
    try:
        try:
            fd = os.open(
                parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent_fd,
            )
        except OSError as error:
            if error.errno in _HOSTILE_OPEN_ERRNOS:
                raise BootstrapError(
                    f"{label} is not a regular no-follow file"
                ) from None
            raise
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise BootstrapError(f"{label} is not a regular no-follow file")
            if info.st_uid != os.getuid():
                raise BootstrapError(f"{label} has unsafe ownership")
        except Exception:
            os.close(fd)
            raise
        return fd, parent_fd, parts[-1]
    except Exception:
        os.close(parent_fd)
        raise


def _open_archive_source(path: str | os.PathLike[str], label: str) -> _BoundArchive:
    archive_path = Path(path)
    fd, parent_fd, _name = _open_absolute_regular(archive_path, label)
    os.close(parent_fd)
    try:
        info = os.fstat(fd)
        return _BoundArchive(archive_path, fd, _file_identity(info))
    except Exception:
        os.close(fd)
        raise


def _validate_archive_source(source: _BoundArchive) -> None:
    info = os.fstat(source.fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or _file_identity(info) != source.identity
    ):
        raise BootstrapError("archive changed while inspecting")


def _tar_number(field: bytes) -> int:
    if not field:
        raise BootstrapError("archive stream is invalid")
    if field[0] & 0x80:
        if field[0] & 0x40:
            raise BootstrapError("archive stream is invalid")
        encoded = bytes((field[0] & 0x7F,)) + field[1:]
        return int.from_bytes(encoded, "big")
    stripped = field.strip(b" \0")
    if not stripped:
        return 0
    if any(character not in b"01234567" for character in stripped):
        raise BootstrapError("archive stream is invalid")
    return int(stripped, 8)


def _valid_tar_checksum(header: bytes) -> bool:
    expected = _tar_number(header[148:156])
    unsigned = sum(header[:148]) + (8 * ord(" ")) + sum(header[156:])
    signed = sum(value if value < 128 else value - 256 for value in header[:148])
    signed += 8 * ord(" ")
    signed += sum(value if value < 128 else value - 256 for value in header[156:])
    return expected in {unsigned, signed}


def _validate_raw_tar_name(header: bytes, typeflag: bytes) -> None:
    name = header[0:100].split(b"\0", 1)[0]
    prefix = header[345:500].split(b"\0", 1)[0]
    raw = prefix + b"/" + name if prefix else name
    if typeflag == tarfile.DIRTYPE and raw.endswith(b"/"):
        raw = raw[:-1]
    parts = raw.split(b"/")
    if (
        not raw
        or raw.startswith(b"/")
        or any(part in {b"", b".", b".."} for part in parts)
        or any(value < 0x20 or value == 0x7F for value in raw)
    ):
        raise BootstrapError("unsafe archive member name")


class _TarGuard:
    """Validate bounded raw tar structure before tarfile consumes each block."""

    def __init__(self, source: BinaryIO, policy: ArchivePolicy) -> None:
        self._source = source
        self._maximum_bytes = (
            policy.max_total_size
            + (2 * policy.max_members * 1024)
            + _MAX_TAR_EXTENSION_TOTAL
            + 1024
        )
        self._maximum_extensions = policy.max_members
        self._consumed = 0
        self._position = 0
        self._header = bytearray()
        self._data_remaining = 0
        self._extension_payload_remaining = 0
        self._extension_kind: bytes | None = None
        self._extension_payload = bytearray()
        self._extension_total = 0
        self._extension_count = 0
        self._extension_depth = 0
        self._zero_blocks = 0
        self._terminated = False

    def tell(self) -> int:
        return self._position

    def read(self, size: int = -1) -> bytes:
        request = _READ_SIZE if size is None or size < 0 else min(size, _READ_SIZE)
        data = self._source.read(request)
        if not isinstance(data, bytes):
            raise BootstrapError("archive stream is invalid")
        self._consume(data)
        self._position += len(data)
        return data

    def finish(self) -> None:
        while data := self._source.read(_READ_SIZE):
            if not isinstance(data, bytes):
                raise BootstrapError("archive stream is invalid")
            self._consume(data)
            self._position += len(data)
        if (
            self._header
            or self._data_remaining
            or self._extension_kind is not None
            or not self._terminated
            or self._zero_blocks < 2
        ):
            raise BootstrapError("archive stream is invalid")

    def _consume(self, data: bytes) -> None:
        self._consumed += len(data)
        if self._consumed > self._maximum_bytes:
            raise BootstrapError("archive exceeds decompressed size limit")
        offset = 0
        while offset < len(data):
            if self._data_remaining:
                amount = min(self._data_remaining, len(data) - offset)
                if self._extension_kind is not None:
                    payload_amount = min(self._extension_payload_remaining, amount)
                    self._extension_payload.extend(
                        data[offset : offset + payload_amount]
                    )
                    self._extension_payload_remaining -= payload_amount
                self._data_remaining -= amount
                offset += amount
                if self._data_remaining == 0:
                    self._finish_extension()
                continue

            amount = min(_TAR_BLOCK_SIZE - len(self._header), len(data) - offset)
            self._header.extend(data[offset : offset + amount])
            offset += amount
            if len(self._header) == _TAR_BLOCK_SIZE:
                header = bytes(self._header)
                self._header.clear()
                self._consume_header(header)

    def _consume_header(self, header: bytes) -> None:
        if header == bytes(_TAR_BLOCK_SIZE):
            self._zero_blocks += 1
            if self._zero_blocks >= 2:
                self._terminated = True
            return
        if self._zero_blocks or self._terminated or not _valid_tar_checksum(header):
            raise BootstrapError("archive stream is invalid")
        typeflag = header[156:157] or b"0"
        if typeflag == tarfile.GNUTYPE_SPARSE:
            raise BootstrapError("sparse archive metadata is forbidden")
        if typeflag not in _TAR_EXTENSION_TYPES:
            _validate_raw_tar_name(header, typeflag)
        size = _tar_number(header[124:136])
        padded_size = (
            (size + _TAR_BLOCK_SIZE - 1) // _TAR_BLOCK_SIZE
        ) * _TAR_BLOCK_SIZE
        if typeflag in _TAR_EXTENSION_TYPES:
            self._extension_count += 1
            self._extension_depth += 1
            self._extension_total += size
            if (
                size > _MAX_TAR_EXTENSION_SIZE
                or self._extension_total > _MAX_TAR_EXTENSION_TOTAL
                or self._extension_count > self._maximum_extensions
            ):
                raise BootstrapError("archive metadata exceeds limit")
            if self._extension_depth > _MAX_TAR_EXTENSION_DEPTH:
                raise BootstrapError("archive metadata extension depth exceeds limit")
            self._extension_kind = typeflag
            self._extension_payload_remaining = size
            self._extension_payload.clear()
        else:
            self._extension_depth = 0
        self._data_remaining = padded_size
        if self._data_remaining == 0:
            self._finish_extension()

    def _finish_extension(self) -> None:
        if self._extension_kind in {b"x", b"g", b"X"}:
            payload = bytes(self._extension_payload)
            if b"GNU.sparse." in payload or b"SCHILY.realsize" in payload:
                raise BootstrapError("sparse archive metadata is forbidden")
        self._extension_kind = None
        self._extension_payload_remaining = 0
        self._extension_payload.clear()


@contextmanager
def _tar_stream(
    source: _BoundArchive, policy: ArchivePolicy
) -> Iterator[tarfile.TarFile]:
    process: subprocess.Popen[bytes] | None = None
    stream: BinaryIO | None = None
    compressed_stream: BinaryIO | None = None
    guard: _TarGuard | None = None
    archive: tarfile.TarFile | None = None
    decoder_fd: int | None = None
    failed = True
    try:
        _validate_archive_source(source)
        path_value = os.fspath(source.path)
        decoder_fd = os.dup(source.fd)
        os.lseek(decoder_fd, 0, os.SEEK_SET)
        if path_value.endswith(".tar.zst"):
            try:
                process = subprocess.Popen(
                    ["zstd", "-dc"],
                    stdin=decoder_fd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                )
            except OSError as error:
                raise BootstrapError("archive stream is invalid") from error
            os.close(decoder_fd)
            decoder_fd = None
            if process.stdout is None:
                raise BootstrapError("archive stream is invalid")
            stream = process.stdout
        elif path_value.endswith(".tar"):
            stream = os.fdopen(decoder_fd, "rb")
            decoder_fd = None
        elif path_value.endswith((".tar.gz", ".tgz")):
            compressed_stream = os.fdopen(decoder_fd, "rb")
            decoder_fd = None
            stream = gzip.GzipFile(fileobj=compressed_stream, mode="rb")
        else:
            raise BootstrapError("archive format is unsupported")
        guard = _TarGuard(stream, policy)
        try:
            archive = tarfile.open(  # noqa: SIM115 - closed in shared finally
                fileobj=guard, mode="r|"
            )
        except (OSError, tarfile.TarError) as error:
            raise BootstrapError("archive stream is invalid") from error
        yield archive
        guard.finish()
        _validate_archive_source(source)
        failed = False
    finally:
        try:
            if archive is not None:
                archive.close()
        finally:
            try:
                if stream is not None:
                    stream.close()
            finally:
                try:
                    if compressed_stream is not None:
                        compressed_stream.close()
                finally:
                    if process is not None:
                        return_code = process.wait()
                        if not failed and return_code != 0:
                            raise BootstrapError("archive stream is invalid")
                    if decoder_fd is not None:
                        os.close(decoder_fd)


def _normalized_member_name(raw: object, seen: set[str]) -> str:
    if not isinstance(raw, str) or not raw:
        raise BootstrapError("unsafe archive member name")
    normalized = posixpath.normpath(raw)
    if normalized in seen:
        raise BootstrapError("duplicate archive member")
    if (
        raw != normalized
        or raw.startswith("/")
        or normalized in {"", ".", ".."}
        or normalized.startswith("../")
        or "\x00" in raw
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw)
    ):
        raise BootstrapError("unsafe archive member name")
    try:
        encoded = os.fsencode(raw)
    except (TypeError, ValueError):
        raise BootstrapError("unsafe archive member name") from None
    if len(encoded) > _MAX_TAR_PATH_BYTES:
        raise BootstrapError("unsafe archive member name")
    seen.add(normalized)
    return normalized


def _member_kind(info: tarfile.TarInfo, policy: ArchivePolicy) -> str:
    if info.isdir():
        return "directory"
    if info.isreg():
        return "regular"
    if info.issym():
        if policy.allow_contained_links:
            return "symlink"
        raise BootstrapError(
            "bundle archive accepts regular files and directories only"
        )
    if info.islnk():
        if policy.allow_contained_links:
            return "hardlink"
        raise BootstrapError(
            "bundle archive accepts regular files and directories only"
        )
    if not policy.allow_contained_links:
        raise BootstrapError(
            "bundle archive accepts regular files and directories only"
        )
    raise BootstrapError("unsupported archive member type")


def _resolved_link(member: ArchiveMember, root: str) -> str:
    target = member.link_target
    if not isinstance(target, str) or not target or "\x00" in target:
        raise BootstrapError("archive link target is invalid")
    if target.startswith("/") or posixpath.isabs(target):
        raise BootstrapError("absolute archive link target is forbidden")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in target):
        raise BootstrapError("archive link target is invalid")
    try:
        if len(os.fsencode(target)) > _MAX_TAR_PATH_BYTES:
            raise BootstrapError("archive link target is invalid")
    except (TypeError, ValueError):
        raise BootstrapError("archive link target is invalid") from None
    if member.kind == "hardlink":
        resolved = posixpath.normpath(target)
    else:
        resolved = posixpath.normpath(
            posixpath.join(posixpath.dirname(member.path), target)
        )
    if resolved != root and not resolved.startswith(root + "/"):
        raise BootstrapError("archive link target escapes expected root")
    return resolved


def _validate_graph(graph: ArchiveGraph, allow_contained_links: bool) -> dict[str, str]:
    indexed = {member.path: member for member in graph.members}
    root_member = indexed.get(graph.root)
    if root_member is not None and root_member.kind != "directory":
        raise BootstrapError("archive root must be a directory")
    for member in graph.members:
        parent = posixpath.dirname(member.path)
        while parent and parent != graph.root:
            declared_parent = indexed.get(parent)
            if declared_parent is not None and declared_parent.kind != "directory":
                raise BootstrapError("archive member has a non-directory parent")
            parent = posixpath.dirname(parent)

    resolved: dict[str, str] = {}
    for member in graph.members:
        if member.kind in {"symlink", "hardlink"}:
            if not allow_contained_links:
                raise BootstrapError(
                    "bundle archive accepts regular files and directories only"
                )
            target = _resolved_link(member, graph.root)
            if target not in indexed:
                raise BootstrapError("undeclared archive link target")
            if member.kind == "hardlink" and indexed[target].kind not in {
                "regular",
                "hardlink",
            }:
                raise BootstrapError("archive hardlink target is not regular")
            resolved[member.path] = target

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(path: str) -> None:
        if path in visited:
            return
        if path in visiting:
            raise BootstrapError("archive link cycle")
        visiting.add(path)
        target = resolved.get(path)
        if target in resolved:
            visit(target)
        visiting.remove(path)
        visited.add(path)

    for path in resolved:
        visit(path)
    return resolved


def _archive_relative(path: str, root: str) -> tuple[str, ...]:
    if path == root:
        return ()
    return tuple(path[len(root) + 1 :].split("/"))


def _open_relative_directory(
    root_fd: int, parts: tuple[str, ...], *, create: bool
) -> int:
    fd = os.dup(root_fd)
    try:
        for part in parts:
            try:
                next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700, dir_fd=fd)
                os.fsync(fd)
                next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            except OSError as error:
                if error.errno in _HOSTILE_OPEN_ERRNOS:
                    raise BootstrapError(
                        "archive member has a non-directory parent"
                    ) from None
                raise
            os.close(fd)
            fd = next_fd
        result = fd
        fd = -1
        return result
    finally:
        if fd >= 0:
            os.close(fd)


def _ensure_extracted_directory(root_fd: int, parts: tuple[str, ...]) -> None:
    fd = _open_relative_directory(root_fd, parts, create=True)
    os.close(fd)


def _open_extracted_regular(root_fd: int, parts: tuple[str, ...]) -> tuple[int, int]:
    if not parts:
        raise BootstrapError("archive root must be a directory")
    parent_fd = _open_relative_directory(root_fd, parts[:-1], create=True)
    try:
        try:
            fd = os.open(
                parts[-1],
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
        except OSError as error:
            if error.errno in _HOSTILE_OPEN_ERRNOS or error.errno == errno.EEXIST:
                raise BootstrapError("archive extraction target is unsafe") from None
            raise
        return fd, parent_fd
    except Exception:
        os.close(parent_fd)
        raise


def _ultimate_hardlink_target(
    path: str, indexed: dict[str, ArchiveMember], resolved: dict[str, str]
) -> str:
    target = resolved[path]
    while indexed[target].kind == "hardlink":
        target = resolved[target]
    return target


def _create_extracted_links(root_fd: int, scan: _ArchiveScan) -> None:
    indexed = {member.path: member for member in scan.graph.members}
    for member in scan.graph.members:
        if member.kind != "hardlink":
            continue
        parts = _archive_relative(member.path, scan.graph.root)
        parent_fd = _open_relative_directory(root_fd, parts[:-1], create=True)
        try:
            target = _ultimate_hardlink_target(
                member.path, indexed, scan.resolved_links
            )
            target_parts = _archive_relative(target, scan.graph.root)
            os.link(
                "/".join(target_parts),
                parts[-1],
                src_dir_fd=root_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    for member in scan.graph.members:
        if member.kind != "symlink":
            continue
        parts = _archive_relative(member.path, scan.graph.root)
        parent_fd = _open_relative_directory(root_fd, parts[:-1], create=True)
        try:
            os.symlink(member.link_target, parts[-1], dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)


def _apply_extracted_directory_modes(root_fd: int, graph: ArchiveGraph) -> None:
    directories = [member for member in graph.members if member.kind == "directory"]
    for member in sorted(
        directories, key=lambda item: item.path.count("/"), reverse=True
    ):
        parts = _archive_relative(member.path, graph.root)
        fd = (
            os.dup(root_fd)
            if not parts
            else _open_relative_directory(root_fd, parts, create=False)
        )
        try:
            os.fchmod(fd, member.mode)
            os.fsync(fd)
        finally:
            os.close(fd)


def _scan_archive(
    source: _BoundArchive,
    policy: ArchivePolicy,
    *,
    extraction_root_fd: int | None = None,
    expected: _ArchiveScan | None = None,
) -> _ArchiveScan:
    policy = _validated_policy(policy)
    _validate_archive_source(source)
    members: list[ArchiveMember] = []
    regular_sha256: dict[str, str] = {}
    seen: set[str] = set()
    total = 0
    try:
        with _tar_stream(source, policy) as archive:
            for info in archive:
                if len(members) >= policy.max_members:
                    raise BootstrapError("archive has too many members")
                name = _normalized_member_name(info.name, seen)
                if name != policy.expected_root and not name.startswith(
                    policy.expected_root + "/"
                ):
                    raise BootstrapError("archive member is outside the expected root")
                kind = _member_kind(info, policy)
                if type(info.mode) is not int or not 0 <= info.mode <= 0o777:
                    raise BootstrapError("archive member mode is unsafe")
                if kind != "regular" and info.size != 0:
                    raise BootstrapError("non-regular archive member declares data")
                size = info.size if kind == "regular" else 0
                if type(size) is not int or size < 0:
                    raise BootstrapError("archive member size is invalid")
                if kind == "regular":
                    total += size
                    if total > policy.max_total_size:
                        raise BootstrapError("archive exceeds expanded size limit")
                member = ArchiveMember(
                    name,
                    kind,  # type: ignore[arg-type]
                    size,
                    info.mode,
                    info.linkname if kind in {"symlink", "hardlink"} else None,
                )
                members.append(member)
                relative_parts = _archive_relative(name, policy.expected_root)
                if extraction_root_fd is not None and kind == "directory":
                    _ensure_extracted_directory(extraction_root_fd, relative_parts)
                if kind == "regular":
                    extracted = archive.extractfile(info)
                    if extracted is None:
                        raise BootstrapError("archive stream is invalid")
                    digest = hashlib.sha256()
                    actual_size = 0
                    output_fd: int | None = None
                    output_parent_fd: int | None = None
                    if extraction_root_fd is not None:
                        output_fd, output_parent_fd = _open_extracted_regular(
                            extraction_root_fd, relative_parts
                        )
                    try:
                        while block := extracted.read(_READ_SIZE):
                            actual_size += len(block)
                            if (
                                actual_size > size
                                or actual_size > policy.max_total_size
                            ):
                                raise BootstrapError(
                                    "archive exceeds expanded size limit"
                                )
                            digest.update(block)
                            if output_fd is not None:
                                write_all(output_fd, block)
                    finally:
                        try:
                            extracted.close()
                        finally:
                            try:
                                if output_fd is not None:
                                    try:
                                        if actual_size == size:
                                            os.fchmod(output_fd, info.mode)
                                            os.fsync(output_fd)
                                    finally:
                                        os.close(output_fd)
                            finally:
                                if output_parent_fd is not None:
                                    try:
                                        os.fsync(output_parent_fd)
                                    finally:
                                        os.close(output_parent_fd)
                    if actual_size != size:
                        raise BootstrapError("archive stream is invalid")
                    regular_sha256[name] = digest.hexdigest()
    except BootstrapError:
        raise
    except (OSError, EOFError, tarfile.TarError) as error:
        raise BootstrapError("archive stream is invalid") from error
    if not members:
        raise BootstrapError("archive has no members")
    _validate_archive_source(source)
    graph = ArchiveGraph(
        policy.expected_root,
        tuple(sorted(members, key=lambda member: member.path)),
        total,
    )
    resolved = _validate_graph(graph, policy.allow_contained_links)
    scan = _ArchiveScan(graph, regular_sha256, resolved)
    if expected is not None and scan != expected:
        raise BootstrapError("archive changed between inspection and extraction")
    if extraction_root_fd is not None:
        _create_extracted_links(extraction_root_fd, scan)
        _apply_extracted_directory_modes(extraction_root_fd, graph)
        os.fsync(extraction_root_fd)
    return scan


def inspect_tar(path: Path, policy: ArchivePolicy) -> ArchiveGraph:
    """Stream and validate a complete archive graph without extracting it."""
    source: _BoundArchive | None = None
    try:
        source = _open_archive_source(path, "archive")
        return _scan_archive(source, policy).graph
    except (OSError, ValueError):
        raise BootstrapError("archive access failed") from None
    finally:
        if source is not None:
            os.close(source.fd)


def _hash_pinned_archive(source: _BoundArchive, spec: DownloadSpec) -> None:
    expected_size, expected_sha256 = _pinned_size_digest(spec.size, spec.sha256)
    _validate_archive_source(source)
    before = os.fstat(source.fd)
    if before.st_size != expected_size:
        raise BootstrapError("bundle archive size mismatch")
    digest = hashlib.sha256()
    offset = 0
    while block := os.pread(source.fd, _READ_SIZE, offset):
        digest.update(block)
        offset += len(block)
    after = os.fstat(source.fd)
    if _file_identity(before) != _file_identity(after):
        raise BootstrapError("bundle archive changed while hashing")
    _validate_archive_source(source)
    if offset != expected_size or digest.hexdigest() != expected_sha256:
        raise BootstrapError("bundle archive digest mismatch")


def _safe_relative_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise BootstrapError(f"{label} is invalid")
    normalized = posixpath.normpath(value)
    if (
        value != normalized
        or value.startswith("/")
        or normalized in {".", ".."}
        or normalized.startswith("../")
        or "\x00" in value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise BootstrapError(f"{label} is invalid")
    try:
        os.fsencode(value)
    except (TypeError, ValueError):
        raise BootstrapError(f"{label} is invalid") from None
    return value


def _validated_bundle_manifest(
    manifest: BootstrapManifest,
) -> tuple[tuple[SourceSpec, ...], tuple[ArtifactSpec, ...]]:
    if not isinstance(manifest, BootstrapManifest):
        raise BootstrapError("bundle trust manifest is invalid")
    if not isinstance(manifest.profile, str) or not manifest.profile:
        raise BootstrapError("bundle trust manifest is invalid")
    if not isinstance(manifest.bundle, DownloadSpec):
        raise BootstrapError("bundle trust manifest is invalid")
    _safe_root(manifest.bundle.expected_root)
    _pinned_size_digest(manifest.bundle.size, manifest.bundle.sha256)
    if type(manifest.sources) is not tuple or type(manifest.artifacts) is not tuple:
        raise BootstrapError("bundle trust manifest is invalid")

    source_names: set[str] = set()
    for source in manifest.sources:
        if (
            not isinstance(source, SourceSpec)
            or not isinstance(source.name, str)
            or not source.name
            or source.name in source_names
            or not isinstance(source.revision, str)
            or re.fullmatch(r"[0-9a-f]{40}", source.revision) is None
        ):
            raise BootstrapError("bundle trust manifest is invalid")
        _safe_root(source.name)
        source_names.add(source.name)

    artifact_names: set[str] = set()
    artifact_paths: set[str] = set()
    for artifact in manifest.artifacts:
        if not isinstance(artifact, ArtifactSpec):
            raise BootstrapError("bundle trust manifest is invalid")
        relative = _safe_relative_path(artifact.relative_path, "bundle artifact path")
        if (
            not isinstance(artifact.logical_name, str)
            or not artifact.logical_name
            or artifact.logical_name in artifact_names
            or relative in artifact_paths
            or type(artifact.size) is not int
            or artifact.size < 0
            or not isinstance(artifact.sha256, str)
            or _SHA256.fullmatch(artifact.sha256) is None
            or type(artifact.mode) is not int
            or not 0 <= artifact.mode <= 0o777
            or artifact.private is not False
        ):
            raise BootstrapError("bundle trust manifest is invalid")
        artifact_names.add(artifact.logical_name)
        artifact_paths.add(relative)
    if not manifest.artifacts:
        raise BootstrapError("bundle trust manifest is invalid")
    if artifact_paths & _bundle_metadata_paths(manifest):
        raise BootstrapError("bundle trust manifest is invalid")
    return manifest.sources, manifest.artifacts


def _bundle_metadata_paths(manifest: BootstrapManifest) -> frozenset[str]:
    return _BUNDLE_FIXED_METADATA | {
        f"licenses/{source.name}.txt" for source in manifest.sources
    }


def _expected_internal_manifest(manifest: BootstrapManifest) -> bytes:
    sources, artifacts = _validated_bundle_manifest(manifest)
    return canonical_json(
        {
            "artifacts": [
                {
                    "logical_name": artifact.logical_name,
                    "mode": artifact.mode,
                    "private": artifact.private,
                    "relative_path": artifact.relative_path,
                    "sha256": artifact.sha256,
                    "size": artifact.size,
                }
                for artifact in artifacts
            ],
            "profile": manifest.profile,
            "schema": 1,
            "sources": {source.name: source.revision for source in sources},
        }
    )


def _open_bundle_root(path: Path) -> int:
    parts = _path_components(path, "bundle root")
    fd = _open_directory_components(parts, "bundle root")
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise BootstrapError("bundle root is unsafe")
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_relative_regular(
    root_fd: int, relative_path: str, label: str
) -> tuple[int, int, str]:
    parts = tuple(relative_path.split("/"))
    parent_fd = _open_relative_directory(root_fd, parts[:-1], create=False)
    try:
        try:
            fd = os.open(
                parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent_fd,
            )
        except OSError as error:
            if error.errno in _HOSTILE_OPEN_ERRNOS:
                raise BootstrapError(
                    f"{label} is not a regular no-follow file"
                ) from None
            raise
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise BootstrapError(f"{label} is not a regular no-follow file")
        except Exception:
            os.close(fd)
            raise
        return fd, parent_fd, parts[-1]
    except Exception:
        os.close(parent_fd)
        raise


def _read_relative_regular(
    root_fd: int,
    relative_path: str,
    label: str,
    *,
    maximum_size: int,
    include_data: bool,
) -> tuple[os.stat_result, str, bytes | None]:
    fd, parent_fd, name = _open_relative_regular(root_fd, relative_path, label)
    verification_fd: int | None = None
    try:
        before = os.fstat(fd)
        if before.st_size > maximum_size:
            raise BootstrapError(f"{label} exceeds its size limit")
        digest = hashlib.sha256()
        chunks: list[bytes] | None = [] if include_data else None
        total = 0
        while block := os.read(fd, _READ_SIZE):
            total += len(block)
            if total > maximum_size:
                raise BootstrapError(f"{label} exceeds its size limit")
            digest.update(block)
            if chunks is not None:
                chunks.append(block)
        after = os.fstat(fd)
        identity = _file_identity(after)
        if total != after.st_size or _file_identity(before) != identity:
            raise BootstrapError(f"{label} changed while hashing")
        verification_fd = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_fd,
        )
        verification = os.fstat(verification_fd)
        if (
            not stat.S_ISREG(verification.st_mode)
            or _file_identity(verification) != identity
        ):
            raise BootstrapError(f"{label} changed while hashing")
        data = b"".join(chunks) if chunks is not None else None
        return after, digest.hexdigest(), data
    finally:
        if verification_fd is not None:
            os.close(verification_fd)
        os.close(fd)
        os.close(parent_fd)


def _walk_tree(root_fd: int) -> dict[str, os.stat_result]:
    result: dict[str, os.stat_result] = {}

    def walk(directory_fd: int, prefix: str) -> None:
        with os.scandir(directory_fd) as entries:
            ordered = sorted(entries, key=lambda entry: entry.name)
        for entry in ordered:
            if (
                entry.name in {"", ".", ".."}
                or "/" in entry.name
                or "\x00" in entry.name
            ):
                raise BootstrapError("extracted archive tree is unsafe")
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            info = entry.stat(follow_symlinks=False)
            result[relative] = info
            if stat.S_ISDIR(info.st_mode):
                try:
                    child_fd = os.open(
                        entry.name, _DIRECTORY_FLAGS, dir_fd=directory_fd
                    )
                except OSError as error:
                    raise BootstrapError(
                        "extracted archive tree changed during verification"
                    ) from error
                try:
                    walk(child_fd, relative)
                finally:
                    os.close(child_fd)

    walk(root_fd, "")
    return result


def _expected_extracted_paths(
    graph: ArchiveGraph,
) -> tuple[dict[str, ArchiveMember], set[str]]:
    expected: dict[str, ArchiveMember] = {}
    implicit_directories: set[str] = set()
    for member in graph.members:
        parts = _archive_relative(member.path, graph.root)
        if not parts:
            continue
        relative = "/".join(parts)
        expected[relative] = member
        for index in range(1, len(parts)):
            implicit_directories.add("/".join(parts[:index]))
    implicit_directories -= set(expected)
    return expected, implicit_directories


def _verify_extracted_tree(root_fd: int, scan: _ArchiveScan) -> _TreeBinding:
    root_info = os.fstat(root_fd)
    root_member = next(
        (member for member in scan.graph.members if member.path == scan.graph.root),
        None,
    )
    expected_root_mode = 0o700 if root_member is None else root_member.mode
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != os.getuid()
        or stat.S_IMODE(root_info.st_mode) != expected_root_mode
    ):
        raise BootstrapError("extracted archive root mismatch")

    expected, implicit_directories = _expected_extracted_paths(scan.graph)
    actual = _walk_tree(root_fd)
    if set(actual) != set(expected) | implicit_directories:
        raise BootstrapError("extracted archive member set mismatch")
    indexed = {member.path: member for member in scan.graph.members}
    for relative in implicit_directories:
        info = actual[relative]
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
            raise BootstrapError("extracted archive directory mismatch")
    for relative, member in expected.items():
        info = actual[relative]
        if info.st_uid != os.getuid():
            raise BootstrapError("extracted archive ownership mismatch")
        if member.kind == "directory":
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_IMODE(info.st_mode) != member.mode
            ):
                raise BootstrapError("extracted archive directory mismatch")
        elif member.kind == "regular":
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != member.mode
                or info.st_size != member.size
            ):
                raise BootstrapError("extracted archive regular file mismatch")
            verified_info, digest, _data = _read_relative_regular(
                root_fd,
                relative,
                "extracted archive regular file",
                maximum_size=member.size,
                include_data=False,
            )
            if (
                verified_info.st_size != member.size
                or digest != scan.regular_sha256[member.path]
            ):
                raise BootstrapError("extracted archive regular file mismatch")
            actual[relative] = verified_info
        elif member.kind == "symlink":
            if not stat.S_ISLNK(info.st_mode):
                raise BootstrapError("extracted archive symlink mismatch")
            parts = tuple(relative.split("/"))
            parent_fd = _open_relative_directory(root_fd, parts[:-1], create=False)
            try:
                if os.readlink(parts[-1], dir_fd=parent_fd) != member.link_target:
                    raise BootstrapError("extracted archive symlink mismatch")
            finally:
                os.close(parent_fd)
        else:
            if not stat.S_ISREG(info.st_mode):
                raise BootstrapError("extracted archive hardlink mismatch")
            target = _ultimate_hardlink_target(
                member.path, indexed, scan.resolved_links
            )
            target_relative = "/".join(_archive_relative(target, scan.graph.root))
            target_info = actual[target_relative]
            if (info.st_dev, info.st_ino) != (target_info.st_dev, target_info.st_ino):
                raise BootstrapError("extracted archive hardlink mismatch")
    final_root = os.fstat(root_fd)
    final_actual = _walk_tree(root_fd)
    if _file_identity(final_root) != _file_identity(root_info) or set(
        final_actual
    ) != set(actual):
        raise BootstrapError("extracted archive tree changed during verification")
    for relative, info in final_actual.items():
        if _file_identity(info) != _file_identity(actual[relative]):
            raise BootstrapError("extracted archive tree changed during verification")
    return _TreeBinding(
        _file_identity(final_root),
        tuple(
            (relative, _file_identity(info))
            for relative, info in sorted(final_actual.items())
        ),
    )


def _validate_bundle_graph(scan: _ArchiveScan, manifest: BootstrapManifest) -> None:
    indexed = {member.path: member for member in scan.graph.members}
    root = scan.graph.root
    metadata_paths = _bundle_metadata_paths(manifest)
    for relative in metadata_paths:
        member = indexed.get(f"{root}/{relative}")
        if member is None:
            raise BootstrapError("bundle metadata is missing")
        if member.kind != "regular" or member.mode != 0o644 or member.size == 0:
            raise BootstrapError("bundle metadata is invalid")
    artifact_paths = {artifact.relative_path for artifact in manifest.artifacts}
    for artifact in manifest.artifacts:
        member = indexed.get(f"{root}/{artifact.relative_path}")
        if member is None or member.kind != "regular":
            raise BootstrapError("bundle artifact is missing")
    for member in scan.graph.members:
        parts = _archive_relative(member.path, root)
        if parts and parts[0] not in _BUNDLE_TOP_LEVEL:
            raise BootstrapError("unexpected bundle top-level entry")
        if (
            member.kind == "regular"
            and "/".join(parts) not in artifact_paths | metadata_paths
        ):
            raise BootstrapError("undeclared bundle regular file")


def _validate_bundle_tree_types(root_fd: int, manifest: BootstrapManifest) -> None:
    actual = _walk_tree(root_fd)
    metadata_paths = _bundle_metadata_paths(manifest)
    artifact_paths = {artifact.relative_path for artifact in manifest.artifacts}
    for relative in metadata_paths:
        info = actual.get(relative)
        if info is None:
            raise BootstrapError("bundle metadata is missing")
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o644
            or info.st_size == 0
        ):
            raise BootstrapError("bundle metadata is invalid")
    for relative, info in actual.items():
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise BootstrapError(
                "bundle root accepts regular files and directories only"
            )
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise BootstrapError(
                "bundle root accepts regular files and directories only"
            )
        top_level = relative.split("/", 1)[0]
        if top_level not in _BUNDLE_TOP_LEVEL:
            raise BootstrapError("unexpected bundle top-level entry")
        if stat.S_ISREG(info.st_mode) and relative not in (
            artifact_paths | metadata_paths
        ):
            raise BootstrapError("undeclared bundle regular file")


def _verify_bundle_root(root: Path, manifest: BootstrapManifest) -> VerifiedBundle:
    _validated_bundle_manifest(manifest)
    root_path = Path(root)
    root_fd = _open_bundle_root(root_path)
    try:
        _validate_bundle_tree_types(root_fd, manifest)
        expected_manifest = _expected_internal_manifest(manifest)
        info, manifest_sha256, manifest_data = _read_relative_regular(
            root_fd,
            "bundle-manifest.json",
            "bundle manifest",
            maximum_size=max(len(expected_manifest), 1),
            include_data=True,
        )
        if stat.S_IMODE(info.st_mode) != 0o644 or manifest_data != expected_manifest:
            raise BootstrapError("bundle manifest mismatch")
        for artifact in manifest.artifacts:
            info, digest, _data = _read_relative_regular(
                root_fd,
                artifact.relative_path,
                "bundle artifact",
                maximum_size=artifact.size,
                include_data=False,
            )
            if (
                info.st_size != artifact.size
                or stat.S_IMODE(info.st_mode) != artifact.mode
                or digest != artifact.sha256
            ):
                raise BootstrapError("bundle artifact mismatch")
        return VerifiedBundle(root_path, manifest_sha256, manifest.artifacts)
    except (FileNotFoundError, NotADirectoryError):
        raise BootstrapError("bundle artifact mismatch") from None
    finally:
        os.close(root_fd)


def verify_bundle_root(root: Path, manifest: BootstrapManifest) -> VerifiedBundle:
    """Verify a link-free extracted bundle against its complete trust manifest."""
    try:
        return _verify_bundle_root(root, manifest)
    except (OSError, ValueError):
        raise BootstrapError("bundle verification failed") from None


def _remove_directory_contents(directory_fd: int) -> None:
    with os.scandir(directory_fd) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            try:
                _remove_directory_contents(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def _name_matches_directory(parent_fd: int, name: str, directory_fd: int) -> bool:
    named_fd: int | None = None
    try:
        named_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        named = os.fstat(named_fd)
        held = os.fstat(directory_fd)
        return (named.st_dev, named.st_ino) == (held.st_dev, held.st_ino)
    except (FileNotFoundError, NotADirectoryError):
        return False
    finally:
        if named_fd is not None:
            os.close(named_fd)


def _remove_if_bound_directory(parent_fd: int, name: str, directory_fd: int) -> None:
    if not _name_matches_directory(parent_fd, name, directory_fd):
        return
    _remove_directory_contents(directory_fd)
    if _name_matches_directory(parent_fd, name, directory_fd):
        os.rmdir(name, dir_fd=parent_fd)
        os.fsync(parent_fd)


def _destination(path: str | os.PathLike[str]) -> Path:
    parts = _path_components(path, "archive destination")
    return Path("/", *parts)


def _extract_and_publish(
    archive: _BoundArchive,
    destination: Path,
    policy: ArchivePolicy,
    first_scan: _ArchiveScan,
    bundle_manifest: BootstrapManifest | None,
) -> VerifiedBundle | Path:
    destination = _destination(destination)
    ensure_private_directory(destination.parent)
    parent_fd = open_owned_root(destination.parent)
    stage_name = ""
    stage_fd: int | None = None
    renamed = False
    published = False
    try:
        try:
            os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise BootstrapError("archive destination already exists")
        for _ in range(100):
            candidate = f".{destination.name}.{secrets.token_hex(12)}.stage"
            try:
                os.mkdir(candidate, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            stage_name = candidate
            break
        else:
            raise BootstrapError("cannot reserve archive staging directory")
        stage_fd = os.open(stage_name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        if stat.S_IMODE(os.fstat(stage_fd).st_mode) != 0o700:
            raise BootstrapError("archive staging directory is unsafe")

        second_scan = _scan_archive(
            archive,
            policy,
            extraction_root_fd=stage_fd,
            expected=first_scan,
        )
        _verify_extracted_tree(stage_fd, second_scan)
        stage_path = destination.parent / stage_name
        if bundle_manifest is None:
            result: VerifiedBundle | Path = destination
        else:
            verified = verify_bundle_root(stage_path, bundle_manifest)
            result = VerifiedBundle(
                destination,
                verified.manifest_sha256,
                verified.artifacts,
            )
        binding = _verify_extracted_tree(stage_fd, second_scan)
        stage_info = os.stat(stage_name, dir_fd=parent_fd, follow_symlinks=False)
        held_stage_info = os.fstat(stage_fd)
        if not stat.S_ISDIR(stage_info.st_mode) or (
            stage_info.st_dev,
            stage_info.st_ino,
        ) != (held_stage_info.st_dev, held_stage_info.st_ino):
            raise BootstrapError("archive staging directory changed before publication")
        os.fsync(stage_fd)
        try:
            rename_noreplace(parent_fd, stage_name, parent_fd, destination.name)
        except FileExistsError:
            raise BootstrapError("archive destination already exists") from None
        renamed = True
        try:
            published_binding = _verify_extracted_tree(stage_fd, second_scan)
            if (
                published_binding.root_identity[:6] != binding.root_identity[:6]
                or published_binding.member_identities != binding.member_identities
                or not _name_matches_directory(parent_fd, destination.name, stage_fd)
            ):
                raise BootstrapError("archive publication verification failed")
        except (BootstrapError, OSError) as error:
            raise BootstrapError("archive publication verification failed") from error
        os.fsync(parent_fd)
        published = True
        return result
    finally:
        try:
            if not published and stage_name:
                try:
                    if stage_fd is not None:
                        cleanup_name = destination.name if renamed else stage_name
                        _remove_if_bound_directory(parent_fd, cleanup_name, stage_fd)
                    elif not renamed:
                        os.rmdir(stage_name, dir_fd=parent_fd)
                        os.fsync(parent_fd)
                except FileNotFoundError:
                    pass
        finally:
            try:
                if stage_fd is not None:
                    os.close(stage_fd)
            finally:
                os.close(parent_fd)


def _extract_bundle(
    path: Path, destination: Path, bundle_manifest: BootstrapManifest
) -> VerifiedBundle:
    _validated_bundle_manifest(bundle_manifest)
    archive = _open_archive_source(path, "bundle archive")
    policy = ArchivePolicy(
        bundle_manifest.bundle.expected_root,
        _BUNDLE_MAX_MEMBERS,
        _BUNDLE_MAX_TOTAL_SIZE,
        False,
    )
    try:
        _hash_pinned_archive(archive, bundle_manifest.bundle)
        first_scan = _scan_archive(archive, policy)
        _validate_bundle_graph(first_scan, bundle_manifest)
        result = _extract_and_publish(
            archive,
            destination,
            policy,
            first_scan,
            bundle_manifest,
        )
        if not isinstance(result, VerifiedBundle):
            raise TypeError("bundle extraction returned an invalid result")
        _validate_archive_source(archive)
        return result
    finally:
        os.close(archive.fd)


def extract_bundle(
    path: Path, destination: Path, bundle_manifest: BootstrapManifest
) -> VerifiedBundle:
    """Verify, stage, and atomically publish one link-free release bundle."""
    try:
        return _extract_bundle(path, destination, bundle_manifest)
    except (OSError, ValueError):
        raise BootstrapError("bundle extraction failed") from None


def _extract_ge(path: Path, destination: Path, expected_root: str) -> Path:
    archive = _open_archive_source(path, "archive")
    policy = ArchivePolicy(
        _safe_root(expected_root),
        _GE_MAX_MEMBERS,
        _GE_MAX_TOTAL_SIZE,
        True,
    )
    try:
        first_scan = _scan_archive(archive, policy)
        result = _extract_and_publish(archive, destination, policy, first_scan, None)
        if not isinstance(result, Path):
            raise TypeError("GE extraction returned an invalid result")
        _validate_archive_source(archive)
        return result
    finally:
        os.close(archive.fd)


def extract_ge(path: Path, destination: Path, expected_root: str) -> Path:
    """Validate and atomically publish one exact-digest GE archive tree."""
    try:
        return _extract_ge(path, destination, expected_root)
    except (OSError, ValueError):
        raise BootstrapError("GE extraction failed") from None
