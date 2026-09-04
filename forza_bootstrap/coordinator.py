# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Two-phase bootstrap coordination; this module currently implements Prepare."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TextIO

from .artifacts import VerifiedBundle, download_once, extract_bundle
from .container_build import check_docker
from .model import (
    BootstrapError,
    BootstrapManifest,
    canonical_json,
    plan_digest,
)
from .proton import (
    ProtonRoots,
    PublishedToolRecord,
    ToolDisposition,
    ToolIdentity,
    acquire_ge,
    inspect_fm,
    prepare_fm_tree,
    publish_fm,
)
from .safeio import atomic_write_private_json, ensure_private_directory, open_owned_root
from .snapshot import (
    Snapshot,
    SnapshotScope,
    SnapshotTarget,
    capture_snapshot,
    write_snapshot,
)
from .state import BootstrapPhase, BootstrapState, create_transaction, transition

_APP_ID = "2440510"
_FM_NAME = "GE-Proton11-3-FM"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_MIN_PREPARE_BYTES = 2 * 1024 * 1024 * 1024
_TERMINAL_RUNTIME_STATES = frozenset({"accepted", "rolled_back", "runtime_restored"})


@dataclass(frozen=True)
class ResolvedHost:
    os_id: str
    architecture: str
    user_root: Path
    steam_root: Path
    app_manifest: Path
    compatibility_tools: Path
    state_root: Path
    cache_root: Path
    runtime_dir: Path


_Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class HostProbe:
    """Replace only OS discovery boundaries in synthetic tests."""

    os_release: Path = Path("/etc/os-release")
    machine: Callable[[], str] = platform.machine
    runner: _Runner = subprocess.run
    which: Callable[[str], str | None] = shutil.which
    disk_usage: Callable[[Path], shutil._ntuple_diskusage] = shutil.disk_usage
    proc_root: Path = Path("/proc")


@dataclass(frozen=True)
class _DirectoryProbe:
    path: Path
    info: os.stat_result
    exists: bool


@dataclass(frozen=True)
class PrepareInspection:
    root_ids: Mapping[str, str]
    filesystem_ids: Mapping[str, str]
    available_bytes: int
    required_bytes: int
    tool_disposition: ToolDisposition
    existing_tool_identity: ToolIdentity | None
    existing_tool_tree_sha256: str | None
    docker_available: bool


@dataclass(frozen=True)
class PrepareOperations:
    preflight: Callable[[PrepareContext], PrepareInspection]
    capture_before: Callable[[PrepareContext, str], Snapshot]
    acquire_ge: Callable[[PrepareContext], Path]
    acquire_bundle: Callable[[PrepareContext], VerifiedBundle]
    publish_fm: Callable[[PrepareContext, Path], PublishedToolRecord]
    locked_preflight: Callable[[PrepareContext, int], PrepareInspection] | None = None
    event: Callable[[str], None] = lambda _event: None
    boundary: Callable[[str], None] = lambda _boundary: None


@dataclass(frozen=True)
class PrepareContext:
    manifest: BootstrapManifest
    host: ResolvedHost
    repository_root: Path
    acquisition_mode: Literal["download", "local", "source"] | str = "download"
    bundle_path: Path | None = None
    output: TextIO = sys.stdout
    operations: PrepareOperations | None = None
    probe: HostProbe = field(default_factory=HostProbe)


def _read_os_id(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise BootstrapError("cannot identify Arch Linux host") from None
    values: list[str] = []
    for line in lines:
        if not line.startswith("ID="):
            continue
        value = line[3:].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values.append(value)
    if len(values) != 1 or not values[0]:
        raise BootstrapError("cannot identify Arch Linux host")
    return values[0]


def _absolute_environment_path(
    env: Mapping[str, str], name: str, *, default: Path | None = None
) -> Path:
    value = env.get(name)
    if value is None and default is not None:
        result = default
    elif not isinstance(value, str) or not value:
        raise BootstrapError(f"{name} is required")
    else:
        try:
            result = Path(value)
        except (TypeError, ValueError):
            raise BootstrapError(f"{name} is invalid") from None
    if not result.is_absolute():
        raise BootstrapError(f"{name} must be an absolute path")
    return result


def _owned_directory(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise BootstrapError(f"{label} is missing") from None
    except OSError:
        raise BootstrapError(f"{label} is unavailable") from None
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
    ):
        raise BootstrapError(f"{label} has unsafe type or ownership")


def _owned_regular(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise BootstrapError(f"{label} is missing") from None
    except OSError:
        raise BootstrapError(f"{label} is unavailable") from None
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
    ):
        raise BootstrapError(f"{label} has unsafe type or ownership")


def _inspect_directory_path(
    path: Path, label: str, *, private_if_exists: bool
) -> _DirectoryProbe:
    if (
        not path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        raise BootstrapError(f"{label} has unsafe path")
    descriptor = os.open("/", _DIRECTORY)
    current = Path("/")
    try:
        for part in path.parts[1:]:
            try:
                next_descriptor = os.open(part, _DIRECTORY, dir_fd=descriptor)
            except FileNotFoundError:
                info = os.fstat(descriptor)
                if info.st_uid != os.getuid():
                    raise BootstrapError(f"{label} has unsafe ownership") from None
                return _DirectoryProbe(current, info, False)
            except OSError:
                raise BootstrapError(f"{label} has unsafe path") from None
            os.close(descriptor)
            descriptor = next_descriptor
            current /= part
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid():
            raise BootstrapError(f"{label} has unsafe ownership")
        if private_if_exists and stat.S_IMODE(info.st_mode) != 0o700:
            raise BootstrapError(f"{label} has unsafe mode")
        return _DirectoryProbe(current, info, True)
    finally:
        os.close(descriptor)


def _stat_generation(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _open_owned_directory(path: Path, label: str) -> int:
    if (
        not path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        raise BootstrapError(f"{label} is invalid")
    descriptor = os.open("/", _DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_descriptor = os.open(part, _DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid():
            raise OSError
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _read_owned_regular(
    path: Path, label: str, maximum_size: int, *, missing_ok: bool = False
) -> bytes | None:
    parent_fd: int | None = None
    file_fd: int | None = None
    try:
        try:
            parent_fd = _open_owned_directory(path.parent, label)
            file_fd = os.open(
                path.name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            if missing_ok:
                return None
            raise
        before = os.fstat(file_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or before.st_size > maximum_size
        ):
            raise OSError
        chunks: list[bytes] = []
        remaining = maximum_size + 1
        while remaining:
            block = os.read(file_fd, min(64 * 1024, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        data = b"".join(chunks)
        after = os.fstat(file_fd)
        bound = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            len(data) > maximum_size
            or _stat_generation(before) != _stat_generation(after)
            or _stat_generation(after) != _stat_generation(bound)
        ):
            raise OSError
        return data
    except BootstrapError:
        raise
    except OSError:
        raise BootstrapError(f"{label} is invalid") from None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def _validate_app_manifest_data(data: bytes) -> None:
    app_ids = re.findall(rb'"appid"\s+"([^"\r\n]+)"', data)
    if len(app_ids) != 1 or app_ids[0] != _APP_ID.encode("ascii"):
        raise BootstrapError(f"Steam AppID {_APP_ID} manifest is invalid")


def _validate_app_manifest(path: Path) -> None:
    data = _read_owned_regular(path, f"Steam AppID {_APP_ID} manifest", 1024 * 1024)
    assert data is not None
    _validate_app_manifest_data(data)


def _steam_library_paths(steam_root: Path) -> tuple[Path, ...]:
    """Return native library roots from Valve's small VDF without writing it."""
    paths = [steam_root]
    file = steam_root / "steamapps/libraryfolders.vdf"
    data = _read_owned_regular(
        file, "Steam library manifest", 4 * 1024 * 1024, missing_ok=True
    )
    if data is None:
        return tuple(paths)
    if b"\x00" in data:
        raise BootstrapError("Steam library manifest is invalid")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise BootstrapError("Steam library manifest is invalid") from None
    for raw in re.findall(r'"path"\s+"((?:[^"\\]|\\.)*)"', text):
        value = raw.replace("\\\\", "\\")
        candidate = Path(value)
        if candidate.is_absolute() and candidate not in paths:
            paths.append(candidate)
    return tuple(paths)


def resolve_supported_host(
    env: Mapping[str, str], probe: HostProbe | None = None
) -> ResolvedHost:
    """Resolve the supported Arch/x86_64/native-Steam layout without writes."""
    if not isinstance(env, Mapping):
        raise BootstrapError("host environment is invalid")
    selected_probe = probe or HostProbe()
    os_id = _read_os_id(selected_probe.os_release)
    if os_id != "arch":
        raise BootstrapError("this bootstrap supports Arch Linux only")
    architecture = selected_probe.machine()
    if architecture != "x86_64":
        raise BootstrapError("this bootstrap supports x86_64 only")

    user_root = _absolute_environment_path(env, "HOME")
    runtime_dir = _absolute_environment_path(env, "XDG_RUNTIME_DIR")
    _owned_directory(user_root, "user root")
    _owned_directory(runtime_dir, "runtime directory")
    data_root = _absolute_environment_path(
        env, "XDG_DATA_HOME", default=user_root / ".local/share"
    )
    state_home = _absolute_environment_path(
        env, "XDG_STATE_HOME", default=user_root / ".local/state"
    )
    cache_home = _absolute_environment_path(
        env, "XDG_CACHE_HOME", default=user_root / ".cache"
    )
    steam_root = data_root / "Steam"
    if not steam_root.exists():
        flatpak = user_root / ".var/app/com.valvesoftware.Steam/.local/share/Steam"
        if flatpak.exists():
            raise BootstrapError(
                "Flatpak Steam is not supported; native Steam is required"
            )
        raise BootstrapError("native Steam root is missing")
    _owned_directory(steam_root, "native Steam root")

    app_manifest: Path | None = None
    for library in _steam_library_paths(steam_root):
        _owned_directory(library, "Steam library root")
        candidate = library / f"steamapps/appmanifest_{_APP_ID}.acf"
        app_data = _read_owned_regular(
            candidate,
            f"Steam AppID {_APP_ID} manifest",
            1024 * 1024,
            missing_ok=True,
        )
        if app_data is None:
            continue
        if library != steam_root:
            raise BootstrapError(
                "AppID 2440510 in additional Steam libraries is not supported"
            )
        _validate_app_manifest_data(app_data)
        if app_manifest is not None:
            raise BootstrapError(f"multiple Steam AppID {_APP_ID} manifests")
        app_manifest = candidate
    if app_manifest is None:
        raise BootstrapError(f"Steam AppID {_APP_ID} manifest is missing")
    return ResolvedHost(
        os_id,
        architecture,
        user_root,
        steam_root,
        app_manifest,
        steam_root / "compatibilitytools.d",
        state_home / "forza-motorsport-linux/bootstrap",
        cache_home / "forza-motorsport-linux/bootstrap",
        runtime_dir,
    )


def _validate_context(context: PrepareContext) -> None:
    if not isinstance(context, PrepareContext):
        raise BootstrapError("Prepare context is invalid")
    if not isinstance(context.manifest, BootstrapManifest):
        raise BootstrapError("Prepare manifest is invalid")
    if not isinstance(context.host, ResolvedHost):
        raise BootstrapError("resolved host is invalid")
    if context.manifest.host != (
        context.host.os_id,
        context.host.architecture,
        "native-steam",
    ):
        raise BootstrapError("resolved host disagrees with bootstrap manifest")
    if context.manifest.app_id != _APP_ID:
        raise BootstrapError("Prepare manifest has an unsupported AppID")
    if context.acquisition_mode not in {"download", "local", "source"}:
        raise BootstrapError("bundle acquisition mode is invalid")
    if context.acquisition_mode == "local" and context.bundle_path is None:
        raise BootstrapError("local bundle path is required")
    if context.acquisition_mode != "local" and context.bundle_path is not None:
        raise BootstrapError(
            "local bundle and source/download mode are mutually exclusive"
        )
    if context.bundle_path is not None and not context.bundle_path.is_absolute():
        raise BootstrapError("local bundle path must be absolute")
    if not context.repository_root.is_absolute():
        raise BootstrapError("repository root must be absolute")


def _root_id(path: Path, label: str) -> str:
    current = path
    while not current.exists():
        if current.parent == current:
            raise BootstrapError(f"{label} has no existing parent")
        current = current.parent
    _owned_directory(current, label)
    info = current.stat()
    return f"{info.st_dev}:{info.st_ino}"


def _probe_launcher_lock(host: ResolvedHost) -> None:
    lock = host.runtime_dir / "forza-linux.lock"
    try:
        fd = os.open(lock, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    except OSError:
        raise BootstrapError("launcher lock is unsafe") from None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise BootstrapError("launcher lock is unsafe")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BootstrapError("launcher lock is held by another process") from None
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(fd)


def _validate_held_launcher_lock(host: ResolvedHost, lease_fd: int) -> None:
    linked_fd: int | None = None
    try:
        lease_info = os.fstat(lease_fd)
        runtime_fd = os.open(host.runtime_dir, _DIRECTORY)
        try:
            linked_fd = os.open(
                "forza-linux.lock",
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=runtime_fd,
            )
        finally:
            os.close(runtime_fd)
        linked_info = os.fstat(linked_fd)
        if (
            not stat.S_ISREG(lease_info.st_mode)
            or lease_info.st_uid != os.getuid()
            or stat.S_IMODE(lease_info.st_mode) != 0o600
            or (lease_info.st_dev, lease_info.st_ino)
            != (linked_info.st_dev, linked_info.st_ino)
        ):
            raise OSError
        fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        raise BootstrapError("launcher lock changed while Prepare held it") from None
    finally:
        if linked_fd is not None:
            os.close(linked_fd)


def _probe_xodus_service(probe: HostProbe) -> None:
    try:
        result = probe.runner(
            ("systemctl", "--user", "is-active", "--quiet", "xodus-forza.service"),
            check=False,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, OSError):
        raise BootstrapError("cannot prove xodus-forza.service is inactive") from None
    if result.returncode == 0:
        raise BootstrapError("xodus-forza.service is active")
    if result.returncode not in {3, 4}:
        raise BootstrapError("cannot prove xodus-forza.service is inactive")


def _probe_forza_processes(probe: HostProbe) -> None:
    patterns = (
        b"forza_steamworks_release_final.exe",
        b"forza_gaming.desktop.x64_release_final.exe",
    )
    try:
        entries = tuple(probe.proc_root.glob("[0-9]*/cmdline"))
    except OSError:
        raise BootstrapError(
            "cannot prove Forza Motorsport processes are inactive"
        ) from None
    for entry in entries:
        try:
            command = entry.read_bytes().lower()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        except OSError:
            raise BootstrapError(
                "cannot prove Forza Motorsport processes are inactive"
            ) from None
        if any(pattern in command for pattern in patterns):
            raise BootstrapError("Forza Motorsport process is active")


def _probe_runtime_children(host: ResolvedHost) -> None:
    root = host.user_root / ".local/state/forza-motorsport-linux/runtime-transactions"
    try:
        root_fd = os.open(root, _DIRECTORY)
    except FileNotFoundError:
        return
    except OSError:
        raise BootstrapError("cannot inspect runtime child transactions") from None

    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    try:
        try:
            root_info = os.fstat(root_fd)
            entries = tuple(os.listdir(root_fd))
        except OSError:
            raise BootstrapError("cannot inspect runtime child transactions") from None
        if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.getuid():
            raise BootstrapError("cannot inspect runtime child transactions")

        for name in entries:
            if name == "artifact-evidence.json":
                continue
            transaction_fd: int | None = None
            journal_fd: int | None = None
            try:
                transaction_fd = os.open(name, _DIRECTORY, dir_fd=root_fd)
                transaction_info = os.fstat(transaction_fd)
                if transaction_info.st_uid != os.getuid():
                    raise OSError
                journal_fd = os.open(
                    "journal.json",
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=transaction_fd,
                )
                before = os.fstat(journal_fd)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_uid != os.getuid()
                    or before.st_nlink != 1
                    or before.st_size > 1024 * 1024
                ):
                    raise OSError
                data = os.read(journal_fd, 1024 * 1024 + 1)
                after = os.fstat(journal_fd)
                bound = os.stat(
                    "journal.json", dir_fd=transaction_fd, follow_symlinks=False
                )
                if (
                    len(data) > 1024 * 1024
                    or identity(before) != identity(after)
                    or identity(after) != identity(bound)
                ):
                    raise OSError
                payload = json.loads(data)
            except (OSError, json.JSONDecodeError, UnicodeError):
                raise BootstrapError(
                    "unfinished runtime child transaction is unsafe"
                ) from None
            finally:
                if journal_fd is not None:
                    os.close(journal_fd)
                if transaction_fd is not None:
                    os.close(transaction_fd)
            if (
                not isinstance(payload, dict)
                or payload.get("state") not in _TERMINAL_RUNTIME_STATES
            ):
                raise BootstrapError("unfinished runtime child transaction exists")
    finally:
        os.close(root_fd)


def _probe_existing_bootstrap_state(
    host: ResolvedHost, state_root: _DirectoryProbe
) -> None:
    if not state_root.exists:
        return
    descriptor: int | None = None
    try:
        descriptor = _open_owned_directory(host.state_root, "bootstrap state root")
        info = os.fstat(descriptor)
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise OSError
        entries = tuple(os.listdir(descriptor))
    except (OSError, BootstrapError):
        raise BootstrapError("bootstrap state root is unsafe") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    significant = [
        entry for entry in entries if entry not in {".bootstrap.lock", ".journal.lock"}
    ]
    if significant:
        raise BootstrapError("unfinished bootstrap transaction requires resume support")


def _read_local_bundle(path: Path, expected_size: int, expected_sha256: str) -> None:
    _owned_regular(path, "local bundle")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise BootstrapError("local bundle is unavailable") from None
    try:
        before = os.fstat(fd)
        digest = hashlib.sha256()
        while block := os.read(fd, 1024 * 1024):
            digest.update(block)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if (
        before.st_size != expected_size
        or digest.hexdigest() != expected_sha256
        or identity(before) != identity(after)
    ):
        raise BootstrapError("local bundle does not match the pinned manifest")


def inspect_prepare(
    context: PrepareContext, *, held_launcher_lock: int | None = None
) -> PrepareInspection:
    """Complete every read-only Prepare preflight before any private write."""
    _validate_context(context)
    host = context.host
    _owned_directory(host.user_root, "user root")
    _owned_directory(host.steam_root, "native Steam root")
    _validate_app_manifest(host.app_manifest)
    _owned_directory(host.runtime_dir, "runtime directory")
    state_root = _inspect_directory_path(
        host.state_root, "state root", private_if_exists=True
    )
    cache_root = _inspect_directory_path(
        host.cache_root, "cache root", private_if_exists=True
    )
    staging = _inspect_directory_path(
        host.cache_root / "staging", "cache staging", private_if_exists=True
    )
    destination_root = _inspect_directory_path(
        host.compatibility_tools,
        "compatibility-tool destination",
        private_if_exists=False,
    )
    if staging.info.st_dev != destination_root.info.st_dev:
        raise BootstrapError(
            "cache staging and compatibility-tool destination must share the same filesystem"
        )
    _probe_existing_bootstrap_state(host, state_root)
    if held_launcher_lock is None:
        _probe_launcher_lock(host)
    else:
        _validate_held_launcher_lock(host, held_launcher_lock)
    _probe_xodus_service(context.probe)
    _probe_forza_processes(context.probe)
    _probe_runtime_children(host)
    for command in ("uv", "zstd", "systemctl"):
        if context.probe.which(command) is None:
            raise BootstrapError(f"required host command is missing: {command}")
    docker_available = context.probe.which("docker") is not None
    if context.acquisition_mode == "source":
        if not docker_available:
            raise BootstrapError("Docker is required only for source mode")
        check_docker(context.probe.runner)
    elif context.acquisition_mode == "local":
        assert context.bundle_path is not None
        _read_local_bundle(
            context.bundle_path,
            context.manifest.bundle.size,
            context.manifest.bundle.sha256,
        )

    destination = host.compatibility_tools / _FM_NAME
    tool = inspect_fm(destination, context.manifest)
    required = max(
        _MIN_PREPARE_BYTES,
        3 * (context.manifest.ge.size + context.manifest.bundle.size),
    )
    try:
        staging_available = context.probe.disk_usage(staging.path).free
        destination_available = context.probe.disk_usage(destination_root.path).free
    except OSError:
        raise BootstrapError("available disk space cannot be determined") from None
    available = min(staging_available, destination_available)
    if available < required:
        raise BootstrapError("insufficient disk space for Prepare")
    return PrepareInspection(
        root_ids={
            "user": _root_id(host.user_root, "user root"),
            "steam": _root_id(host.steam_root, "native Steam root"),
            "cache": f"{cache_root.info.st_dev}:{cache_root.info.st_ino}",
            "state": f"{state_root.info.st_dev}:{state_root.info.st_ino}",
            "runtime": _root_id(host.runtime_dir, "runtime directory"),
        },
        filesystem_ids={
            "cache-staging": str(staging.info.st_dev),
            "compatibility-destination": str(destination_root.info.st_dev),
        },
        available_bytes=available,
        required_bytes=required,
        tool_disposition=tool.disposition,
        existing_tool_identity=tool.identity,
        existing_tool_tree_sha256=tool.tree_sha256,
        docker_available=docker_available,
    )


def _identity_value(identity: ToolIdentity) -> dict[str, object]:
    if (
        not isinstance(identity, ToolIdentity)
        or identity.name != _FM_NAME
        or identity.base_release != "GE-Proton11-3"
        or _SHA256.fullmatch(identity.vdf_sha256) is None
        or _SHA256.fullmatch(identity.managed_tree_sha256) is None
        or (
            identity.marker_sha256 is not None
            and _SHA256.fullmatch(identity.marker_sha256) is None
        )
    ):
        raise BootstrapError("compatibility-tool identity is invalid")
    return {
        "name": identity.name,
        "base_release": identity.base_release,
        "vdf_sha256": identity.vdf_sha256,
        "managed_tree_sha256": identity.managed_tree_sha256,
        "marker_sha256": identity.marker_sha256,
    }


def build_prepare_plan(
    context: PrepareContext, inspection: PrepareInspection | None = None
) -> dict[str, Any]:
    """Build the canonical, path-redacted Phase A plan."""
    _validate_context(context)
    facts = inspection if inspection is not None else inspect_prepare(context)
    if not isinstance(facts, PrepareInspection):
        raise BootstrapError("Prepare inspection is invalid")
    required_root_ids = {"user", "steam", "cache", "state", "runtime"}
    if set(facts.root_ids) != required_root_ids or any(
        not isinstance(value, str)
        or re.fullmatch(r"(?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*)", value) is None
        for value in facts.root_ids.values()
    ):
        raise BootstrapError("Prepare root identities are invalid")
    required_filesystem_ids = {"cache-staging", "compatibility-destination"}
    if set(facts.filesystem_ids) != required_filesystem_ids or any(
        not isinstance(value, str) or re.fullmatch(r"(?:0|[1-9][0-9]*)", value) is None
        for value in facts.filesystem_ids.values()
    ):
        raise BootstrapError("Prepare filesystem identities are invalid")
    if len(set(facts.filesystem_ids.values())) != 1:
        raise BootstrapError(
            "cache staging and compatibility-tool destination must share the same filesystem"
        )
    if (
        type(facts.available_bytes) is not int
        or type(facts.required_bytes) is not int
        or facts.available_bytes < facts.required_bytes
    ):
        raise BootstrapError("insufficient disk space for Prepare")
    if facts.tool_disposition not in {ToolDisposition.ABSENT, ToolDisposition.ADOPTED}:
        raise BootstrapError("compatibility-tool destination conflicts")
    if context.acquisition_mode == "source" and not facts.docker_available:
        raise BootstrapError("Docker is required only for source mode")
    if facts.tool_disposition is ToolDisposition.ADOPTED:
        if not isinstance(facts.existing_tool_identity, ToolIdentity):
            raise BootstrapError("adopted compatibility-tool identity is missing")
        if (
            not isinstance(facts.existing_tool_tree_sha256, str)
            or _SHA256.fullmatch(facts.existing_tool_tree_sha256) is None
        ):
            raise BootstrapError("adopted compatibility-tool tree identity is missing")
        before: dict[str, object] = {
            "disposition": "adopted",
            "identity": _identity_value(facts.existing_tool_identity),
            "tree_sha256": facts.existing_tool_tree_sha256,
        }
        after: dict[str, object] = {
            **_identity_value(facts.existing_tool_identity),
            "tree_sha256": facts.existing_tool_tree_sha256,
        }
    else:
        if (
            facts.existing_tool_identity is not None
            or facts.existing_tool_tree_sha256 is not None
        ):
            raise BootstrapError("absent compatibility-tool has an identity")
        before = {"disposition": "absent"}
        after = {
            "name": _FM_NAME,
            "base_release": context.manifest.ge.release,
            "source_archive_sha256": context.manifest.ge.sha256,
            "manifest_sha256": context.manifest.sha256,
        }
    bundle_mode = {
        "download": "verified-download",
        "local": "verified-local",
        "source": "rootless-source-build",
    }[context.acquisition_mode]
    bundle_cache_relative = {
        "download": f"downloads/{context.manifest.bundle.filename}",
        "local": None,
        "source": f"source-output/{context.manifest.bundle.filename}",
    }[context.acquisition_mode]
    local_input = (
        {"path_sha256": hashlib.sha256(os.fsencode(context.bundle_path)).hexdigest()}
        if context.bundle_path is not None
        else None
    )
    root_paths = {
        "user": context.host.user_root,
        "steam": context.host.steam_root,
        "cache": context.host.cache_root,
        "state": context.host.state_root,
        "runtime": context.host.runtime_dir,
    }
    return {
        "schema": 1,
        "phase": "prepare",
        "app_id": context.manifest.app_id,
        "manifest_sha256": context.manifest.sha256,
        "host": {
            "os_id": context.host.os_id,
            "architecture": context.host.architecture,
            "profile": "native-steam",
        },
        "roots": {
            name: {
                "identity": facts.root_ids[name],
                "path_sha256": hashlib.sha256(
                    os.fsencode(root_paths[name])
                ).hexdigest(),
            }
            for name in sorted(facts.root_ids)
        },
        "disk": {
            "required_bytes": facts.required_bytes,
            "filesystems": {
                name: {"device": facts.filesystem_ids[name]}
                for name in sorted(facts.filesystem_ids)
            },
        },
        "acquisition": {
            "ge": {
                "method": "protonup-0.1.5-frozen",
                "release": context.manifest.ge.release,
                "cache": {
                    "root": "cache",
                    "relative": f"downloads/{context.manifest.ge.filename}",
                },
                "size": context.manifest.ge.size,
                "sha256": context.manifest.ge.sha256,
            },
            "bundle": {
                "method": bundle_mode,
                "cache": {
                    "root": "cache",
                    "relative": bundle_cache_relative,
                },
                "local_input": local_input,
                "size": context.manifest.bundle.size,
                "sha256": context.manifest.bundle.sha256,
                "builder_image": (
                    context.manifest.builder.image
                    if context.acquisition_mode == "source"
                    else None
                ),
            },
        },
        "destination": {
            "root": "steam",
            "relative": f"compatibilitytools.d/{_FM_NAME}",
            "before": before,
            "after": after,
        },
    }


def _snapshot_scope(context: PrepareContext, transaction_id: str) -> SnapshotScope:
    targets = [
        SnapshotTarget(
            "compat/tool-marker",
            "steam",
            f"compatibilitytools.d/{_FM_NAME}/.forza-bootstrap.json",
        ),
        SnapshotTarget(
            "compat/controller",
            "steam",
            f"compatibilitytools.d/{_FM_NAME}/files/lib/wine/x86_64-windows/windows.gaming.input.dll",
        ),
        SnapshotTarget(
            "compat/mountmgr",
            "steam",
            f"compatibilitytools.d/{_FM_NAME}/files/lib/wine/x86_64-windows/mountmgr.sys",
        ),
        SnapshotTarget(
            "compat/xgameruntime-pe64",
            "steam",
            f"compatibilitytools.d/{_FM_NAME}/files/lib/wine/x86_64-windows/xgameruntime.dll",
        ),
        SnapshotTarget(
            "compat/xgameruntime-unix64",
            "steam",
            f"compatibilitytools.d/{_FM_NAME}/files/lib/wine/x86_64-unix/xgameruntime.so",
        ),
        SnapshotTarget(
            "prefix/controller",
            "steam",
            f"steamapps/compatdata/{_APP_ID}/pfx/drive_c/windows/system32/windows.gaming.input.dll",
        ),
        SnapshotTarget(
            "prefix/mountmgr",
            "steam",
            f"steamapps/compatdata/{_APP_ID}/pfx/drive_c/windows/system32/drivers/mountmgr.sys",
        ),
        SnapshotTarget(
            "prefix/threading",
            "steam",
            f"steamapps/compatdata/{_APP_ID}/pfx/drive_c/windows/system32/xgameruntime.dll.threading",
            True,
        ),
    ]
    user_paths = (
        ".local/bin/forza-linux",
        ".local/bin/forza-doctor",
        ".local/libexec/forza-motorsport-linux/patch-known-build",
        ".local/share/forza-motorsport-linux/supported-builds.toml",
        ".local/libexec/xodus-forza/xodus-service",
        ".local/libexec/xodus-forza/xodus-cli",
        ".local/libexec/xodus-forza/xodus-overlay",
        ".config/systemd/user/xodus-forza.service",
        ".local/state/forza-motorsport-linux/install-manifest",
    )
    targets.extend(SnapshotTarget(f"user/{path}", "user", path) for path in user_paths)
    return SnapshotScope(
        1,
        transaction_id,
        context.manifest.sha256,
        tuple(sorted(targets, key=lambda item: item.logical_path)),
    )


def _capture_before(context: PrepareContext, transaction_id: str) -> Snapshot:
    return capture_snapshot(
        _snapshot_scope(context, transaction_id),
        {"steam": context.host.steam_root, "user": context.host.user_root},
    )


def _ensure_cache_subdirectory(root: Path, relative: str) -> Path:
    ensure_private_directory(root)
    current = root
    for part in relative.split("/"):
        current = current / part
        ensure_private_directory(current)
    return current


def _acquire_ge(context: PrepareContext) -> Path:
    downloads = _ensure_cache_subdirectory(context.host.cache_root, "downloads")
    extracted = _ensure_cache_subdirectory(context.host.cache_root, "ge")
    return acquire_ge(
        context.manifest,
        ProtonRoots(context.repository_root, downloads, extracted),
    )


def _acquire_bundle(context: PrepareContext) -> VerifiedBundle:
    downloads = _ensure_cache_subdirectory(context.host.cache_root, "downloads")
    extraction = _ensure_cache_subdirectory(context.host.cache_root, "bundle")
    if context.acquisition_mode == "download":
        archive = download_once(context.manifest.bundle, downloads)
    elif context.acquisition_mode == "local":
        assert context.bundle_path is not None
        archive = context.bundle_path
    else:
        output = _ensure_cache_subdirectory(context.host.cache_root, "source-output")
        if any(output.iterdir()):
            candidate = output / context.manifest.bundle.filename
            _read_local_bundle(
                candidate,
                context.manifest.bundle.size,
                context.manifest.bundle.sha256,
            )
            archive = candidate
        else:
            try:
                context.probe.runner(
                    (
                        str(context.repository_root / "tools/build-bootstrap-bundle"),
                        "--clean",
                        "--output",
                        str(output),
                        "--builder-image",
                        context.manifest.builder.image,
                        "--expected-sha256",
                        context.manifest.bundle.sha256,
                    ),
                    cwd=context.repository_root,
                    check=True,
                    shell=False,
                )
            except (OSError, subprocess.CalledProcessError):
                raise BootstrapError("source bundle build failed") from None
            archive = output / context.manifest.bundle.filename
    return extract_bundle(
        archive,
        extraction / context.manifest.bundle.expected_root,
        context.manifest,
    )


def _publish_fm(context: PrepareContext, ge_root: Path) -> PublishedToolRecord:
    stage_parent = _ensure_cache_subdirectory(context.host.cache_root, "staging")
    stage = stage_parent / _FM_NAME
    prepare_fm_tree(ge_root, stage)
    if (
        context.host.compatibility_tools
        != context.host.steam_root / "compatibilitytools.d"
    ):
        raise BootstrapError("compatibility-tool root is invalid")
    try:
        steam_fd = os.open(context.host.steam_root, _DIRECTORY)
    except OSError:
        raise BootstrapError("native Steam root changed before publication") from None
    compatibility_fd: int | None = None
    try:
        try:
            os.mkdir("compatibilitytools.d", 0o755, dir_fd=steam_fd)
            os.fsync(steam_fd)
        except FileExistsError:
            pass
        compatibility_fd = os.open("compatibilitytools.d", _DIRECTORY, dir_fd=steam_fd)
        info = os.fstat(compatibility_fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise BootstrapError("compatibility-tool root is unsafe")
    except OSError:
        raise BootstrapError("compatibility-tool root is unsafe") from None
    finally:
        if compatibility_fd is not None:
            os.close(compatibility_fd)
        os.close(steam_fd)
    return publish_fm(stage, context.host.compatibility_tools / _FM_NAME)


def default_prepare_operations() -> PrepareOperations:
    return PrepareOperations(
        preflight=inspect_prepare,
        capture_before=_capture_before,
        acquire_ge=_acquire_ge,
        acquire_bundle=_acquire_bundle,
        publish_fm=_publish_fm,
        locked_preflight=lambda context, lease: inspect_prepare(
            context, held_launcher_lock=lease
        ),
    )


def _operations(context: PrepareContext) -> PrepareOperations:
    operations = context.operations or default_prepare_operations()
    if not isinstance(operations, PrepareOperations):
        raise BootstrapError("Prepare operations are invalid")
    return operations


def _record(operations: PrepareOperations, name: str) -> None:
    operations.event(name)
    operations.boundary(name)


def _write_transaction_json(
    state: BootstrapState, name: str, value: Mapping[str, object]
) -> None:
    if state._state_root is None:
        raise BootstrapError("bootstrap state is not bound to storage")
    root_fd = open_owned_root(state._state_root)
    transaction_fd: int | None = None
    try:
        transaction_fd = os.open(state.transaction_id, _DIRECTORY, dir_fd=root_fd)
        atomic_write_private_json(transaction_fd, name, dict(value))
    finally:
        if transaction_fd is not None:
            os.close(transaction_fd)
        os.close(root_fd)


def _append_boundary(state: BootstrapState, boundary: str) -> BootstrapState:
    return transition(
        state,
        BootstrapPhase.PREPARING,
        BootstrapPhase.PREPARING,
        completed_boundaries=(*state.completed_boundaries, boundary),
    )


def _logical_cache_reference(root: Path, path: Path, label: str) -> dict[str, str]:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        raise BootstrapError(f"{label} escaped the private cache") from None
    if not relative or relative.startswith("../"):
        raise BootstrapError(f"{label} escaped the private cache")
    return {"root": "cache", "relative": relative}


def _validated_acquisition_reference(
    context: PrepareContext, path: Path, label: str
) -> dict[str, str]:
    reference = _logical_cache_reference(context.host.cache_root, path, label)
    relative = Path(reference["relative"])
    descriptors: list[int] = []
    try:
        current_fd = os.open(context.host.cache_root, _DIRECTORY)
        descriptors.append(current_fd)
        root_info = os.fstat(current_fd)
        if root_info.st_uid != os.getuid():
            raise OSError
        for part in relative.parts:
            current_fd = os.open(part, _DIRECTORY, dir_fd=current_fd)
            descriptors.append(current_fd)
            info = os.fstat(current_fd)
            if info.st_uid != os.getuid():
                raise OSError
    except OSError:
        raise BootstrapError(f"{label} escaped the private cache") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    return reference


def _validate_before_snapshot(
    snapshot: Snapshot,
    state: BootstrapState,
    context: PrepareContext,
    inspection: PrepareInspection,
) -> None:
    if (
        not isinstance(snapshot, Snapshot)
        or snapshot.transaction_id != state.transaction_id
        or snapshot.manifest_sha256 != context.manifest.sha256
        or snapshot.steam_root_id != inspection.root_ids["steam"]
    ):
        raise BootstrapError("before snapshot identity does not match Prepare")


def _acquire_launcher_lease(host: ResolvedHost) -> int:
    try:
        runtime_fd = os.open(host.runtime_dir, _DIRECTORY)
    except OSError:
        raise BootstrapError("runtime directory changed before Prepare") from None
    try:
        try:
            lock_fd = os.open(
                "forza-linux.lock",
                os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=runtime_fd,
            )
        except OSError:
            raise BootstrapError("cannot acquire the launcher lock") from None
    finally:
        os.close(runtime_fd)
    try:
        info = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise BootstrapError("launcher lock is unsafe")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BootstrapError("launcher lock is held by another process") from None
        return lock_fd
    except Exception:
        os.close(lock_fd)
        raise


def _prepare_confirmed(
    context: PrepareContext,
    operations: PrepareOperations,
    inspection: PrepareInspection,
    plan: dict[str, Any],
    digest: str,
    lease_fd: int,
) -> BootstrapState:
    reinspection = (
        operations.locked_preflight(context, lease_fd)
        if operations.locked_preflight is not None
        else operations.preflight(context)
    )
    if plan_digest(build_prepare_plan(context, reinspection)) != digest:
        raise BootstrapError(
            "Prepare inputs changed during confirmation; no changes were made"
        )

    state = create_transaction(context.host.state_root, context.manifest.sha256)
    state = transition(
        state,
        BootstrapPhase.NEW,
        BootstrapPhase.PREPARING,
        completed_boundaries=("confirmed-plan",),
    )
    _write_transaction_json(state, "plan.json", plan)

    before = operations.capture_before(context, state.transaction_id)
    _validate_before_snapshot(before, state, context, inspection)
    write_snapshot(
        context.host.state_root / state.transaction_id / "before.json", before
    )
    state = _append_boundary(state, "before-snapshot")
    _record(operations, "write-before-snapshot")

    ge_root: Path | None = None
    if inspection.tool_disposition is ToolDisposition.ABSENT:
        ge_root = operations.acquire_ge(context)
        if not isinstance(ge_root, Path):
            raise BootstrapError("GE-Proton acquisition returned an invalid root")
        ge_reference = _validated_acquisition_reference(
            context, ge_root, "GE-Proton root"
        )
        state = _append_boundary(state, "ge-acquired")
        _record(operations, "acquire-ge")

    bundle = operations.acquire_bundle(context)
    if not isinstance(bundle, VerifiedBundle):
        raise BootstrapError("bundle acquisition returned an invalid root")
    bundle_reference = _validated_acquisition_reference(
        context, bundle.root, "bundle root"
    )
    if (
        bundle.artifacts != context.manifest.artifacts
        or not isinstance(bundle.manifest_sha256, str)
        or _SHA256.fullmatch(bundle.manifest_sha256) is None
    ):
        raise BootstrapError("verified bundle artifact contract is invalid")
    state = _append_boundary(state, "bundle-acquired")
    _record(operations, "acquire-bundle")

    if inspection.tool_disposition is ToolDisposition.ADOPTED:
        assert inspection.existing_tool_identity is not None
        current_tool = inspect_fm(
            context.host.compatibility_tools / _FM_NAME, context.manifest
        )
        if (
            current_tool.disposition is not ToolDisposition.ADOPTED
            or current_tool.identity != inspection.existing_tool_identity
            or current_tool.tree_sha256 != inspection.existing_tool_tree_sha256
        ):
            raise BootstrapError("adopted compatibility tool changed after acquisition")
        disposition = "adopted"
        identity = current_tool.identity
        tool_tree_sha256 = current_tool.tree_sha256
        assert identity is not None
        ge_reference = {
            "root": "steam",
            "relative": f"compatibilitytools.d/{_FM_NAME}",
        }
    else:
        assert ge_root is not None
        published = operations.publish_fm(context, ge_root)
        if (
            not isinstance(published, PublishedToolRecord)
            or published.disposition is not ToolDisposition.CREATED
            or published.destination != context.host.compatibility_tools / _FM_NAME
            or _SHA256.fullmatch(published.published_tree_sha256) is None
        ):
            raise BootstrapError(
                "compatibility-tool destination changed after confirmation"
            )
        disposition = "created"
        identity = published.identity
        tool_tree_sha256 = published.published_tree_sha256
        state = _append_boundary(state, "compatibility-tool-published")
        _record(operations, "publish-fm-tool")

    prepare_record: dict[str, object] = {
        "schema": 1,
        "plan_sha256": digest,
        "ge_root": ge_reference,
        "bundle_root": bundle_reference,
        "bundle_manifest_sha256": bundle.manifest_sha256,
        "compatibility_tool": {
            "disposition": disposition,
            "identity": _identity_value(identity),
            "tree_sha256": tool_tree_sha256,
        },
    }
    _write_transaction_json(state, "prepare.json", prepare_record)
    state = transition(
        state,
        BootstrapPhase.PREPARING,
        BootstrapPhase.AWAITING_STEAM_PREFIX,
        completed_boundaries=(*state.completed_boundaries, "awaiting-steam-prefix"),
        compatibility_tool_disposition=disposition,
    )
    _record(operations, "checkpoint-awaiting-prefix")
    context.output.write(
        "Prepare complete. Fully restart Steam, then select GE-Proton11-3-FM "
        "for Forza Motorsport. Leave the Steam launch options empty, Launch "
        "Forza yourself once, wait for its prefix to be created, close it, and "
        "rerun this bootstrap command with --threading-dll.\n"
    )
    return state


def prepare(context: PrepareContext, confirm: Callable[[str], str]) -> BootstrapState:
    """Execute confirmed Phase A and stop at the manual Steam handoff."""
    _validate_context(context)
    if not callable(confirm):
        raise BootstrapError("Prepare confirmation callback is invalid")
    operations = _operations(context)

    inspection = operations.preflight(context)
    _record(operations, "preflight")
    plan = build_prepare_plan(context, inspection)
    digest = plan_digest(plan)
    context.output.write(canonical_json(plan).decode("ascii") + "\n")
    context.output.write(f"PLAN_SHA256={digest}\n")
    _record(operations, "print-prepare-plan")
    confirmation = confirm(digest)
    if (
        not isinstance(confirmation, str)
        or _SHA256.fullmatch(confirmation) is None
        or confirmation != digest
    ):
        raise BootstrapError(
            "Prepare plan digest confirmation did not match; no changes were made"
        )
    _record(operations, "confirm-prepare-plan")

    reinspection = operations.preflight(context)
    if plan_digest(build_prepare_plan(context, reinspection)) != digest:
        raise BootstrapError(
            "Prepare inputs changed during confirmation; no changes were made"
        )
    lease_fd = _acquire_launcher_lease(context.host)
    try:
        return _prepare_confirmed(
            context, operations, inspection, plan, digest, lease_fd
        )
    finally:
        os.close(lease_fd)
