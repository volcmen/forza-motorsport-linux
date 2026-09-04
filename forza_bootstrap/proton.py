# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Frozen ProtonUp acquisition and dedicated compatibility-tool staging."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from . import artifacts as artifacts_module
from .artifacts import verify_cached_file
from .model import (
    ArtifactSpec,
    BootstrapError,
    BootstrapManifest,
    DownloadSpec,
    ProtonUpSpec,
    canonical_json,
    sha256_bytes,
)
from .safeio import (
    atomic_write_private_json,
    ensure_private_directory,
    rename_noreplace,
    write_all,
)

_BASE_RELEASE = "GE-Proton11-3"
_FM_NAME = "GE-Proton11-3-FM"
_PROTONUP_VERSION = "0.1.5"
_PROTONUP_PROJECT = "tools/protonup"
_SUPPORTED_BUILDS_PATH = (
    Path(__file__).resolve().parents[1] / "manifests/supported-builds.toml"
)
_MARKER = ".forza-bootstrap.json"
_VDF = "compatibilitytool.vdf"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_REGULAR_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_READ_SIZE = 1024 * 1024
_MAX_VDF_SIZE = 1024 * 1024
_MANAGED_PATCH_TARGETS = {
    "windows.gaming.input.dll": (
        "files/lib/wine/x86_64-windows/windows.gaming.input.dll"
    ),
    "mountmgr.sys": "files/lib/wine/x86_64-windows/mountmgr.sys",
}
_MANAGED_RUNTIME_TARGETS = {
    "runtime/xgameruntime.dll": ("files/lib/wine/x86_64-windows/xgameruntime.dll"),
    "runtime/xgameruntime.so": "files/lib/wine/x86_64-unix/xgameruntime.so",
}
_TREE_EXCLUDES = frozenset(
    {
        _MARKER,
        _VDF,
        *_MANAGED_PATCH_TARGETS.values(),
        *_MANAGED_RUNTIME_TARGETS.values(),
    }
)
_ALLOWED_ENVIRONMENT = (
    "PATH",
    "HTTPS_PROXY",
    "NO_PROXY",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
)


class ToolDisposition(str, Enum):
    ABSENT = "absent"
    CREATED = "created"
    ADOPTED = "adopted"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class ToolIdentity:
    name: str
    base_release: str
    vdf_sha256: str
    managed_tree_sha256: str
    marker_sha256: str | None


@dataclass(frozen=True)
class ProtonRoots:
    repo_root: Path
    cache_root: Path
    extraction_root: Path


@dataclass(frozen=True)
class PublishedToolRecord:
    destination: Path
    disposition: ToolDisposition
    identity: ToolIdentity
    root_device: int
    root_inode: int
    published_tree_sha256: str


@dataclass(frozen=True)
class ToolInspection:
    disposition: ToolDisposition
    identity: ToolIdentity | None
    tree_sha256: str | None


@dataclass(frozen=True)
class _VdfToken:
    kind: str
    value: str
    start: int
    end: int


@dataclass(frozen=True)
class _VdfEntry:
    key: _VdfToken
    value: _VdfToken | dict[str, _VdfEntry]


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


def _absolute_path(value: object, label: str) -> Path:
    try:
        path = Path(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise BootstrapError(f"{label} path is invalid") from None
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise BootstrapError(f"{label} path must be absolute")
    return path


def _path_parts(path: Path, label: str) -> tuple[str, ...]:
    path = _absolute_path(path, label)
    parts = tuple(str(path).split("/")[1:])
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise BootstrapError(f"{label} path is invalid")
    return parts


def _open_directory(path: Path, label: str) -> int:
    parts = _path_parts(path, label)
    fd = os.open("/", _DIRECTORY_FLAGS)
    try:
        for part in parts:
            next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise BootstrapError(f"{label} is unsafe")
        return fd
    except Exception:
        os.close(fd)
        raise


def _read_regular_at(
    parent_fd: int, name: str, label: str, *, maximum_size: int | None = None
) -> tuple[bytes, os.stat_result]:
    fd: int | None = None
    verification_fd: int | None = None
    try:
        fd = os.open(name, _REGULAR_FLAGS, dir_fd=parent_fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
            raise BootstrapError(f"{label} is not a safe regular file")
        if maximum_size is not None and before.st_size > maximum_size:
            raise BootstrapError(f"{label} is too large")
        chunks: list[bytes] = []
        total = 0
        while block := os.read(fd, _READ_SIZE):
            total += len(block)
            if maximum_size is not None and total > maximum_size:
                raise BootstrapError(f"{label} is too large")
            chunks.append(block)
        after = os.fstat(fd)
        if _file_identity(before) != _file_identity(after) or total != after.st_size:
            raise BootstrapError(f"{label} changed while reading")
        verification_fd = os.open(name, _REGULAR_FLAGS, dir_fd=parent_fd)
        named = os.fstat(verification_fd)
        if _file_identity(named) != _file_identity(after):
            raise BootstrapError(f"{label} changed while reading")
        return b"".join(chunks), after
    except BootstrapError:
        raise
    except (FileNotFoundError, OSError):
        raise BootstrapError(f"{label} is not a safe regular file") from None
    finally:
        if verification_fd is not None:
            os.close(verification_fd)
        if fd is not None:
            os.close(fd)


def _tokenize_vdf(data: bytes) -> tuple[_VdfToken, ...]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise BootstrapError("compatibility-tool VDF is invalid") from None
    tokens: list[_VdfToken] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character.isspace():
            index += 1
            continue
        if text.startswith("//", index):
            newline = text.find("\n", index + 2)
            index = len(text) if newline < 0 else newline + 1
            continue
        if character in "{}":
            tokens.append(_VdfToken(character, character, index, index + 1))
            index += 1
            continue
        if character != '"':
            raise BootstrapError("compatibility-tool VDF is invalid")
        start = index
        index += 1
        value: list[str] = []
        while index < len(text) and text[index] != '"':
            if text[index] == "\\":
                index += 1
                if index >= len(text) or text[index] not in {'"', "\\"}:
                    raise BootstrapError("compatibility-tool VDF is invalid")
            value.append(text[index])
            index += 1
        if index >= len(text):
            raise BootstrapError("compatibility-tool VDF is invalid")
        index += 1
        tokens.append(_VdfToken("string", "".join(value), start, index))
    return tuple(tokens)


def _parse_vdf(data: bytes) -> dict[str, _VdfEntry]:
    tokens = _tokenize_vdf(data)
    position = 0

    def parse_entries(closing: bool) -> dict[str, _VdfEntry]:
        nonlocal position
        result: dict[str, _VdfEntry] = {}
        while position < len(tokens):
            if tokens[position].kind == "}":
                if not closing:
                    raise BootstrapError("compatibility-tool VDF is invalid")
                position += 1
                return result
            key = tokens[position]
            position += 1
            if key.kind != "string" or position >= len(tokens):
                raise BootstrapError("compatibility-tool VDF is invalid")
            if key.value in result:
                raise BootstrapError(f"duplicate VDF key: {key.value}")
            value_token = tokens[position]
            position += 1
            if value_token.kind == "{":
                value: _VdfToken | dict[str, _VdfEntry] = parse_entries(True)
            elif value_token.kind == "string":
                value = value_token
            else:
                raise BootstrapError("compatibility-tool VDF is invalid")
            result[key.value] = _VdfEntry(key, value)
        if closing:
            raise BootstrapError("compatibility-tool VDF is invalid")
        return result

    parsed = parse_entries(False)
    if position != len(tokens):
        raise BootstrapError("compatibility-tool VDF is invalid")
    return parsed


def _object(entry: _VdfEntry | None) -> dict[str, _VdfEntry]:
    if entry is None or not isinstance(entry.value, dict):
        raise BootstrapError("compatibility-tool VDF identity is invalid")
    return entry.value


def _vdf_identity(data: bytes, expected: str) -> tuple[_VdfToken, _VdfToken]:
    root = _object(_parse_vdf(data).get("compatibilitytools"))
    tools = _object(root.get("compat_tools"))
    if set(tools) != {expected}:
        raise BootstrapError("compatibility-tool VDF identity is invalid")
    tool = tools[expected]
    fields = _object(tool)
    display = fields.get("display_name")
    if display is None or not isinstance(display.value, _VdfToken):
        raise BootstrapError("compatibility-tool VDF identity is invalid")
    if display.value.value != expected:
        raise BootstrapError("compatibility-tool VDF identity is invalid")
    return tool.key, display.value


def _rewritten_vdf(data: bytes) -> bytes:
    tool_key, display = _vdf_identity(data, _BASE_RELEASE)
    text = data.decode("utf-8")
    for token in sorted((tool_key, display), key=lambda item: item.start, reverse=True):
        text = text[: token.start] + f'"{_FM_NAME}"' + text[token.end :]
    result = text.encode("utf-8")
    _vdf_identity(result, _FM_NAME)
    return result


def _atomic_write_bytes(parent_fd: int, name: str, data: bytes, mode: int) -> None:
    temporary = ""
    fd: int | None = None
    try:
        for _ in range(100):
            temporary = f".{name}.{secrets.token_hex(12)}.tmp"
            try:
                fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    mode,
                    dir_fd=parent_fd,
                )
                break
            except FileExistsError:
                continue
        else:
            raise BootstrapError("cannot reserve compatibility-tool temporary file")
        os.fchmod(fd, mode)
        write_all(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        temporary = ""
        os.fsync(parent_fd)
    finally:
        if fd is not None:
            os.close(fd)
        if temporary:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass


def _scan_tree(
    directory_fd: int, prefix: str = "", *, excludes: frozenset[str] = frozenset()
) -> list[dict[str, object]]:
    before = os.fstat(directory_fd)
    if not stat.S_ISDIR(before.st_mode) or before.st_uid != os.getuid():
        raise BootstrapError("compatibility-tool tree is unsafe")
    with os.scandir(directory_fd) as entries:
        names = sorted(entry.name for entry in entries)
    records: list[dict[str, object]] = []
    for name in names:
        relative = f"{prefix}/{name}" if prefix else name
        if relative in excludes:
            continue
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISDIR(info.st_mode):
                child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
                try:
                    child_before = os.fstat(child_fd)
                    if _file_identity(child_before) != _file_identity(info):
                        raise BootstrapError(
                            "compatibility-tool tree changed while hashing"
                        )
                    records.append(
                        {"kind": "directory", "mode": mode, "path": relative}
                    )
                    records.extend(_scan_tree(child_fd, relative, excludes=excludes))
                    child_after = os.fstat(child_fd)
                    named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if _file_identity(child_before) != _file_identity(
                        child_after
                    ) or _file_identity(child_after) != _file_identity(named):
                        raise BootstrapError(
                            "compatibility-tool tree changed while hashing"
                        )
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(info.st_mode):
                data, regular_info = _read_regular_at(
                    directory_fd, name, "compatibility-tool tree member"
                )
                if _file_identity(regular_info) != _file_identity(info):
                    raise BootstrapError(
                        "compatibility-tool tree changed while hashing"
                    )
                records.append(
                    {
                        "kind": "regular",
                        "mode": mode,
                        "path": relative,
                        "sha256": sha256_bytes(data),
                        "size": len(data),
                    }
                )
            elif stat.S_ISLNK(info.st_mode):
                target = os.readlink(name, dir_fd=directory_fd)
                after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if _file_identity(info) != _file_identity(after):
                    raise BootstrapError(
                        "compatibility-tool tree changed while hashing"
                    )
                records.append(
                    {
                        "kind": "symlink",
                        "mode": mode,
                        "path": relative,
                        "target": os.fsencode(target).hex(),
                    }
                )
            else:
                raise BootstrapError("compatibility-tool tree contains a special file")
        except BootstrapError:
            raise
        except (FileNotFoundError, NotADirectoryError, OSError):
            raise BootstrapError(
                "compatibility-tool tree changed while hashing"
            ) from None
    after = os.fstat(directory_fd)
    if _file_identity(before) != _file_identity(after):
        raise BootstrapError("compatibility-tool tree changed while hashing")
    return records


def _tree_digest(root_fd: int, *, excludes: frozenset[str] = frozenset()) -> str:
    return sha256_bytes(canonical_json(_scan_tree(root_fd, excludes=excludes)))


def _relative_regular_digest(root_fd: int, relative: str) -> tuple[str, int, int]:
    parts = relative.split("/")
    parent_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        data, info = _read_regular_at(
            parent_fd, parts[-1], "managed compatibility-tool target"
        )
        return sha256_bytes(data), len(data), stat.S_IMODE(info.st_mode)
    finally:
        os.close(parent_fd)


def _relative_state(root_fd: int, relative: str) -> tuple[str, int, int] | None:
    try:
        return _relative_regular_digest(root_fd, relative)
    except BootstrapError:
        parts = relative.split("/")
        parent_fd = os.dup(root_fd)
        try:
            for part in parts[:-1]:
                try:
                    next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent_fd)
                except FileNotFoundError:
                    return None
                os.close(parent_fd)
                parent_fd = next_fd
            try:
                os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
        finally:
            os.close(parent_fd)
        raise


def _supported_patch_hashes() -> dict[str, tuple[str, str]]:
    try:
        with _SUPPORTED_BUILDS_PATH.open("rb") as stream:
            document = tomllib.load(stream)
        patches = document["patches"]
        if not isinstance(patches, list):
            raise TypeError
        result: dict[str, tuple[str, str]] = {}
        for item in patches:
            if not isinstance(item, dict):
                raise TypeError
            name = item["file"]
            original = item["original_sha256"]
            patched = item["patched_sha256"]
            if (
                name not in _MANAGED_PATCH_TARGETS
                or name in result
                or not isinstance(original, str)
                or not isinstance(patched, str)
                or len(original) != 64
                or len(patched) != 64
                or any(character not in "0123456789abcdef" for character in original)
                or any(character not in "0123456789abcdef" for character in patched)
            ):
                raise TypeError
            result[name] = (original, patched)
        if set(result) != set(_MANAGED_PATCH_TARGETS):
            raise TypeError
        return result
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        raise BootstrapError("supported-build manifest is invalid") from None


def _managed_target_hashes(root_fd: int) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for relative in _MANAGED_PATCH_TARGETS.values():
        digest, _size, _mode = _relative_regular_digest(root_fd, relative)
        hashes[relative] = digest
    for relative in _MANAGED_RUNTIME_TARGETS.values():
        state = _relative_state(root_fd, relative)
        if state is not None:
            hashes[relative] = state[0]
    return hashes


def _validate_tool_manifest(manifest: BootstrapManifest) -> None:
    if (
        not isinstance(manifest, BootstrapManifest)
        or not isinstance(manifest.ge, DownloadSpec)
        or manifest.ge.name != _BASE_RELEASE
        or manifest.ge.release != _BASE_RELEASE
        or manifest.ge.expected_root != _BASE_RELEASE
        or not isinstance(manifest.artifacts, tuple)
    ):
        raise BootstrapError("GE-Proton manifest is invalid")


def _runtime_artifacts(manifest: BootstrapManifest) -> dict[str, ArtifactSpec]:
    selected: dict[str, ArtifactSpec] = {}
    for artifact in manifest.artifacts:
        if not isinstance(artifact, ArtifactSpec):
            raise BootstrapError("GE-Proton manifest is invalid")
        if artifact.relative_path in _MANAGED_RUNTIME_TARGETS:
            if artifact.relative_path in selected:
                raise BootstrapError("GE-Proton manifest is invalid")
            selected[artifact.relative_path] = artifact
    if selected and set(selected) != set(_MANAGED_RUNTIME_TARGETS):
        raise BootstrapError("GE-Proton manifest is invalid")
    return selected


def _validate_managed_targets(root_fd: int, manifest: BootstrapManifest | None) -> None:
    supported = _supported_patch_hashes()
    patch_states: list[int] = []
    for name, relative in _MANAGED_PATCH_TARGETS.items():
        digest, _size, _mode = _relative_regular_digest(root_fd, relative)
        accepted = supported[name]
        if digest not in accepted:
            raise BootstrapError("managed compatibility-tool target is unsupported")
        patch_states.append(accepted.index(digest))
    if len(set(patch_states)) != 1:
        raise BootstrapError("managed compatibility-tool targets are mixed")

    runtime_states = {
        bundle_path: _relative_state(root_fd, destination)
        for bundle_path, destination in _MANAGED_RUNTIME_TARGETS.items()
    }
    present = {name for name, state in runtime_states.items() if state is not None}
    if present and present != set(_MANAGED_RUNTIME_TARGETS):
        raise BootstrapError("managed compatibility-tool targets are mixed")
    if manifest is None:
        if present:
            raise BootstrapError("managed compatibility-tool target is unsupported")
        return
    _validate_tool_manifest(manifest)
    artifacts = _runtime_artifacts(manifest)
    if present and not artifacts:
        raise BootstrapError("managed compatibility-tool target is unsupported")
    for bundle_path in present:
        artifact = artifacts[bundle_path]
        state = runtime_states[bundle_path]
        if state is None:
            raise BootstrapError("managed compatibility-tool targets are mixed")
        digest, size, mode = state
        if (
            not isinstance(artifact.sha256, str)
            or digest != artifact.sha256
            or size != artifact.size
            or mode != artifact.mode
        ):
            raise BootstrapError("managed compatibility-tool target is unsupported")


def _name_matches_directory(parent_fd: int, name: str, root_fd: int) -> bool:
    named_fd: int | None = None
    try:
        named_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        named = os.fstat(named_fd)
        held = os.fstat(root_fd)
        return (named.st_dev, named.st_ino) == (held.st_dev, held.st_ino)
    except (FileNotFoundError, NotADirectoryError):
        return False
    finally:
        if named_fd is not None:
            os.close(named_fd)


def _restore_recovery(parent_fd: int, recovery: str, destination: str) -> None:
    try:
        rename_noreplace(parent_fd, recovery, parent_fd, destination)
    except (FileExistsError, FileNotFoundError):
        return
    os.fsync(parent_fd)


def _quarantine_publication_mismatches(
    parent_fd: int, destination: str
) -> tuple[str, ...]:
    recoveries: list[str] = []
    for _ in range(100):
        try:
            os.stat(destination, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            os.fsync(parent_fd)
            return tuple(recoveries)
        recovery = f".{destination}.{secrets.token_hex(12)}.publication-recovery"
        try:
            rename_noreplace(parent_fd, destination, parent_fd, recovery)
        except FileExistsError:
            continue
        except FileNotFoundError:
            continue
        recoveries.append(recovery)
        os.fsync(parent_fd)
    raise BootstrapError("cannot quarantine failed compatibility-tool publication")


def _remove_directory_contents(directory_fd: int) -> None:
    with os.scandir(directory_fd) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            try:
                held = os.fstat(child_fd)
                if _file_identity(held) != _file_identity(info):
                    raise BootstrapError("compatibility-tool changed during rollback")
                _remove_directory_contents(child_fd)
                named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if _file_identity(os.fstat(child_fd)) != _file_identity(named):
                    raise BootstrapError("compatibility-tool changed during rollback")
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if _file_identity(current) != _file_identity(info):
                raise BootstrapError("compatibility-tool changed during rollback")
            os.unlink(name, dir_fd=directory_fd)


def _marker_value(root_fd: int, identity: ToolIdentity) -> dict[str, object]:
    return {
        "base_release": identity.base_release,
        "managed_targets": _managed_target_hashes(root_fd),
        "managed_tree_sha256": identity.managed_tree_sha256,
        "name": identity.name,
        "schema": 1,
        "vdf_sha256": identity.vdf_sha256,
    }


def _valid_recorded_targets(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    keys = set(value)
    patch_paths = set(_MANAGED_PATCH_TARGETS.values())
    runtime_paths = set(_MANAGED_RUNTIME_TARGETS.values())
    if frozenset(keys) not in {
        frozenset(patch_paths),
        frozenset(patch_paths | runtime_paths),
    }:
        return False
    if any(
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        for digest in value.values()
    ):
        return False
    supported = _supported_patch_hashes()
    return all(
        value[relative] in supported[name]
        for name, relative in _MANAGED_PATCH_TARGETS.items()
    )


def _read_identity(
    root_fd: int, *, require_recorded_targets: bool = True
) -> ToolIdentity:
    vdf, _info = _read_regular_at(
        root_fd, _VDF, "compatibility-tool VDF", maximum_size=_MAX_VDF_SIZE
    )
    _vdf_identity(vdf, _FM_NAME)
    marker, _marker_info = _read_regular_at(
        root_fd, _MARKER, "compatibility-tool marker", maximum_size=64 * 1024
    )
    try:
        value = json.loads(marker)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BootstrapError("compatibility-tool marker is invalid") from None
    expected_keys = {
        "base_release",
        "managed_targets",
        "managed_tree_sha256",
        "name",
        "schema",
        "vdf_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise BootstrapError("compatibility-tool marker is invalid")
    identity = ToolIdentity(
        name=value.get("name"),
        base_release=value.get("base_release"),
        vdf_sha256=value.get("vdf_sha256"),
        managed_tree_sha256=value.get("managed_tree_sha256"),
        marker_sha256=sha256_bytes(marker),
    )
    digests = (
        identity.vdf_sha256,
        identity.managed_tree_sha256,
        identity.marker_sha256,
    )
    if (
        value.get("schema") != 1
        or identity.name != _FM_NAME
        or identity.base_release != _BASE_RELEASE
        or any(
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            for digest in digests
        )
        or identity.vdf_sha256 != sha256_bytes(vdf)
        or identity.managed_tree_sha256
        != _tree_digest(root_fd, excludes=_TREE_EXCLUDES)
        or not _valid_recorded_targets(value.get("managed_targets"))
        or (
            require_recorded_targets
            and value.get("managed_targets") != _managed_target_hashes(root_fd)
        )
        or canonical_json(value) != marker
    ):
        raise BootstrapError("compatibility-tool marker is invalid")
    return identity


def protonup_argv(repo_root: Path, cache_root: Path) -> tuple[str, ...]:
    """Return the one permitted, download-only ProtonUp command."""
    repository = _absolute_path(repo_root, "repository root")
    cache = _absolute_path(cache_root, "ProtonUp cache")
    return (
        "uv",
        "run",
        "--frozen",
        "--project",
        str(repository / _PROTONUP_PROJECT),
        "protonup",
        "--download",
        "-t",
        _BASE_RELEASE,
        "-o",
        str(cache),
        "-y",
    )


def _validated_acquisition(
    manifest: BootstrapManifest, roots: ProtonRoots
) -> tuple[Path, Path, Path]:
    if not isinstance(manifest, BootstrapManifest):
        raise BootstrapError("GE-Proton manifest is invalid")
    if not isinstance(roots, ProtonRoots):
        raise BootstrapError("GE-Proton roots are invalid")
    if not isinstance(manifest.protonup, ProtonUpSpec) or not isinstance(
        manifest.ge, DownloadSpec
    ):
        raise BootstrapError("GE-Proton manifest is invalid")
    ge = manifest.ge
    if (
        manifest.protonup.version != _PROTONUP_VERSION
        or manifest.protonup.project_relative != _PROTONUP_PROJECT
        or ge.name != _BASE_RELEASE
        or ge.release != _BASE_RELEASE
        or ge.expected_root != _BASE_RELEASE
        or ge.filename != f"{_BASE_RELEASE}.tar.gz"
    ):
        raise BootstrapError("GE-Proton manifest is unsupported")
    repository = _absolute_path(roots.repo_root, "repository root")
    cache = _absolute_path(roots.cache_root, "ProtonUp cache")
    extraction = _absolute_path(roots.extraction_root, "GE extraction root")
    return repository, cache, extraction


def _protonup_environment(cache_root: Path) -> dict[str, str]:
    environment = {
        key: value
        for key in _ALLOWED_ENVIRONMENT
        if (value := os.environ.get(key)) is not None
    }
    environment["UV_CACHE_DIR"] = str(cache_root / ".uv-cache")
    return environment


def _extract_pinned_ge(path: Path, destination: Path, spec: DownloadSpec) -> Path:
    source = artifacts_module._open_archive_source(path, "GE archive")
    policy = artifacts_module.ArchivePolicy(
        spec.expected_root,
        artifacts_module._GE_MAX_MEMBERS,
        artifacts_module._GE_MAX_TOTAL_SIZE,
        True,
    )
    try:
        artifacts_module._hash_pinned_archive(source, spec)
        first_scan = artifacts_module._scan_archive(source, policy)
        result = artifacts_module._extract_and_publish(
            source, destination, policy, first_scan, None
        )
        if not isinstance(result, Path):
            raise BootstrapError("GE extraction returned an invalid result")
        artifacts_module._validate_archive_source(source)
        return result
    finally:
        os.close(source.fd)


def acquire_ge(
    manifest: BootstrapManifest,
    roots: ProtonRoots,
    runner: Callable[..., Any] = subprocess.run,
) -> Path:
    """Acquire once, independently verify, and safely extract pinned GE-Proton."""
    repository, cache, extraction_root = _validated_acquisition(manifest, roots)
    try:
        ensure_private_directory(cache)
        archive = cache / manifest.ge.filename
        if not verify_cached_file(archive, manifest.ge.size, manifest.ge.sha256):
            runner(
                protonup_argv(repository, cache),
                check=True,
                shell=False,
                env=_protonup_environment(cache),
                umask=0o077,
            )
            if not verify_cached_file(archive, manifest.ge.size, manifest.ge.sha256):
                raise BootstrapError("ProtonUp did not produce the pinned archive")
        destination = extraction_root / manifest.ge.expected_root
        return _extract_pinned_ge(archive, destination, manifest.ge)
    except BootstrapError:
        raise
    except Exception as error:
        raise BootstrapError("GE-Proton acquisition failed") from error


def prepare_fm_tree(extracted_ge: Path, stage: Path) -> ToolIdentity:
    """Copy verified GE and atomically assign the dedicated FM identity."""
    source = _absolute_path(extracted_ge, "extracted GE")
    destination = _absolute_path(stage, "compatibility-tool stage")
    if source.name != _BASE_RELEASE or source == destination:
        raise BootstrapError("extracted GE root is invalid")
    source_fd: int | None = None
    stage_fd: int | None = None
    try:
        source_fd = _open_directory(source, "extracted GE root")
        vdf, vdf_info = _read_regular_at(
            source_fd, _VDF, "compatibility-tool VDF", maximum_size=_MAX_VDF_SIZE
        )
        rewritten = _rewritten_vdf(vdf)
        _validate_managed_targets(source_fd, None)
        before = _tree_digest(source_fd)

        parent_fd = _open_directory(destination.parent, "stage parent")
        try:
            if stat.S_IMODE(os.fstat(parent_fd).st_mode) != 0o700:
                raise BootstrapError("stage parent is not private")
            try:
                os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise BootstrapError("compatibility-tool stage already exists")
        finally:
            os.close(parent_fd)

        shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copy2)
        stage_fd = _open_directory(destination, "compatibility-tool stage")
        os.fchmod(stage_fd, 0o700)
        os.fsync(stage_fd)
        copied = _tree_digest(stage_fd)
        after = _tree_digest(source_fd)
        if before != copied or before != after:
            raise BootstrapError("extracted GE changed while staging")

        _atomic_write_bytes(
            stage_fd,
            _VDF,
            rewritten,
            stat.S_IMODE(vdf_info.st_mode),
        )
        vdf_sha256 = sha256_bytes(rewritten)
        managed_tree_sha256 = _tree_digest(stage_fd, excludes=_TREE_EXCLUDES)
        initial = ToolIdentity(
            _FM_NAME,
            _BASE_RELEASE,
            vdf_sha256,
            managed_tree_sha256,
            None,
        )
        atomic_write_private_json(stage_fd, _MARKER, _marker_value(stage_fd, initial))
        identity = _read_identity(stage_fd)
        os.fsync(stage_fd)
        return identity
    except BootstrapError:
        raise
    except Exception as error:
        raise BootstrapError("compatibility-tool staging failed") from error
    finally:
        if stage_fd is not None:
            os.close(stage_fd)
        if source_fd is not None:
            os.close(source_fd)
        # Failed private stages are retained for diagnosis. Deleting by pathname
        # here could remove a same-name object installed concurrently.


def inspect_existing_fm(path: Path, manifest: BootstrapManifest) -> ToolDisposition:
    """Classify one existing dedicated tree without modifying it."""
    destination = _absolute_path(path, "compatibility-tool destination")
    if destination.name != _FM_NAME:
        raise BootstrapError("compatibility-tool destination is invalid")
    _validate_tool_manifest(manifest)
    parent_fd: int | None = None
    root_fd: int | None = None
    try:
        try:
            parent_fd = _open_directory(destination.parent, "compatibility-tool parent")
        except FileNotFoundError:
            return ToolDisposition.ABSENT
        try:
            root_fd = os.open(destination.name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            return ToolDisposition.ABSENT
        except OSError:
            raise BootstrapError("existing compatibility tool conflicts") from None
        root_info = os.fstat(root_fd)
        if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.getuid():
            raise BootstrapError("existing compatibility tool conflicts")
        vdf, _vdf_info = _read_regular_at(
            root_fd, _VDF, "compatibility-tool VDF", maximum_size=_MAX_VDF_SIZE
        )
        _vdf_identity(vdf, _FM_NAME)
        _validate_managed_targets(root_fd, manifest)
        try:
            marker_info = os.stat(_MARKER, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            marker_info = None
        if marker_info is not None:
            if not stat.S_ISREG(marker_info.st_mode):
                raise BootstrapError("existing compatibility tool conflicts")
            _read_identity(root_fd, require_recorded_targets=False)
        return ToolDisposition.ADOPTED
    except BootstrapError as error:
        if str(error) == "existing compatibility tool conflicts":
            raise
        raise BootstrapError("existing compatibility tool conflicts") from error
    except Exception as error:
        raise BootstrapError("existing compatibility tool conflicts") from error
    finally:
        if root_fd is not None:
            os.close(root_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def inspect_fm(path: Path, manifest: BootstrapManifest) -> ToolInspection:
    """Classify and bind the exact complete identity of a dedicated tool."""
    disposition = inspect_existing_fm(path, manifest)
    if disposition is ToolDisposition.ABSENT:
        return ToolInspection(disposition, None, None)
    destination = _absolute_path(path, "compatibility-tool destination")
    parent_fd: int | None = None
    root_fd: int | None = None
    try:
        parent_fd = _open_directory(destination.parent, "compatibility-tool parent")
        root_fd = os.open(destination.name, _DIRECTORY_FLAGS, dir_fd=parent_fd)

        def current_identity() -> ToolIdentity:
            _validate_managed_targets(root_fd, manifest)
            vdf, _vdf_info = _read_regular_at(
                root_fd, _VDF, "compatibility-tool VDF", maximum_size=_MAX_VDF_SIZE
            )
            _vdf_identity(vdf, _FM_NAME)
            try:
                marker_info = os.stat(
                    _MARKER, dir_fd=root_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                return ToolIdentity(
                    _FM_NAME,
                    _BASE_RELEASE,
                    sha256_bytes(vdf),
                    _tree_digest(root_fd, excludes=_TREE_EXCLUDES),
                    None,
                )
            if not stat.S_ISREG(marker_info.st_mode):
                raise BootstrapError("existing compatibility tool conflicts")
            return _read_identity(root_fd, require_recorded_targets=False)

        identity = current_identity()
        tree_sha256 = _tree_digest(root_fd)
        if (
            not _name_matches_directory(parent_fd, destination.name, root_fd)
            or current_identity() != identity
            or _tree_digest(root_fd) != tree_sha256
            or not _name_matches_directory(parent_fd, destination.name, root_fd)
        ):
            raise BootstrapError("existing compatibility tool conflicts")
        return ToolInspection(disposition, identity, tree_sha256)
    except BootstrapError as error:
        if str(error) == "existing compatibility tool conflicts":
            raise
        raise BootstrapError("existing compatibility tool conflicts") from error
    except Exception as error:
        raise BootstrapError("existing compatibility tool conflicts") from error
    finally:
        if root_fd is not None:
            os.close(root_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def publish_fm(stage: Path, destination: Path) -> PublishedToolRecord:
    """Publish a complete staged tree with no replacement."""
    source = _absolute_path(stage, "compatibility-tool stage")
    target = _absolute_path(destination, "compatibility-tool destination")
    if source == target:
        raise BootstrapError("compatibility-tool publication paths conflict")
    if target.name != _FM_NAME:
        raise BootstrapError("compatibility-tool destination is invalid")
    source_parent_fd: int | None = None
    target_parent_fd: int | None = None
    source_fd: int | None = None
    existing_fd: int | None = None
    created_record: PublishedToolRecord | None = None
    try:
        source_parent_fd = _open_directory(source.parent, "stage parent")
        target_parent_fd = _open_directory(target.parent, "compatibility-tool parent")
        source_fd = os.open(source.name, _DIRECTORY_FLAGS, dir_fd=source_parent_fd)
        source_info = os.fstat(source_fd)
        if not stat.S_ISDIR(source_info.st_mode) or source_info.st_uid != os.getuid():
            raise BootstrapError("compatibility-tool stage is unsafe")
        identity = _read_identity(source_fd)
        published_digest = _tree_digest(source_fd)

        try:
            existing_fd = os.open(
                target.name, _DIRECTORY_FLAGS, dir_fd=target_parent_fd
            )
        except FileNotFoundError:
            existing_fd = None
        except OSError:
            raise BootstrapError("publication destination conflicts") from None
        if existing_fd is not None:
            try:
                existing_identity = _read_identity(existing_fd)
                existing_digest = _tree_digest(existing_fd)
            except BootstrapError:
                raise BootstrapError("publication destination conflicts") from None
            if existing_identity != identity or existing_digest != published_digest:
                raise BootstrapError("publication destination conflicts")
            existing_info = os.fstat(existing_fd)
            return PublishedToolRecord(
                target,
                ToolDisposition.ADOPTED,
                existing_identity,
                existing_info.st_dev,
                existing_info.st_ino,
                existing_digest,
            )

        if (
            not _name_matches_directory(source_parent_fd, source.name, source_fd)
            or _read_identity(source_fd) != identity
            or _tree_digest(source_fd) != published_digest
        ):
            raise BootstrapError("compatibility-tool publication verification failed")
        try:
            rename_noreplace(
                source_parent_fd,
                source.name,
                target_parent_fd,
                target.name,
            )
        except FileExistsError:
            raise BootstrapError("publication destination conflicts") from None
        created_record = PublishedToolRecord(
            target,
            ToolDisposition.CREATED,
            identity,
            source_info.st_dev,
            source_info.st_ino,
            published_digest,
        )
        os.fsync(target_parent_fd)
        named_fd: int | None = None
        try:
            try:
                named_fd = os.open(
                    target.name, _DIRECTORY_FLAGS, dir_fd=target_parent_fd
                )
                named_info = os.fstat(named_fd)
                held_info = os.fstat(source_fd)
                if (
                    (named_info.st_dev, named_info.st_ino)
                    != (held_info.st_dev, held_info.st_ino)
                    or _read_identity(source_fd) != identity
                    or _tree_digest(source_fd) != published_digest
                ):
                    raise BootstrapError(
                        "compatibility-tool publication verification failed"
                    )
            except Exception as verification_error:
                created_record = None
                try:
                    _quarantine_publication_mismatches(target_parent_fd, target.name)
                except Exception as recovery_error:
                    raise BootstrapError(
                        "compatibility-tool publication verification failed; "
                        "recovery failed"
                    ) from recovery_error
                raise BootstrapError(
                    "compatibility-tool publication verification failed"
                ) from verification_error
        finally:
            if named_fd is not None:
                os.close(named_fd)
        return created_record
    except BootstrapError:
        if created_record is not None:
            rollback_published_fm(created_record)
        raise
    except Exception as error:
        if created_record is not None:
            try:
                rollback_published_fm(created_record)
            except BootstrapError as rollback_error:
                raise BootstrapError(
                    f"compatibility-tool publication failed; rollback failed: {rollback_error}"
                ) from error
        raise BootstrapError("compatibility-tool publication failed") from error
    finally:
        if existing_fd is not None:
            os.close(existing_fd)
        if source_fd is not None:
            os.close(source_fd)
        if target_parent_fd is not None:
            os.close(target_parent_fd)
        if source_parent_fd is not None:
            os.close(source_parent_fd)


def rollback_published_fm(record: PublishedToolRecord) -> ToolDisposition:
    """Remove only the unchanged tree created by this publication record."""
    if not isinstance(record, PublishedToolRecord):
        raise BootstrapError("compatibility-tool publication record is invalid")
    if record.disposition is ToolDisposition.ADOPTED:
        return ToolDisposition.ADOPTED
    if record.disposition is not ToolDisposition.CREATED:
        raise BootstrapError("compatibility-tool publication record is invalid")
    destination = _absolute_path(record.destination, "compatibility-tool destination")
    parent_fd: int | None = None
    root_fd: int | None = None
    recovery_name: str | None = None
    try:
        try:
            parent_fd = _open_directory(destination.parent, "compatibility-tool parent")
        except FileNotFoundError:
            return ToolDisposition.ABSENT
        try:
            root_fd = os.open(destination.name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            return ToolDisposition.ABSENT
        except OSError:
            return ToolDisposition.CONFLICT
        info = os.fstat(root_fd)
        try:
            unchanged = (
                (info.st_dev, info.st_ino) == (record.root_device, record.root_inode)
                and _read_identity(root_fd) == record.identity
                and _tree_digest(root_fd) == record.published_tree_sha256
            )
        except BootstrapError:
            unchanged = False
        if not unchanged:
            return ToolDisposition.CONFLICT

        for _ in range(100):
            candidate = f".{destination.name}.{secrets.token_hex(12)}.rollback"
            try:
                rename_noreplace(parent_fd, destination.name, parent_fd, candidate)
            except FileExistsError:
                continue
            except FileNotFoundError:
                return ToolDisposition.ABSENT
            recovery_name = candidate
            break
        else:
            raise BootstrapError("cannot reserve compatibility-tool rollback name")
        os.fsync(parent_fd)

        try:
            quarantined_unchanged = (
                _name_matches_directory(parent_fd, recovery_name, root_fd)
                and _read_identity(root_fd) == record.identity
                and _tree_digest(root_fd) == record.published_tree_sha256
            )
        except BootstrapError:
            quarantined_unchanged = False
        if not quarantined_unchanged:
            _restore_recovery(parent_fd, recovery_name, destination.name)
            return ToolDisposition.CONFLICT
        _remove_directory_contents(root_fd)
        if not _name_matches_directory(parent_fd, recovery_name, root_fd):
            return ToolDisposition.CONFLICT
        os.rmdir(recovery_name, dir_fd=parent_fd)
        recovery_name = None
        os.fsync(parent_fd)
        return ToolDisposition.ABSENT
    except BootstrapError:
        raise
    except Exception as error:
        raise BootstrapError("compatibility-tool rollback failed") from error
    finally:
        if root_fd is not None:
            os.close(root_fd)
        if parent_fd is not None:
            os.close(parent_fd)
