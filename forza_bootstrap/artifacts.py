# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pinned artifact downloads and link-safe archive extraction."""

from __future__ import annotations

import errno
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
_BUNDLE_TOP_LEVEL = frozenset(
    {"bundle-manifest.json", "bin", "runtime", "licenses", "provenance", "SHA256SUMS"}
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
) -> None:
    fd: int | None = None
    verification_fd: int | None = None
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
    except (FileNotFoundError, OSError):
        raise BootstrapError("download staged file mismatch") from None
    finally:
        if verification_fd is not None:
            os.close(verification_fd)
        if fd is not None:
            os.close(fd)


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
        _verify_download_stage(parent_fd, temporary, spec.size, spec.sha256)
        try:
            rename_noreplace(parent_fd, temporary, parent_fd, spec.filename)
        except FileExistsError:
            raise BootstrapError("download cache destination already exists") from None
        published = True
        os.fsync(parent_fd)
        return final_path
    finally:
        if fd is not None:
            os.close(fd)
        if not published:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
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


def _source_identity(path: str | os.PathLike[str], label: str) -> _FileIdentity:
    fd, parent_fd, _name = _open_absolute_regular(path, label)
    try:
        return _file_identity(os.fstat(fd))
    finally:
        os.close(fd)
        os.close(parent_fd)


@contextmanager
def _tar_stream(path: Path) -> Iterator[tarfile.TarFile]:
    process: subprocess.Popen[bytes] | None = None
    stream: BinaryIO | None = None
    archive: tarfile.TarFile | None = None
    failed = True
    try:
        path_value = os.fspath(path)
        if path_value.endswith(".tar.zst"):
            try:
                process = subprocess.Popen(
                    ["zstd", "-dc", "--", path_value],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                )
            except OSError as error:
                raise BootstrapError("archive stream is invalid") from error
            if process.stdout is None:
                raise BootstrapError("archive stream is invalid")
            stream = process.stdout
        elif path_value.endswith((".tar", ".tar.gz", ".tgz")):
            fd, parent_fd, _name = _open_absolute_regular(path, "archive")
            os.close(parent_fd)
            stream = os.fdopen(fd, "rb")
        else:
            raise BootstrapError("archive format is unsupported")
        try:
            archive = tarfile.open(  # noqa: SIM115 - closed in shared finally
                fileobj=stream, mode="r|*"
            )
        except (OSError, tarfile.TarError) as error:
            raise BootstrapError("archive stream is invalid") from error
        yield archive
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
                if process is not None:
                    return_code = process.wait()
                    if not failed and return_code != 0:
                        raise BootstrapError("archive stream is invalid")


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
        os.fsencode(raw)
    except (TypeError, ValueError):
        raise BootstrapError("unsafe archive member name") from None
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
    path: Path,
    policy: ArchivePolicy,
    *,
    extraction_root_fd: int | None = None,
    expected: _ArchiveScan | None = None,
) -> _ArchiveScan:
    policy = _validated_policy(policy)
    before = _source_identity(path, "archive")
    members: list[ArchiveMember] = []
    regular_sha256: dict[str, str] = {}
    seen: set[str] = set()
    total = 0
    try:
        with _tar_stream(path) as archive:
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
    after = _source_identity(path, "archive")
    if after != before:
        raise BootstrapError("archive changed while inspecting")
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
    try:
        return _scan_archive(path, policy).graph
    except (OSError, ValueError):
        raise BootstrapError("archive access failed") from None


def _hash_pinned_archive(path: Path, spec: DownloadSpec) -> None:
    expected_size, expected_sha256 = _pinned_size_digest(spec.size, spec.sha256)
    fd, parent_fd, name = _open_absolute_regular(path, "bundle archive")
    verification_fd: int | None = None
    try:
        before = os.fstat(fd)
        if before.st_size != expected_size:
            raise BootstrapError("bundle archive size mismatch")
        digest = hashlib.sha256()
        while block := os.read(fd, _READ_SIZE):
            digest.update(block)
        after = os.fstat(fd)
        after_identity = _file_identity(after)
        if _file_identity(before) != after_identity:
            raise BootstrapError("bundle archive changed while hashing")
        try:
            verification_fd = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent_fd,
            )
        except OSError as error:
            if error.errno in _HOSTILE_OPEN_ERRNOS:
                raise BootstrapError("bundle archive changed while hashing") from None
            raise
        verification = os.fstat(verification_fd)
        if (
            not stat.S_ISREG(verification.st_mode)
            or _file_identity(verification) != after_identity
        ):
            raise BootstrapError("bundle archive changed while hashing")
        if digest.hexdigest() != expected_sha256:
            raise BootstrapError("bundle archive digest mismatch")
    finally:
        if verification_fd is not None:
            os.close(verification_fd)
        os.close(fd)
        os.close(parent_fd)


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
    return manifest.sources, manifest.artifacts


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


def _verify_extracted_tree(root_fd: int, scan: _ArchiveScan) -> None:
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


def _validate_bundle_graph(scan: _ArchiveScan, manifest: BootstrapManifest) -> None:
    indexed = {member.path: member for member in scan.graph.members}
    root = scan.graph.root
    required_manifest = f"{root}/bundle-manifest.json"
    if (
        indexed.get(required_manifest) is None
        or indexed[required_manifest].kind != "regular"
    ):
        raise BootstrapError("bundle manifest is missing")
    for artifact in manifest.artifacts:
        member = indexed.get(f"{root}/{artifact.relative_path}")
        if member is None or member.kind != "regular":
            raise BootstrapError("bundle artifact is missing")
    for member in scan.graph.members:
        parts = _archive_relative(member.path, root)
        if parts and parts[0] not in _BUNDLE_TOP_LEVEL:
            raise BootstrapError("unexpected bundle top-level entry")


def _validate_bundle_tree_types(root_fd: int) -> None:
    for relative, info in _walk_tree(root_fd).items():
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


def _verify_bundle_root(root: Path, manifest: BootstrapManifest) -> VerifiedBundle:
    _validated_bundle_manifest(manifest)
    root_path = Path(root)
    root_fd = _open_bundle_root(root_path)
    try:
        _validate_bundle_tree_types(root_fd)
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


def _destination(path: str | os.PathLike[str]) -> Path:
    parts = _path_components(path, "archive destination")
    return Path("/", *parts)


def _extract_and_publish(
    archive: Path,
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
        _verify_extracted_tree(stage_fd, second_scan)
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
        published = True
        os.fsync(parent_fd)
        return result
    finally:
        try:
            if not published and stage_name:
                try:
                    if stage_fd is not None:
                        _remove_directory_contents(stage_fd)
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
    archive = Path(path)
    _hash_pinned_archive(archive, bundle_manifest.bundle)
    policy = ArchivePolicy(
        bundle_manifest.bundle.expected_root,
        _BUNDLE_MAX_MEMBERS,
        _BUNDLE_MAX_TOTAL_SIZE,
        False,
    )
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
    return result


def extract_bundle(
    path: Path, destination: Path, bundle_manifest: BootstrapManifest
) -> VerifiedBundle:
    """Verify, stage, and atomically publish one link-free release bundle."""
    try:
        return _extract_bundle(path, destination, bundle_manifest)
    except (OSError, ValueError):
        raise BootstrapError("bundle extraction failed") from None


def _extract_ge(path: Path, destination: Path, expected_root: str) -> Path:
    archive = Path(path)
    policy = ArchivePolicy(
        _safe_root(expected_root),
        _GE_MAX_MEMBERS,
        _GE_MAX_TOTAL_SIZE,
        True,
    )
    first_scan = _scan_archive(archive, policy)
    result = _extract_and_publish(archive, destination, policy, first_scan, None)
    if not isinstance(result, Path):
        raise TypeError("GE extraction returned an invalid result")
    return result


def extract_ge(path: Path, destination: Path, expected_root: str) -> Path:
    """Validate and atomically publish one exact-digest GE archive tree."""
    try:
        return _extract_ge(path, destination, expected_root)
    except (OSError, ValueError):
        raise BootstrapError("GE extraction failed") from None
