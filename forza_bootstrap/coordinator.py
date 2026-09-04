# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Two-phase bootstrap coordination for Prepare and Finish."""

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
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, TextIO

from .artifacts import VerifiedBundle, download_once, extract_bundle, verify_bundle_root
from .container_build import check_docker
from .licensed import (
    MAX_LICENSED_SIZE,
    LicensedAction,
    install_threading_copy,
    plan_threading_copy,
    rollback_threading_copy,
)
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
    rollback_published_fm,
)
from .safeio import (
    atomic_write_private_json,
    ensure_private_directory,
    open_owned_root,
    read_private_json,
)
from .snapshot import (
    ChangeRule,
    ComparedRecord,
    Comparison,
    FileRecord,
    Snapshot,
    SnapshotScope,
    SnapshotTarget,
    capture_snapshot,
    compare_snapshots,
    read_snapshot,
    render_comparison,
    write_snapshot,
)
from .state import (
    BootstrapPhase,
    BootstrapState,
    create_transaction,
    load_transaction,
    load_unfinished_transaction,
    transition,
)

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


@dataclass(frozen=True)
class PrefixInspection:
    prefix_root_id: str
    system32_id: str


@dataclass(frozen=True)
class PlannedTarget:
    logical_path: str
    sha256: str
    mode: int


@dataclass(frozen=True)
class RuntimePlan:
    sha256: str
    evidence_sha256: str
    targets: tuple[PlannedTarget, ...]
    disposition: Literal["install", "keep"]


@dataclass(frozen=True)
class PatchInspection:
    states: tuple[Literal["original", "patched"], ...]
    targets: tuple[PlannedTarget, ...]
    disposition: Literal["apply", "keep"]


@dataclass(frozen=True)
class FinishPlan:
    document: dict[str, object]
    sha256: str
    runtime_plan_sha256: str
    patch_after_sha256: tuple[str, ...]
    snapshot_rules: tuple[ChangeRule, ...]


@dataclass(frozen=True)
class FinishOperations:
    validate_prefix: Callable[[FinishContext], PrefixInspection]
    plan_threading: Callable[[FinishContext], LicensedAction]
    capture_current: Callable[[FinishContext], Snapshot]
    lock_runtime_evidence: Callable[[FinishContext], str]
    runtime_plan: Callable[[FinishContext, Snapshot, str], RuntimePlan]
    inspect_patches: Callable[[FinishContext, Snapshot], PatchInspection]
    command_versions: Callable[[FinishContext, RuntimePlan], Mapping[str, str]]
    install_threading: Callable[[FinishContext, LicensedAction, Path], None]
    install_runtime: Callable[[FinishContext, RuntimePlan], str | None]
    runtime_status: Callable[[FinishContext, RuntimePlan, str | None], None]
    apply_patches: Callable[[FinishContext, PatchInspection, Path], Path | None]
    doctor: Callable[[FinishContext], str]
    steam_options: Callable[[FinishContext], str]
    event: Callable[[str], None] = lambda _event: None
    boundary: Callable[[str], None] = lambda _boundary: None


@dataclass(frozen=True)
class FinishContext:
    manifest: BootstrapManifest
    host: ResolvedHost
    repository_root: Path
    state: BootstrapState
    threading_dll: Path | None
    bundle_root: Path
    bundle_manifest_sha256: str
    compatibility_tool_tree_sha256: str
    output: TextIO = sys.stdout
    operations: FinishOperations | None = None
    probe: HostProbe = field(default_factory=HostProbe)
    runner: _Runner = subprocess.run
    child_environment: Mapping[str, str] = field(default_factory=dict)
    command_sha256: Mapping[str, str] = field(default_factory=dict)
    launcher_lease_fd: int | None = None


RecoveryStatus = Literal["pending", "rolled_back", "not_applicable", "accepted"]


@dataclass(frozen=True)
class RecoveryOperations:
    """Inspection and mutation boundaries owned by bootstrap recovery."""

    resume_prepare: Callable[[RecoveryContext, BootstrapState], BootstrapState]
    resume_finish: Callable[[RecoveryContext, BootstrapState], BootstrapState]
    inspect_patches: Callable[[RecoveryContext, BootstrapState], RecoveryStatus]
    restore_patches: Callable[[RecoveryContext, BootstrapState], None]
    inspect_runtime: Callable[[RecoveryContext, BootstrapState], RecoveryStatus]
    rollback_runtime: Callable[[RecoveryContext, BootstrapState], None]
    inspect_threading: Callable[[RecoveryContext, BootstrapState], RecoveryStatus]
    rollback_threading: Callable[[RecoveryContext, BootstrapState], None]
    inspect_tool: Callable[[RecoveryContext, BootstrapState], RecoveryStatus]
    rollback_tool: Callable[[RecoveryContext, BootstrapState], None]
    event: Callable[[str], None] = lambda _event: None
    boundary: Callable[[str], None] = lambda _boundary: None


@dataclass(frozen=True)
class RecoveryContext:
    """Private durable state and exact owner operations for resume/rollback."""

    state: BootstrapState
    state_root: Path
    output: TextIO = sys.stdout
    operations: RecoveryOperations | None = None
    prepare_context: PrepareContext | None = None
    finish_context: FinishContext | None = None


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
            or lease_info.st_nlink != 1
            or (lease_info.st_dev, lease_info.st_ino)
            != (linked_info.st_dev, linked_info.st_ino)
        ):
            raise OSError
        fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        raise BootstrapError(
            "launcher lock changed while coordinator held it"
        ) from None
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


def _published_tool_value(record: PublishedToolRecord) -> dict[str, object]:
    return {
        "schema": 1,
        "destination": {
            "root": "steam",
            "relative": f"compatibilitytools.d/{_FM_NAME}",
        },
        "disposition": record.disposition.value,
        "identity": _identity_value(record.identity),
        "root_device": record.root_device,
        "root_inode": record.root_inode,
        "published_tree_sha256": record.published_tree_sha256,
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


def _transaction_json_if_present(
    state: BootstrapState, name: str
) -> dict[str, object] | None:
    try:
        return _read_transaction_json(state, name)
    except FileNotFoundError:
        return None


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
            or info.st_nlink != 1
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
    _write_transaction_json(state, "prepare-plan.json", plan)

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
        _write_transaction_json(
            state,
            "compatibility-tool.json",
            _published_tool_value(
                PublishedToolRecord(
                    context.host.compatibility_tools / _FM_NAME,
                    ToolDisposition.ADOPTED,
                    identity,
                    0,
                    0,
                    tool_tree_sha256,
                )
            ),
        )
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
        _write_transaction_json(
            state, "compatibility-tool.json", _published_tool_value(published)
        )
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


_RUNTIME_TARGETS: dict[str, tuple[str, str, str]] = {
    "launcher-gate": (
        "user",
        ".local/bin/forza-linux",
        "user/.local/bin/forza-linux",
    ),
    "doctor": (
        "user",
        ".local/bin/forza-doctor",
        "user/.local/bin/forza-doctor",
    ),
    "known-build-patcher": (
        "user",
        ".local/libexec/forza-motorsport-linux/patch-known-build",
        "user/.local/libexec/forza-motorsport-linux/patch-known-build",
    ),
    "supported-builds": (
        "user",
        ".local/share/forza-motorsport-linux/supported-builds.toml",
        "user/.local/share/forza-motorsport-linux/supported-builds.toml",
    ),
    "xgameruntime-pe64": (
        "compat-tool",
        "files/lib/wine/x86_64-windows/xgameruntime.dll",
        "compat/xgameruntime-pe64",
    ),
    "xgameruntime-unix64": (
        "compat-tool",
        "files/lib/wine/x86_64-unix/xgameruntime.so",
        "compat/xgameruntime-unix64",
    ),
    "xodus-service": (
        "user",
        ".local/libexec/xodus-forza/xodus-service",
        "user/.local/libexec/xodus-forza/xodus-service",
    ),
    "xodus-cli": (
        "user",
        ".local/libexec/xodus-forza/xodus-cli",
        "user/.local/libexec/xodus-forza/xodus-cli",
    ),
    "xodus-overlay": (
        "user",
        ".local/libexec/xodus-forza/xodus-overlay",
        "user/.local/libexec/xodus-forza/xodus-overlay",
    ),
    "systemd-unit": (
        "user",
        ".config/systemd/user/xodus-forza.service",
        "user/.config/systemd/user/xodus-forza.service",
    ),
}
_USER_MANIFEST_PATH = ".local/state/forza-motorsport-linux/install-manifest"
_USER_MANIFEST_LOGICAL = f"user/{_USER_MANIFEST_PATH}"
_INSTALL_HEADER = "forza-motorsport-linux-install-v1"
_USER_PAYLOADS = (
    ".local/bin/forza-linux",
    ".local/bin/forza-doctor",
    ".local/libexec/forza-motorsport-linux/patch-known-build",
    ".local/share/forza-motorsport-linux/supported-builds.toml",
    ".local/libexec/xodus-forza/xodus-service",
    ".local/libexec/xodus-forza/xodus-cli",
    ".local/libexec/xodus-forza/xodus-overlay",
    ".config/systemd/user/xodus-forza.service",
)
_PATCH_TARGETS = (
    (
        "compat/controller",
        "compatibilitytools.d/GE-Proton11-3-FM/files/lib/wine/x86_64-windows/windows.gaming.input.dll",
        "windows.gaming.input.dll",
    ),
    (
        "prefix/controller",
        "steamapps/compatdata/2440510/pfx/drive_c/windows/system32/windows.gaming.input.dll",
        "windows.gaming.input.dll",
    ),
    (
        "compat/mountmgr",
        "compatibilitytools.d/GE-Proton11-3-FM/files/lib/wine/x86_64-windows/mountmgr.sys",
        "mountmgr.sys",
    ),
    (
        "prefix/mountmgr",
        "steamapps/compatdata/2440510/pfx/drive_c/windows/system32/drivers/mountmgr.sys",
        "mountmgr.sys",
    ),
)
_RUNTIME_PLAN_LINE = re.compile(
    r"PLAN (?P<role>[a-z0-9-]+): "
    r"(?P<root>compat-tool|user)/(?P<relative>[^ ]+) "
    r"before=(?:absent|[0-9a-f]{64}) "
    r"source=(?P<source>[0-9a-f]{64}) mode=(?P<mode>[0-7]{4})\Z"
)


def _prefix_directory(context: FinishContext) -> Path:
    return context.host.steam_root / "steamapps/compatdata/2440510/pfx"


def _validate_prefix_tree(context: FinishContext) -> PrefixInspection:
    prefix_relative = Path("steamapps/compatdata/2440510/pfx")
    system32_relative = prefix_relative / "drive_c/windows/system32"
    required = system32_relative / "drivers"
    descriptor: int | None = None
    prefix_info: os.stat_result | None = None
    system32_info: os.stat_result | None = None
    try:
        descriptor = os.open(context.host.steam_root, _DIRECTORY)
        root_info = os.fstat(descriptor)
        if root_info.st_uid != os.getuid():
            raise BootstrapError("Forza prefix has unsafe ownership")
        current = Path()
        for part in required.parts:
            current /= part
            next_descriptor = os.open(part, _DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid():
                raise BootstrapError("Forza prefix has unsafe ownership")
            if current == prefix_relative:
                prefix_info = info
            elif current == system32_relative:
                system32_info = info
    except FileNotFoundError:
        raise BootstrapError("Forza prefix is missing or incomplete") from None
    except BootstrapError:
        raise
    except OSError:
        raise BootstrapError("Forza prefix has an unsafe path") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if prefix_info is None or system32_info is None:
        raise BootstrapError("Forza prefix is missing or incomplete")
    return PrefixInspection(
        f"{prefix_info.st_dev}:{prefix_info.st_ino}",
        f"{system32_info.st_dev}:{system32_info.st_ino}",
    )


def validate_prefix(context: FinishContext) -> PrefixInspection:
    """Validate the exact current-user prefix without following any component."""
    if not isinstance(context, FinishContext):
        raise BootstrapError("Finish context is invalid")
    _owned_directory(context.host.steam_root, "native Steam root")
    _validate_app_manifest(context.host.app_manifest)
    _probe_xodus_service(context.probe)
    _probe_forza_processes(context.probe)
    _probe_runtime_children(context.host)
    inspection = _validate_prefix_tree(context)
    tool = inspect_fm(context.host.compatibility_tools / _FM_NAME, context.manifest)
    if (
        tool.disposition is not ToolDisposition.ADOPTED
        or tool.tree_sha256 != context.compatibility_tool_tree_sha256
    ):
        raise BootstrapError("dedicated compatibility tool changed before Finish")
    bundle = verify_bundle_root(context.bundle_root, context.manifest)
    if bundle.manifest_sha256 != context.bundle_manifest_sha256:
        raise BootstrapError("verified bundle changed before Finish")
    return inspection


def _minimal_finish_environment(context: FinishContext) -> dict[str, str]:
    state_home = context.host.state_root.parents[1]
    cache_home = context.host.cache_root.parents[1]
    environment = {
        "HOME": str(context.host.user_root),
        "LANG": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "XDG_CACHE_HOME": str(cache_home),
        "XDG_DATA_HOME": str(context.host.steam_root.parent),
        "XDG_RUNTIME_DIR": str(context.host.runtime_dir),
        "XDG_STATE_HOME": str(state_home),
    }
    allowed = {
        "DBUS_SESSION_BUS_ADDRESS",
        "FORZA_BUSCTL",
        "FORZA_DF",
        "FORZA_RUNTIME_EXPECTED_XGAMERUNTIME_SHA",
        "FORZA_RUNTIME_EXPECTED_XODUS_SHA",
        "FORZA_RUNTIME_INTEGRATION_ROOT",
        "FORZA_RUNTIME_TESTING",
        "FORZA_SYSTEMCTL",
        "FORZA_TEST_ROOT",
        "FORZA_TEST_UID",
    }
    for name, value in context.child_environment.items():
        if name not in allowed or not isinstance(value, str) or "\x00" in value:
            raise BootstrapError("Finish child environment is invalid")
        environment[name] = value
    return environment


def _open_finish_executable(path: Path, label: str) -> tuple[int, str]:
    if not path.is_absolute():
        raise BootstrapError(f"{label} executable path is invalid")
    parent_fd: int | None = None
    executable_fd: int | None = None
    try:
        parent_fd = _open_owned_directory(path.parent, f"{label} executable")
        executable_fd = os.open(
            path.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_fd,
        )
        before = os.fstat(executable_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or not stat.S_IMODE(before.st_mode) & 0o111
            or before.st_size > 16 * 1024 * 1024
        ):
            raise OSError
        digest = hashlib.sha256()
        remaining = 16 * 1024 * 1024 + 1
        while remaining:
            block = os.read(executable_fd, min(64 * 1024, remaining))
            if not block:
                break
            digest.update(block)
            remaining -= len(block)
        after = os.fstat(executable_fd)
        bound = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            remaining == 0
            or _stat_generation(before) != _stat_generation(after)
            or _stat_generation(after) != _stat_generation(bound)
        ):
            raise OSError
        os.lseek(executable_fd, 0, os.SEEK_SET)
        return executable_fd, digest.hexdigest()
    except (OSError, BootstrapError):
        if executable_fd is not None:
            os.close(executable_fd)
        raise BootstrapError(f"{label} executable is unsafe") from None
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def _run_finish_child(
    context: FinishContext,
    argv: tuple[str, ...],
    label: str,
    *,
    command: str,
    accepted: frozenset[int] = frozenset({0}),
) -> subprocess.CompletedProcess[str]:
    if not argv or any(
        not isinstance(argument, str) or "\x00" in argument for argument in argv
    ):
        raise BootstrapError(f"{label} command is invalid")
    required = {"runtime-installer", "patcher", "doctor", "steam-options"}
    if command not in required:
        raise BootstrapError(f"{label} command identity is invalid")
    expected = context.command_sha256.get(command)
    if context.command_sha256 and (
        set(context.command_sha256) != required
        or not isinstance(expected, str)
        or _SHA256.fullmatch(expected) is None
    ):
        raise BootstrapError(f"{label} command identity is invalid")
    executable_fd, actual = _open_finish_executable(Path(argv[0]), label)
    if expected is not None and actual != expected:
        os.close(executable_fd)
        raise BootstrapError(f"{label} executable changed after Finish plan")
    pass_fds = [executable_fd]
    environment = _minimal_finish_environment(context)
    if command == "runtime-installer":
        environment.setdefault(
            "FORZA_RUNTIME_INTEGRATION_ROOT", str(context.repository_root)
        )
    if (
        command == "runtime-installer"
        and len(argv) > 1
        and argv[1] in {"install", "rollback", "restore-runtime", "accept"}
        and context.launcher_lease_fd is not None
    ):
        pass_fds.append(context.launcher_lease_fd)
        environment["FORZA_LAUNCHER_LEASE_FD"] = str(context.launcher_lease_fd)
    try:
        result = context.runner(
            argv,
            cwd=context.repository_root,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            env=environment,
            executable=f"/proc/self/fd/{executable_fd}",
            pass_fds=tuple(pass_fds),
        )
    except (FileNotFoundError, OSError):
        raise BootstrapError(f"{label} could not be executed") from None
    finally:
        os.close(executable_fd)
    if (
        not isinstance(result, subprocess.CompletedProcess)
        or type(result.returncode) is not int
        or result.returncode not in accepted
        or not isinstance(result.stdout, str)
        or not isinstance(result.stderr, str)
    ):
        raise BootstrapError(f"{label} failed")
    return result


def _runtime_common_argv(context: FinishContext) -> tuple[str, ...]:
    evidence = (
        context.host.user_root
        / ".local/state/forza-motorsport-linux/runtime-transactions/artifact-evidence.json"
    )
    return (
        str(context.repository_root / "scripts/install-runtime-components"),
        "--compat-tool-root",
        str(context.host.compatibility_tools / _FM_NAME),
        "--user-root",
        str(context.host.user_root),
        "--artifact-bundle-root",
        str(context.bundle_root),
        "--bundle-manifest",
        str(context.bundle_root / "bundle-manifest.json"),
        "--artifact-evidence-manifest",
        str(evidence),
    )


def _runtime_argv(
    context: FinishContext, operation: str, *extra: str
) -> tuple[str, ...]:
    common = _runtime_common_argv(context)
    return (common[0], operation, *common[1:], *extra)


def _default_lock_runtime_evidence(context: FinishContext) -> str:
    result = _run_finish_child(
        context,
        _runtime_argv(context, "lock-evidence"),
        "runtime evidence lock",
        command="runtime-installer",
    )
    match = re.fullmatch(r"EVIDENCE_SHA256=([0-9a-f]{64})\n?", result.stdout)
    if match is None:
        raise BootstrapError("runtime evidence lock returned invalid output")
    return match.group(1)


def _runtime_manifest_target(
    runtime_targets: Mapping[str, PlannedTarget],
) -> PlannedTarget:
    by_relative = {
        relative: runtime_targets[role]
        for role, (root, relative, _logical) in _RUNTIME_TARGETS.items()
        if root == "user"
    }
    if set(by_relative) != set(_USER_PAYLOADS):
        raise BootstrapError("runtime child plan does not cover the user manifest")
    lines = [_INSTALL_HEADER]
    lines.extend(
        f"{relative}\t{by_relative[relative].mode:o}\t{by_relative[relative].sha256}"
        for relative in _USER_PAYLOADS
    )
    payload = ("\n".join(lines) + "\n").encode("ascii")
    return PlannedTarget(
        _USER_MANIFEST_LOGICAL, hashlib.sha256(payload).hexdigest(), 0o600
    )


def _snapshot_has_targets(
    snapshot: Snapshot, targets: tuple[PlannedTarget, ...]
) -> bool:
    records = _snapshot_records(snapshot, "pre-Finish")
    return all(
        target.logical_path in records
        and records[target.logical_path].state == "regular"
        and records[target.logical_path].sha256 == target.sha256
        and records[target.logical_path].mode == target.mode
        for target in targets
    )


def _default_runtime_plan(
    context: FinishContext, snapshot: Snapshot, evidence_sha256: str
) -> RuntimePlan:
    result = _run_finish_child(
        context,
        _runtime_argv(context, "plan"),
        "runtime child plan",
        command="runtime-installer",
    )
    records: dict[str, PlannedTarget] = {}
    digest: str | None = None
    for line in result.stdout.splitlines():
        if line.startswith(
            (
                "PLAN runtime profile:",
                "PLAN source revision ",
                "PLAN user ownership:",
            )
        ):
            continue
        if line.startswith("PLAN_SHA256="):
            if (
                digest is not None
                or re.fullmatch(r"PLAN_SHA256=[0-9a-f]{64}", line) is None
            ):
                raise BootstrapError("runtime child plan returned invalid output")
            digest = line.removeprefix("PLAN_SHA256=")
            continue
        match = _RUNTIME_PLAN_LINE.fullmatch(line)
        if match is None:
            raise BootstrapError("runtime child plan returned invalid output")
        role = match.group("role")
        contract = _RUNTIME_TARGETS.get(role)
        if (
            contract is None
            or role in records
            or match.group("root") != contract[0]
            or match.group("relative") != contract[1]
        ):
            raise BootstrapError("runtime child plan returned invalid target")
        records[role] = PlannedTarget(
            contract[2], match.group("source"), int(match.group("mode"), 8)
        )
    if digest is None or set(records) != set(_RUNTIME_TARGETS):
        raise BootstrapError("runtime child plan is incomplete")
    manifest = _runtime_manifest_target(records)
    targets = tuple(records[role] for role in _RUNTIME_TARGETS) + (manifest,)
    targets = tuple(sorted(targets, key=lambda item: item.logical_path))
    disposition: Literal["install", "keep"] = (
        "keep" if _snapshot_has_targets(snapshot, targets) else "install"
    )
    return RuntimePlan(digest, evidence_sha256, targets, disposition)


def _patch_specs(context: FinishContext) -> dict[str, str]:
    path = context.repository_root / "manifests/supported-builds.toml"
    data = _read_owned_regular(path, "supported build manifest", 1024 * 1024)
    assert data is not None
    try:
        value = tomllib.loads(data.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError):
        raise BootstrapError("supported build manifest is invalid") from None
    patches = value.get("patches")
    if not isinstance(patches, list):
        raise BootstrapError("supported build manifest is invalid")
    result: dict[str, str] = {}
    for patch in patches:
        if not isinstance(patch, dict):
            raise BootstrapError("supported build manifest is invalid")
        file = patch.get("file")
        digest = patch.get("patched_sha256")
        if (
            not isinstance(file, str)
            or file in result
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
        ):
            raise BootstrapError("supported build manifest is invalid")
        result[file] = digest
    if set(result) != {"windows.gaming.input.dll", "mountmgr.sys"}:
        raise BootstrapError("supported build manifest is invalid")
    return result


def _default_inspect_patches(
    context: FinishContext, snapshot: Snapshot
) -> PatchInspection:
    specs = _patch_specs(context)
    records = _snapshot_records(snapshot, "pre-Finish")
    states: list[Literal["original", "patched"]] = []
    targets: list[PlannedTarget] = []
    patcher = context.repository_root / "scripts/patch-known-build"
    manifest = context.repository_root / "manifests/supported-builds.toml"
    for logical, relative, file in _PATCH_TARGETS:
        result = _run_finish_child(
            context,
            (
                str(patcher),
                "inspect",
                "--manifest",
                str(manifest),
                "--target",
                str(context.host.steam_root / relative),
            ),
            "patch inspection",
            command="patcher",
        )
        state = result.stdout.rstrip("\n")
        if state not in {"original", "patched"}:
            raise BootstrapError("patch state is unknown or mixed")
        item = records.get(logical)
        if item is None or item.state != "regular" or item.mode is None:
            raise BootstrapError("patch target is outside the pre-Finish snapshot")
        states.append(state)
        targets.append(PlannedTarget(logical, specs[file], item.mode))
    if set(states) == {"original"}:
        disposition: Literal["apply", "keep"] = "apply"
    elif set(states) == {"patched"}:
        disposition = "keep"
    else:
        raise BootstrapError("patch state is unknown or mixed")
    return PatchInspection(tuple(states), tuple(targets), disposition)


def _command_sha256(path: Path) -> str:
    descriptor, digest = _open_finish_executable(path, "Finish command")
    os.close(descriptor)
    return digest


def _default_command_versions(
    context: FinishContext, runtime: RuntimePlan
) -> Mapping[str, str]:
    doctor_targets = tuple(
        target
        for target in runtime.targets
        if target.logical_path == "user/.local/bin/forza-doctor"
    )
    if len(doctor_targets) != 1 or not doctor_targets[0].mode & 0o111:
        raise BootstrapError("runtime child plan has no executable doctor")
    return {
        "runtime-installer": _command_sha256(
            context.repository_root / "scripts/install-runtime-components"
        ),
        "patcher": _command_sha256(
            context.repository_root / "scripts/patch-known-build"
        ),
        "doctor": doctor_targets[0].sha256,
        "steam-options": _command_sha256(
            context.repository_root / "scripts/print-steam-options"
        ),
    }


def _default_install_runtime(
    context: FinishContext, runtime: RuntimePlan
) -> str | None:
    result = _run_finish_child(
        context,
        _runtime_argv(
            context,
            "install",
            "--plan-sha256",
            runtime.sha256,
        ),
        "runtime child install",
        command="runtime-installer",
    )
    match = re.fullmatch(r"TRANSACTION_ID=([0-9a-f]{24})\n?", result.stdout)
    if match is None:
        raise BootstrapError("runtime child returned an invalid transaction id")
    return match.group(1)


def _default_runtime_status(
    context: FinishContext, runtime: RuntimePlan, transaction: str | None
) -> None:
    if runtime.disposition == "keep":
        if transaction is not None:
            raise BootstrapError("runtime no-op unexpectedly created a transaction")
        return
    if transaction is None:
        raise BootstrapError("runtime child transaction id is missing")
    result = _run_finish_child(
        context,
        _runtime_argv(context, "status"),
        "runtime child status",
        command="runtime-installer",
    )
    lines = result.stdout.splitlines()
    if f"transaction={transaction} state=installed" not in lines or any(
        line.endswith("state=recovery_required") for line in lines
    ):
        raise BootstrapError("runtime child transaction is not installed")


def _default_apply_patches(
    context: FinishContext, patches: PatchInspection, backup_root: Path
) -> Path | None:
    if patches.disposition == "keep":
        return None
    result = _run_finish_child(
        context,
        (
            str(context.repository_root / "scripts/patch-known-build"),
            "apply-forza",
            "--manifest",
            str(context.repository_root / "manifests/supported-builds.toml"),
            "--steam-root",
            str(context.host.steam_root),
            "--backup-root",
            str(backup_root),
        ),
        "patch apply",
        command="patcher",
    )
    lines = result.stdout.splitlines()
    if len(lines) != 1:
        raise BootstrapError("patcher returned an invalid backup manifest")
    backup = Path(lines[0])
    try:
        relative = backup.relative_to(backup_root)
    except ValueError:
        raise BootstrapError("patcher returned an invalid backup manifest") from None
    if len(relative.parts) != 1:
        raise BootstrapError("patcher returned an invalid backup manifest")
    _owned_regular(backup, "patch backup manifest")
    return backup


def _default_doctor(context: FinishContext) -> str:
    try:
        result = _run_finish_child(
            context,
            (str(context.host.user_root / ".local/bin/forza-doctor"),),
            "doctor",
            command="doctor",
        )
    except BootstrapError as error:
        if "executable changed after Finish plan" in str(error):
            raise
        raise BootstrapError("doctor found blockers") from None
    return result.stdout


def _default_steam_options(context: FinishContext) -> str:
    result = _run_finish_child(
        context,
        (str(context.repository_root / "scripts/print-steam-options"),),
        "Steam launch-options command",
        command="steam-options",
    )
    return result.stdout


def default_finish_operations() -> FinishOperations:
    return FinishOperations(
        validate_prefix=validate_prefix,
        plan_threading=lambda context: plan_threading_copy(
            context.threading_dll,
            _prefix_directory(context)
            / "drive_c/windows/system32/xgameruntime.dll.threading",
        ),
        capture_current=lambda context: capture_snapshot(
            _snapshot_scope(
                PrepareContext(
                    context.manifest,
                    context.host,
                    context.repository_root,
                ),
                context.state.transaction_id,
            ),
            {"steam": context.host.steam_root, "user": context.host.user_root},
        ),
        lock_runtime_evidence=_default_lock_runtime_evidence,
        runtime_plan=_default_runtime_plan,
        inspect_patches=_default_inspect_patches,
        command_versions=_default_command_versions,
        install_threading=lambda _context, action, journal: install_threading_copy(
            action, journal
        ),
        install_runtime=_default_install_runtime,
        runtime_status=_default_runtime_status,
        apply_patches=_default_apply_patches,
        doctor=_default_doctor,
        steam_options=_default_steam_options,
    )


@dataclass(frozen=True)
class _FinishInputs:
    prefix: PrefixInspection
    licensed: LicensedAction
    pre_finish: Snapshot
    runtime: RuntimePlan
    patches: PatchInspection
    command_versions: tuple[tuple[str, str], ...]


def _validate_finish_context(context: FinishContext) -> None:
    if not isinstance(context, FinishContext):
        raise BootstrapError("Finish context is invalid")
    if not isinstance(context.manifest, BootstrapManifest):
        raise BootstrapError("Finish manifest is invalid")
    if not isinstance(context.host, ResolvedHost):
        raise BootstrapError("resolved host is invalid")
    if not isinstance(context.state, BootstrapState):
        raise BootstrapError("Finish state is invalid")
    if context.state.phase is not BootstrapPhase.AWAITING_STEAM_PREFIX:
        raise BootstrapError("Finish requires AWAITING_STEAM_PREFIX state")
    if (
        context.state.manifest_sha256 != context.manifest.sha256
        or context.manifest.app_id != _APP_ID
    ):
        raise BootstrapError("Finish state does not match the bootstrap manifest")
    if context.state._state_root != context.host.state_root:
        raise BootstrapError("Finish state is not bound to the resolved state root")
    durable = load_unfinished_transaction(context.host.state_root)
    if durable != context.state:
        raise BootstrapError("Finish does not match the last durable state")
    for path, label in (
        (context.repository_root, "repository root"),
        (context.bundle_root, "bundle root"),
    ):
        if not isinstance(path, Path) or not path.is_absolute():
            raise BootstrapError(f"Finish {label} is invalid")
    if context.threading_dll is None:
        raise BootstrapError("Finish requires --threading-dll")
    if (
        not isinstance(context.threading_dll, Path)
        or not context.threading_dll.is_absolute()
    ):
        raise BootstrapError("Finish threading input must be an absolute path")
    for value, label in (
        (context.bundle_manifest_sha256, "bundle manifest"),
        (context.compatibility_tool_tree_sha256, "compatibility-tool tree"),
    ):
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            raise BootstrapError(f"Finish {label} digest is invalid")
    if not isinstance(context.child_environment, Mapping):
        raise BootstrapError("Finish child environment is invalid")
    if not isinstance(context.command_sha256, Mapping):
        raise BootstrapError("Finish command identities are invalid")
    if (
        context.launcher_lease_fd is not None
        and type(context.launcher_lease_fd) is not int
    ):
        raise BootstrapError("Finish launcher lease is invalid")


def _finish_operations(context: FinishContext) -> FinishOperations:
    operations = context.operations or default_finish_operations()
    if not isinstance(operations, FinishOperations):
        raise BootstrapError("Finish operations are invalid")
    return operations


def _validate_prefix_inspection(value: PrefixInspection) -> None:
    if not isinstance(value, PrefixInspection):
        raise BootstrapError("prefix inspection is invalid")
    for identity in (value.prefix_root_id, value.system32_id):
        if re.fullmatch(r"(?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*)", identity) is None:
            raise BootstrapError("prefix inspection is invalid")


def _validate_planned_targets(
    targets: tuple[PlannedTarget, ...], label: str
) -> tuple[PlannedTarget, ...]:
    if type(targets) is not tuple or not targets:
        raise BootstrapError(f"{label} targets are invalid")
    seen: set[str] = set()
    for target in targets:
        if (
            not isinstance(target, PlannedTarget)
            or not isinstance(target.logical_path, str)
            or not target.logical_path
            or target.logical_path in seen
            or not isinstance(target.sha256, str)
            or _SHA256.fullmatch(target.sha256) is None
            or type(target.mode) is not int
            or not 0 <= target.mode <= 0o7777
        ):
            raise BootstrapError(f"{label} targets are invalid")
        seen.add(target.logical_path)
    return tuple(sorted(targets, key=lambda item: item.logical_path))


def _validate_runtime_plan(value: RuntimePlan, evidence_sha256: str) -> RuntimePlan:
    if (
        not isinstance(value, RuntimePlan)
        or _SHA256.fullmatch(value.sha256) is None
        or value.evidence_sha256 != evidence_sha256
        or value.disposition not in {"install", "keep"}
    ):
        raise BootstrapError("runtime child plan is invalid")
    _validate_planned_targets(value.targets, "runtime child plan")
    return value


def _validate_patch_inspection(value: PatchInspection) -> PatchInspection:
    if (
        not isinstance(value, PatchInspection)
        or type(value.states) is not tuple
        or not value.states
        or any(state not in {"original", "patched"} for state in value.states)
        or value.disposition not in {"apply", "keep"}
        or len(value.states) != len(value.targets)
        or (value.disposition == "apply" and set(value.states) != {"original"})
        or (value.disposition == "keep" and set(value.states) != {"patched"})
    ):
        raise BootstrapError("patch state is unknown or mixed")
    _validate_planned_targets(value.targets, "patch plan")
    return value


def _validated_command_versions(
    value: Mapping[str, str],
) -> tuple[tuple[str, str], ...]:
    required = {"runtime-installer", "patcher", "doctor", "steam-options"}
    if set(value) != required or any(
        not isinstance(item, str) or _SHA256.fullmatch(item) is None
        for item in value.values()
    ):
        raise BootstrapError("Finish command versions are invalid")
    return tuple(sorted(value.items()))


def _validate_finish_snapshot(
    snapshot: Snapshot, context: FinishContext, *, label: str
) -> Snapshot:
    steam_fd: int | None = None
    try:
        steam_fd = _open_owned_directory(context.host.steam_root, "Finish Steam root")
        steam_info = os.fstat(steam_fd)
    except (OSError, BootstrapError):
        raise BootstrapError("Finish Steam root identity is invalid") from None
    finally:
        if steam_fd is not None:
            os.close(steam_fd)
    steam_root_id = f"{steam_info.st_dev}:{steam_info.st_ino}"
    if (
        not isinstance(snapshot, Snapshot)
        or snapshot.transaction_id != context.state.transaction_id
        or snapshot.manifest_sha256 != context.manifest.sha256
        or snapshot.steam_root_id != steam_root_id
    ):
        raise BootstrapError(f"{label} snapshot Steam-root identity is invalid")
    return snapshot


def _collect_finish_inputs(
    context: FinishContext,
    operations: FinishOperations,
    *,
    record_events: bool,
) -> _FinishInputs:
    def event(name: str) -> None:
        if record_events:
            _record(operations, name)

    prefix = operations.validate_prefix(context)
    _validate_prefix_inspection(prefix)
    event("validate-prefix")
    licensed = operations.plan_threading(context)
    if not isinstance(licensed, LicensedAction):
        raise BootstrapError("licensed threading plan is invalid")
    event("validate-threading-input")
    pre_finish = _validate_finish_snapshot(
        operations.capture_current(context), context, label="pre-Finish"
    )
    evidence = operations.lock_runtime_evidence(context)
    if not isinstance(evidence, str) or _SHA256.fullmatch(evidence) is None:
        raise BootstrapError("runtime evidence digest is invalid")
    event("lock-runtime-evidence")
    runtime = _validate_runtime_plan(
        operations.runtime_plan(context, pre_finish, evidence), evidence
    )
    runtime_matches = _snapshot_has_targets(pre_finish, runtime.targets)
    if (runtime.disposition == "keep") != runtime_matches:
        raise BootstrapError("runtime child no-op disposition is invalid")
    event("runtime-plan")
    patches = _validate_patch_inspection(
        operations.inspect_patches(context, pre_finish)
    )
    event("patch-inspect")
    versions = _validated_command_versions(
        operations.command_versions(context, runtime)
    )
    return _FinishInputs(prefix, licensed, pre_finish, runtime, patches, versions)


def _snapshot_records(snapshot: Snapshot, label: str) -> dict[str, FileRecord]:
    if type(snapshot.records) is not tuple:
        raise BootstrapError(f"{label} snapshot records are invalid")
    result: dict[str, FileRecord] = {}
    for item in snapshot.records:
        if not isinstance(item, FileRecord) or item.logical_path in result:
            raise BootstrapError(f"{label} snapshot records are invalid")
        result[item.logical_path] = item
    return result


def _planned_rules(before: Snapshot, inputs: _FinishInputs) -> tuple[ChangeRule, ...]:
    before_records = _snapshot_records(before, "before")
    current_records = _snapshot_records(inputs.pre_finish, "pre-Finish")
    if set(before_records) != set(current_records):
        raise BootstrapError("pre-Finish snapshot coverage changed")
    desired: dict[str, tuple[str | None, int | None]] = {
        path: (item.sha256, item.mode) for path, item in current_records.items()
    }
    for target in (*inputs.runtime.targets, *inputs.patches.targets):
        if target.logical_path not in desired:
            raise BootstrapError("Finish target is outside the snapshot policy")
        desired[target.logical_path] = (target.sha256, target.mode)
    private_path = "prefix/threading"
    if private_path not in desired:
        raise BootstrapError("licensed target is outside the snapshot policy")
    current_private = current_records[private_path]
    licensed_before = inputs.licensed.before
    if (
        current_private.state != licensed_before.state
        or current_private.mode != licensed_before.mode
        or current_private.size != licensed_before.size
        or current_private.sha256 != licensed_before.sha256
        or current_private.private is not True
        or licensed_before.private is not True
    ):
        raise BootstrapError("licensed target changed before Finish planning")
    desired[private_path] = (inputs.licensed.source_sha256, 0o644)
    rules: list[ChangeRule] = []
    for path in sorted(before_records):
        expected_hash, expected_mode = desired[path]
        original = before_records[path]
        unchanged = (
            original.state == ("absent" if expected_hash is None else "regular")
            and original.sha256 == expected_hash
            and original.mode == expected_mode
        )
        rules.append(
            ChangeRule(
                path,
                "unchanged" if unchanged else "expected",
                expected_hash,
                expected_mode,
            )
        )
    return tuple(rules)


def _private_snapshot_value(snapshot: Snapshot) -> dict[str, object]:
    return {
        "version": snapshot.version,
        "transaction_id": snapshot.transaction_id,
        "manifest_sha256": snapshot.manifest_sha256,
        "steam_root_id": snapshot.steam_root_id,
        "records": [
            {
                "logical_path": item.logical_path,
                "state": item.state,
                "mode": item.mode,
                "size": item.size,
                "sha256": item.sha256,
                "private": item.private,
            }
            for item in sorted(snapshot.records, key=lambda record: record.logical_path)
        ],
    }


def _finish_plan_from(
    context: FinishContext, inputs: _FinishInputs, before: Snapshot
) -> FinishPlan:
    rules = _planned_rules(before, inputs)
    licensed_private = inputs.licensed.private_json()
    licensed_commitment = hashlib.sha256(canonical_json(licensed_private)).hexdigest()
    pre_finish_commitment = hashlib.sha256(
        canonical_json(_private_snapshot_value(inputs.pre_finish))
    ).hexdigest()
    private_paths = {
        item.logical_path for item in inputs.pre_finish.records if item.private is True
    }
    patch_hashes = tuple(target.sha256 for target in inputs.patches.targets)
    document: dict[str, object] = {
        "schema": 1,
        "phase": "finish",
        "app_id": context.manifest.app_id,
        "manifest_sha256": context.manifest.sha256,
        "transaction_id": context.state.transaction_id,
        "prefix": {
            "root_identity": inputs.prefix.prefix_root_id,
            "system32_identity": inputs.prefix.system32_id,
        },
        "bundle_manifest_sha256": context.bundle_manifest_sha256,
        "compatibility_tool_tree_sha256": context.compatibility_tool_tree_sha256,
        "licensed": {
            "disposition": inputs.licensed.disposition,
            "identity": "[private local digest verified]",
            "private_action_sha256": licensed_commitment,
        },
        "pre_finish_sha256": pre_finish_commitment,
        "runtime": {
            "disposition": inputs.runtime.disposition,
            "evidence_sha256": inputs.runtime.evidence_sha256,
        },
        "runtime_plan_sha256": inputs.runtime.sha256,
        "patch": {
            "disposition": inputs.patches.disposition,
            "states": list(inputs.patches.states),
        },
        "patch_after_sha256": list(patch_hashes),
        "snapshot_rules": [
            {
                "logical_path": rule.logical_path,
                "policy": rule.policy,
                "after_sha256": (
                    "[private local digest verified]"
                    if rule.logical_path in private_paths
                    else rule.after_sha256
                ),
                "after_mode": rule.after_mode,
            }
            for rule in rules
        ],
        "commands": dict(inputs.command_versions),
    }
    return FinishPlan(
        document=document,
        sha256=plan_digest(document),
        runtime_plan_sha256=inputs.runtime.sha256,
        patch_after_sha256=patch_hashes,
        snapshot_rules=rules,
    )


def _read_before_snapshot(context: FinishContext) -> Snapshot:
    path = context.host.state_root / context.state.transaction_id / "before.json"
    before = read_snapshot(path)
    return _validate_finish_snapshot(before, context, label="before")


def _read_transaction_json(state: BootstrapState, name: str) -> dict[str, object]:
    if state._state_root is None:
        raise BootstrapError("bootstrap state is not bound to storage")
    root_fd = open_owned_root(state._state_root)
    transaction_fd: int | None = None
    try:
        transaction_fd = os.open(state.transaction_id, _DIRECTORY, dir_fd=root_fd)
        value = read_private_json(transaction_fd, name)
    finally:
        if transaction_fd is not None:
            os.close(transaction_fd)
        os.close(root_fd)
    if not isinstance(value, dict):
        raise BootstrapError(f"bootstrap {name} is invalid")
    return value


def finish_context_from_prepare(
    context: PrepareContext,
    state: BootstrapState,
    threading_dll: Path | None,
) -> FinishContext:
    """Reconstruct only the private, digest-bound inputs recorded by Prepare."""
    _validate_context(context)
    if (
        not isinstance(state, BootstrapState)
        or state.phase is not BootstrapPhase.AWAITING_STEAM_PREFIX
        or state._state_root != context.host.state_root
        or state.manifest_sha256 != context.manifest.sha256
    ):
        raise BootstrapError("Finish requires the recorded Prepare checkpoint")
    record = _read_transaction_json(state, "prepare.json")
    expected_keys = {
        "schema",
        "plan_sha256",
        "ge_root",
        "bundle_root",
        "bundle_manifest_sha256",
        "compatibility_tool",
    }
    if set(record) != expected_keys or record.get("schema") != 1:
        raise BootstrapError("Prepare checkpoint is invalid")
    prepare_plan_sha256 = record.get("plan_sha256")
    if (
        not isinstance(prepare_plan_sha256, str)
        or _SHA256.fullmatch(prepare_plan_sha256) is None
    ):
        raise BootstrapError("Prepare checkpoint is invalid")
    prepare_plan = _transaction_json_if_present(state, "prepare-plan.json")
    if prepare_plan is None:
        prepare_plan = _read_transaction_json(state, "plan.json")
    if (
        prepare_plan.get("schema") != 1
        or prepare_plan.get("phase") != "prepare"
        or prepare_plan.get("manifest_sha256") != context.manifest.sha256
        or plan_digest(prepare_plan) != prepare_plan_sha256
    ):
        raise BootstrapError("Prepare checkpoint does not match its plan")
    bundle = record.get("bundle_root")
    ge = record.get("ge_root")
    tool = record.get("compatibility_tool")
    identity = tool.get("identity") if isinstance(tool, dict) else None
    if (
        not isinstance(bundle, dict)
        or set(bundle) != {"root", "relative"}
        or bundle.get("root") != "cache"
        or not isinstance(bundle.get("relative"), str)
        or not isinstance(tool, dict)
        or set(tool) != {"disposition", "identity", "tree_sha256"}
        or tool.get("disposition") not in {"created", "adopted"}
        or tool.get("disposition") != state.compatibility_tool_disposition
        or not isinstance(identity, dict)
        or set(identity)
        != {
            "name",
            "base_release",
            "vdf_sha256",
            "managed_tree_sha256",
            "marker_sha256",
        }
        or identity.get("name") != _FM_NAME
        or identity.get("base_release") != context.manifest.ge.release
        or any(
            not isinstance(identity.get(name), str)
            or _SHA256.fullmatch(identity[name]) is None
            for name in ("vdf_sha256", "managed_tree_sha256")
        )
        or (
            identity.get("marker_sha256") is not None
            and (
                not isinstance(identity.get("marker_sha256"), str)
                or _SHA256.fullmatch(identity["marker_sha256"]) is None
            )
        )
        or not isinstance(tool.get("tree_sha256"), str)
        or _SHA256.fullmatch(tool["tree_sha256"]) is None
        or not isinstance(record.get("bundle_manifest_sha256"), str)
        or _SHA256.fullmatch(record["bundle_manifest_sha256"]) is None
    ):
        raise BootstrapError("Prepare checkpoint is invalid")
    if not isinstance(ge, dict) or set(ge) != {"root", "relative"}:
        raise BootstrapError("Prepare checkpoint is invalid")
    if ge.get("root") == "cache":
        ge_relative = ge.get("relative")
        if not isinstance(ge_relative, str):
            raise BootstrapError("Prepare checkpoint is invalid")
        ge_path = context.host.cache_root / Path(ge_relative)
        if (
            _validated_acquisition_reference(
                context, ge_path, "recorded GE-Proton root"
            )
            != ge
        ):
            raise BootstrapError("Prepare checkpoint is invalid")
    elif ge != {
        "root": "steam",
        "relative": f"compatibilitytools.d/{_FM_NAME}",
    }:
        raise BootstrapError("Prepare checkpoint is invalid")
    relative = Path(bundle["relative"])
    if (
        relative.is_absolute()
        or relative.as_posix() != bundle["relative"]
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise BootstrapError("Prepare checkpoint is invalid")
    bundle_root = context.host.cache_root / relative
    reference = _validated_acquisition_reference(
        context, bundle_root, "recorded bundle root"
    )
    if reference != bundle:
        raise BootstrapError("Prepare checkpoint is invalid")
    return FinishContext(
        manifest=context.manifest,
        host=context.host,
        repository_root=context.repository_root,
        state=state,
        threading_dll=threading_dll,
        bundle_root=bundle_root,
        bundle_manifest_sha256=record["bundle_manifest_sha256"],
        compatibility_tool_tree_sha256=tool["tree_sha256"],
        output=context.output,
        probe=context.probe,
    )


def build_finish_plan(context: FinishContext) -> FinishPlan:
    """Create the single public Phase B plan plus private in-memory bindings."""
    _validate_finish_context(context)
    operations = _finish_operations(context)
    before = _read_before_snapshot(context)
    inputs = _collect_finish_inputs(context, operations, record_events=False)
    return _finish_plan_from(context, inputs, before)


def _append_finish_boundary(
    state: BootstrapState,
    boundary: str,
    **updates: object,
) -> BootstrapState:
    return transition(
        state,
        BootstrapPhase.INSTALLING_FINISH,
        BootstrapPhase.INSTALLING_FINISH,
        completed_boundaries=(*state.completed_boundaries, boundary),
        **updates,
    )


def _comparison_value(comparison: Comparison) -> dict[str, object]:
    def records(values: tuple[ComparedRecord, ...]) -> list[object]:
        result: list[object] = []
        for item in values:
            before = item.before
            after = item.after
            result.append(
                {
                    "logical_path": item.logical_path,
                    "policy": item.policy,
                    "before": {
                        "state": before.state,
                        "mode": before.mode,
                        "size": before.size,
                        "sha256": before.sha256,
                        "private": before.private,
                    },
                    "after": {
                        "state": after.state,
                        "mode": after.mode,
                        "size": after.size,
                        "sha256": after.sha256,
                        "private": after.private,
                    },
                }
            )
        return result

    return {
        "schema": 1,
        "ok": comparison.ok,
        "expected_changes": records(comparison.expected_changes),
        "required_unchanged": records(comparison.required_unchanged),
        "unexpected_changes": records(comparison.unexpected_changes),
    }


def _require_finish_lease(context: FinishContext) -> None:
    lease_fd = context.launcher_lease_fd
    if type(lease_fd) is not int:
        raise BootstrapError("Finish launcher lease is missing")
    _validate_held_launcher_lock(context.host, lease_fd)


def _finish_under_lease(
    context: FinishContext,
    operations: FinishOperations,
    plan: FinishPlan,
) -> BootstrapState:
    _require_finish_lease(context)
    revalidated_before = _read_before_snapshot(context)
    revalidated = _collect_finish_inputs(context, operations, record_events=False)
    if _finish_plan_from(context, revalidated, revalidated_before) != plan:
        raise BootstrapError(
            "Finish inputs changed after confirmation; no managed changes were made"
        )
    before = revalidated_before
    inputs = revalidated
    context = replace(
        context,
        command_sha256=dict(inputs.command_versions),
    )
    _require_finish_lease(context)

    write_snapshot(
        context.host.state_root / context.state.transaction_id / "pre-finish.json",
        inputs.pre_finish,
    )
    _write_transaction_json(
        context.state, "licensed-plan.json", inputs.licensed.private_json()
    )
    _write_transaction_json(context.state, "plan.json", plan.document)
    state = transition(
        context.state,
        BootstrapPhase.AWAITING_STEAM_PREFIX,
        BootstrapPhase.PLANNED_FINISH,
        completed_boundaries=(
            *context.state.completed_boundaries,
            "finish-plan-confirmed",
        ),
        finish_plan_sha256=plan.sha256,
    )
    state = transition(
        state,
        BootstrapPhase.PLANNED_FINISH,
        BootstrapPhase.INSTALLING_FINISH,
        completed_boundaries=(*state.completed_boundaries, "installing-finish"),
    )
    transaction = context.host.state_root / state.transaction_id
    recovery = transaction / "recovery"
    ensure_private_directory(recovery)

    _require_finish_lease(context)
    operations.validate_prefix(context)
    if inputs.licensed.disposition != "keep":
        operations.install_threading(context, inputs.licensed, recovery / "licensed")
    state = _append_finish_boundary(state, "licensed-installed")
    _record(operations, "install-threading")

    _require_finish_lease(context)
    operations.validate_prefix(context)
    child = (
        operations.install_runtime(context, inputs.runtime)
        if inputs.runtime.disposition != "keep"
        else None
    )
    if child is not None and (
        not isinstance(child, str) or re.fullmatch(r"[0-9a-f]{24}", child) is None
    ):
        raise BootstrapError("runtime child returned an invalid transaction id")
    state = _append_finish_boundary(
        state,
        "runtime-installed",
        **({"child_runtime_transaction": child} if child is not None else {}),
    )
    _record(operations, "runtime-install")
    _require_finish_lease(context)
    operations.runtime_status(context, inputs.runtime, child)
    state = _append_finish_boundary(state, "runtime-status-verified")
    _record(operations, "runtime-status")

    _require_finish_lease(context)
    operations.validate_prefix(context)
    patch_root = recovery / "patches"
    if inputs.patches.disposition != "keep":
        ensure_private_directory(patch_root)
        patch_backup = operations.apply_patches(context, inputs.patches, patch_root)
    else:
        patch_backup = None
    if patch_backup is not None:
        if not isinstance(patch_backup, Path) or not patch_backup.is_absolute():
            raise BootstrapError("patcher returned an invalid backup manifest")
        try:
            relative_backup = patch_backup.relative_to(patch_root)
        except ValueError:
            raise BootstrapError(
                "patcher returned an invalid backup manifest"
            ) from None
        if len(relative_backup.parts) != 1:
            raise BootstrapError("patcher returned an invalid backup manifest")
        _owned_regular(patch_backup, "patch backup manifest")
        state = _append_finish_boundary(
            state,
            "patches-applied",
            patch_backup_manifest=str(patch_backup),
        )
    else:
        state = _append_finish_boundary(state, "patches-verified")
    _record(operations, "patch-apply")

    _require_finish_lease(context)
    operations.validate_prefix(context)
    doctor_output = operations.doctor(context)
    if not isinstance(doctor_output, str):
        raise BootstrapError("doctor returned invalid output")
    state = _append_finish_boundary(state, "doctor-passed")
    _record(operations, "doctor")

    _require_finish_lease(context)
    after = _validate_finish_snapshot(
        operations.capture_current(context), context, label="after"
    )
    write_snapshot(transaction / "after.json", after)
    state = _append_finish_boundary(state, "after-snapshot")
    _record(operations, "write-after-snapshot")
    _require_finish_lease(context)
    comparison = compare_snapshots(before, after, plan.snapshot_rules)
    _write_transaction_json(state, "comparison.json", _comparison_value(comparison))
    state = _append_finish_boundary(state, "snapshots-compared")
    _record(operations, "compare-snapshots")
    if not comparison.ok:
        raise BootstrapError("snapshot comparison found unexpected changes")

    _require_finish_lease(context)
    options = operations.steam_options(context)
    if (
        not isinstance(options, str)
        or not options.endswith("\n")
        or options.count("\n") != 1
    ):
        raise BootstrapError("Steam launch-options command returned invalid output")
    state = _append_finish_boundary(state, "steam-options-verified")
    _record(operations, "steam-options")
    _require_finish_lease(context)
    state = transition(
        state,
        BootstrapPhase.INSTALLING_FINISH,
        BootstrapPhase.READY_TO_ATTEMPT,
        completed_boundaries=(*state.completed_boundaries, "ready-to-attempt"),
    )
    context.output.write(render_comparison(comparison))
    context.output.write(options)
    context.output.write(
        "READY TO ATTEMPT. Paste the exact launch-options line into Steam and "
        "launch Forza yourself.\n"
    )
    return state


def finish(context: FinishContext, confirm: Callable[[str], str]) -> BootstrapState:
    """Execute one confirmed Finish plan without editing Steam or launching Forza."""
    _validate_finish_context(context)
    if not callable(confirm):
        raise BootstrapError("Finish confirmation callback is invalid")
    operations = _finish_operations(context)
    before = _read_before_snapshot(context)
    inputs = _collect_finish_inputs(context, operations, record_events=True)
    plan = _finish_plan_from(context, inputs, before)
    context.output.write(canonical_json(plan.document).decode("ascii") + "\n")
    context.output.write(f"PLAN_SHA256={plan.sha256}\n")
    _record(operations, "print-finish-plan")
    confirmation = confirm(plan.sha256)
    if (
        not isinstance(confirmation, str)
        or _SHA256.fullmatch(confirmation) is None
        or confirmation != plan.sha256
    ):
        raise BootstrapError(
            "Finish plan digest confirmation did not match; no managed changes were made"
        )
    _record(operations, "confirm-finish-plan")
    lease_fd = _acquire_launcher_lease(context.host)
    try:
        locked_context = replace(
            context,
            launcher_lease_fd=lease_fd,
        )
        return _finish_under_lease(locked_context, operations, plan)
    finally:
        os.close(lease_fd)


def _tool_record_from_state(state: BootstrapState) -> PublishedToolRecord:
    value = _read_transaction_json(state, "compatibility-tool.json")
    destination = value.get("destination")
    identity = value.get("identity")
    disposition = value.get("disposition")
    if (
        set(value)
        != {
            "schema",
            "destination",
            "disposition",
            "identity",
            "root_device",
            "root_inode",
            "published_tree_sha256",
        }
        or value.get("schema") != 1
        or destination
        != {
            "root": "steam",
            "relative": f"compatibilitytools.d/{_FM_NAME}",
        }
        or disposition not in {"created", "adopted"}
        or not isinstance(identity, dict)
        or set(identity)
        != {
            "name",
            "base_release",
            "vdf_sha256",
            "managed_tree_sha256",
            "marker_sha256",
        }
        or identity.get("name") != _FM_NAME
        or identity.get("base_release") != "GE-Proton11-3"
        or any(
            not isinstance(identity.get(name), str)
            or _SHA256.fullmatch(identity[name]) is None
            for name in ("vdf_sha256", "managed_tree_sha256")
        )
        or (
            identity.get("marker_sha256") is not None
            and (
                not isinstance(identity.get("marker_sha256"), str)
                or _SHA256.fullmatch(identity["marker_sha256"]) is None
            )
        )
        or type(value.get("root_device")) is not int
        or value["root_device"] < 0
        or type(value.get("root_inode")) is not int
        or value["root_inode"] < 0
        or not isinstance(value.get("published_tree_sha256"), str)
        or _SHA256.fullmatch(value["published_tree_sha256"]) is None
    ):
        raise BootstrapError("compatibility-tool recovery record is invalid")
    return PublishedToolRecord(
        Path("/"),
        ToolDisposition(disposition),
        ToolIdentity(
            identity["name"],
            identity["base_release"],
            identity["vdf_sha256"],
            identity["managed_tree_sha256"],
            identity["marker_sha256"],
        ),
        value["root_device"],
        value["root_inode"],
        value["published_tree_sha256"],
    )


def _resume_prepare_under_lease(
    context: RecoveryContext, state: BootstrapState
) -> BootstrapState:
    prepare_context = context.prepare_context
    if not isinstance(prepare_context, PrepareContext):
        raise BootstrapError("Prepare recovery context is missing")
    if state.phase is BootstrapPhase.NEW:
        raise BootstrapError("unconfirmed Prepare transaction requires recovery")
    operations = _operations(prepare_context)
    plan = _transaction_json_if_present(state, "prepare-plan.json")
    if plan is None:
        plan = _read_transaction_json(state, "plan.json")
    if (
        plan.get("schema") != 1
        or plan.get("phase") != "prepare"
        or plan.get("manifest_sha256") != state.manifest_sha256
    ):
        raise BootstrapError("Prepare recovery plan is invalid")
    destination = plan.get("destination")
    before_tool = destination.get("before") if isinstance(destination, dict) else None
    tool_record_value = _transaction_json_if_present(state, "compatibility-tool.json")
    before_path = context.state_root / state.transaction_id / "before.json"
    try:
        before_path.lstat()
    except FileNotFoundError:
        if (
            tool_record_value is not None
            or "compatibility-tool-published" in state.completed_boundaries
        ):
            raise BootstrapError(
                "Prepare recovery before snapshot is missing after publication"
            ) from None
        if before_tool == {"disposition": "absent"}:
            current_tool = inspect_fm(
                prepare_context.host.compatibility_tools / _FM_NAME,
                prepare_context.manifest,
            )
            if current_tool.disposition is not ToolDisposition.ABSENT:
                raise BootstrapError("compatibility-tool recovery is ambiguous")
        before = operations.capture_before(prepare_context, state.transaction_id)
        write_snapshot(before_path, before)
    except OSError:
        raise BootstrapError("Prepare recovery before snapshot is unsafe") from None
    else:
        try:
            before = read_snapshot(before_path)
        except BootstrapError:
            raise BootstrapError(
                "Prepare recovery before snapshot is malformed or unsafe"
            ) from None
    if (
        before.transaction_id != state.transaction_id
        or before.manifest_sha256 != state.manifest_sha256
    ):
        raise BootstrapError("Prepare recovery snapshot is invalid")

    bundle = operations.acquire_bundle(prepare_context)
    if not isinstance(bundle, VerifiedBundle):
        raise BootstrapError("bundle recovery returned an invalid root")
    bundle_reference = _validated_acquisition_reference(
        prepare_context, bundle.root, "bundle root"
    )
    ge_reference: dict[str, str]
    if tool_record_value is not None:
        tool_record = _tool_record_from_state(state)
        tool_record = replace(
            tool_record,
            destination=prepare_context.host.compatibility_tools / _FM_NAME,
        )
        current = inspect_fm(tool_record.destination, prepare_context.manifest)
        if (
            current.disposition is not ToolDisposition.ADOPTED
            or current.identity != tool_record.identity
            or current.tree_sha256 != tool_record.published_tree_sha256
        ):
            raise BootstrapError("compatibility tool changed during Prepare recovery")
        if tool_record.disposition is ToolDisposition.CREATED:
            try:
                current_info = tool_record.destination.lstat()
            except OSError:
                raise BootstrapError(
                    "compatibility tool changed during Prepare recovery"
                ) from None
            if (current_info.st_dev, current_info.st_ino) != (
                tool_record.root_device,
                tool_record.root_inode,
            ):
                raise BootstrapError(
                    "compatibility tool changed during Prepare recovery"
                )
        disposition = tool_record.disposition.value
        identity = tool_record.identity
        tree_sha256 = tool_record.published_tree_sha256
        ge_reference = {
            "root": "steam",
            "relative": f"compatibilitytools.d/{_FM_NAME}",
        }
    else:
        current = inspect_fm(
            prepare_context.host.compatibility_tools / _FM_NAME,
            prepare_context.manifest,
        )
        if current.disposition is ToolDisposition.ADOPTED:
            planned_identity = (
                before_tool.get("identity") if isinstance(before_tool, dict) else None
            )
            if (
                not isinstance(before_tool, dict)
                or before_tool.get("disposition") != "adopted"
                or not isinstance(current.identity, ToolIdentity)
                or not isinstance(current.tree_sha256, str)
                or planned_identity != _identity_value(current.identity)
                or before_tool.get("tree_sha256") != current.tree_sha256
            ):
                raise BootstrapError("compatibility-tool recovery is ambiguous")
            disposition = "adopted"
            identity = current.identity
            tree_sha256 = current.tree_sha256
            record = PublishedToolRecord(
                prepare_context.host.compatibility_tools / _FM_NAME,
                ToolDisposition.ADOPTED,
                identity,
                0,
                0,
                tree_sha256,
            )
            ge_reference = {
                "root": "steam",
                "relative": f"compatibilitytools.d/{_FM_NAME}",
            }
        elif current.disposition is ToolDisposition.ABSENT and before_tool == {
            "disposition": "absent"
        }:
            ge_root = operations.acquire_ge(prepare_context)
            ge_reference = _validated_acquisition_reference(
                prepare_context, ge_root, "GE-Proton root"
            )
            record = operations.publish_fm(prepare_context, ge_root)
            if not isinstance(record, PublishedToolRecord):
                raise BootstrapError(
                    "compatibility-tool recovery publication is invalid"
                )
            disposition = record.disposition.value
            identity = record.identity
            tree_sha256 = record.published_tree_sha256
        else:
            raise BootstrapError("compatibility-tool recovery is ambiguous")
        _write_transaction_json(
            state, "compatibility-tool.json", _published_tool_value(record)
        )

    prepare_record: dict[str, object] = {
        "schema": 1,
        "plan_sha256": plan_digest(plan),
        "ge_root": ge_reference,
        "bundle_root": bundle_reference,
        "bundle_manifest_sha256": bundle.manifest_sha256,
        "compatibility_tool": {
            "disposition": disposition,
            "identity": _identity_value(identity),
            "tree_sha256": tree_sha256,
        },
    }
    _write_transaction_json(state, "prepare.json", prepare_record)
    return transition(
        state,
        BootstrapPhase.PREPARING,
        BootstrapPhase.AWAITING_STEAM_PREFIX,
        completed_boundaries=(
            *state.completed_boundaries,
            *(
                ()
                if "awaiting-steam-prefix" in state.completed_boundaries
                else ("awaiting-steam-prefix",)
            ),
        ),
        compatibility_tool_disposition=disposition,
    )


def _resume_prepare_default(
    context: RecoveryContext, state: BootstrapState
) -> BootstrapState:
    prepare_context = context.prepare_context
    if not isinstance(prepare_context, PrepareContext):
        raise BootstrapError("Prepare recovery context is missing")
    lease_fd = _acquire_launcher_lease(prepare_context.host)
    try:
        _validate_held_launcher_lock(prepare_context.host, lease_fd)
        return _resume_prepare_under_lease(context, state)
    finally:
        os.close(lease_fd)


def _licensed_before_record(licensed: dict[str, object]) -> FileRecord:
    value = licensed.get("before")
    destination = licensed.get("destination_logical")
    if (
        not isinstance(value, dict)
        or set(value) != {"logical_path", "state", "mode", "size", "sha256", "private"}
        or not isinstance(destination, str)
        or not isinstance(value.get("logical_path"), str)
        or not value["logical_path"]
        or value.get("private") is not True
        or not isinstance(value.get("state"), str)
        or value.get("state") not in ("absent", "regular")
    ):
        raise BootstrapError("Finish recovery plan is invalid")
    state = value["state"]
    mode = value["mode"]
    size = value["size"]
    digest = value["sha256"]
    if state == "absent":
        if (mode, size, digest) != (None, None, None):
            raise BootstrapError("Finish recovery plan is invalid")
    elif (
        type(mode) is not int
        or not 0 <= mode <= 0o7777
        or type(size) is not int
        or size < 0
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
    ):
        raise BootstrapError("Finish recovery plan is invalid")
    return FileRecord(  # type: ignore[arg-type]
        value["logical_path"], state, mode, size, digest, True
    )


def _finish_plan_for_recovery(state: BootstrapState) -> FinishPlan:
    document = _read_transaction_json(state, "plan.json")
    licensed = _read_transaction_json(state, "licensed-plan.json")
    try:
        digest = plan_digest(document)
    except (TypeError, ValueError):
        raise BootstrapError("Finish recovery plan is invalid") from None
    document_keys = {
        "schema",
        "phase",
        "app_id",
        "manifest_sha256",
        "transaction_id",
        "prefix",
        "bundle_manifest_sha256",
        "compatibility_tool_tree_sha256",
        "licensed",
        "pre_finish_sha256",
        "runtime",
        "runtime_plan_sha256",
        "patch",
        "patch_after_sha256",
        "snapshot_rules",
        "commands",
    }
    if (
        set(document) != document_keys
        or document.get("schema") != 1
        or document.get("phase") != "finish"
        or document.get("app_id") != _APP_ID
        or document.get("manifest_sha256") != state.manifest_sha256
        or document.get("transaction_id") != state.transaction_id
        or not isinstance(state.finish_plan_sha256, str)
        or digest != state.finish_plan_sha256
        or not isinstance(document.get("runtime_plan_sha256"), str)
        or _SHA256.fullmatch(document["runtime_plan_sha256"]) is None
        or not isinstance(document.get("snapshot_rules"), list)
    ):
        raise BootstrapError("Finish recovery plan is invalid")
    prefix = document.get("prefix")
    public_licensed = document.get("licensed")
    runtime = document.get("runtime")
    patch = document.get("patch")
    commands = document.get("commands")
    patch_hashes = document.get("patch_after_sha256")
    if (
        not isinstance(prefix, dict)
        or set(prefix) != {"root_identity", "system32_identity"}
        or any(
            not isinstance(value, str)
            or re.fullmatch(r"(?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*)", value) is None
            for value in prefix.values()
        )
        or not isinstance(document.get("bundle_manifest_sha256"), str)
        or _SHA256.fullmatch(document["bundle_manifest_sha256"]) is None
        or not isinstance(document.get("compatibility_tool_tree_sha256"), str)
        or _SHA256.fullmatch(document["compatibility_tool_tree_sha256"]) is None
        or not isinstance(document.get("pre_finish_sha256"), str)
        or _SHA256.fullmatch(document["pre_finish_sha256"]) is None
        or not isinstance(public_licensed, dict)
        or set(public_licensed)
        != {
            "disposition",
            "identity",
            "private_action_sha256",
        }
        or not isinstance(public_licensed.get("disposition"), str)
        or public_licensed.get("disposition") not in ("install", "replace", "keep")
        or public_licensed.get("identity") != "[private local digest verified]"
        or not isinstance(public_licensed.get("private_action_sha256"), str)
        or _SHA256.fullmatch(public_licensed["private_action_sha256"]) is None
        or not isinstance(runtime, dict)
        or set(runtime) != {"disposition", "evidence_sha256"}
        or not isinstance(runtime.get("disposition"), str)
        or runtime.get("disposition") not in ("install", "keep")
        or not isinstance(runtime.get("evidence_sha256"), str)
        or _SHA256.fullmatch(runtime["evidence_sha256"]) is None
        or not isinstance(patch, dict)
        or set(patch) != {"disposition", "states"}
        or not isinstance(patch.get("disposition"), str)
        or patch.get("disposition") not in ("apply", "keep")
        or not isinstance(patch.get("states"), list)
        or not patch["states"]
        or any(
            not isinstance(value, str) or value not in ("original", "patched")
            for value in patch["states"]
        )
        or (patch["disposition"] == "apply" and set(patch["states"]) != {"original"})
        or (patch["disposition"] == "keep" and set(patch["states"]) != {"patched"})
        or not isinstance(patch_hashes, list)
        or len(patch_hashes) != len(patch["states"])
        or any(
            not isinstance(value, str) or _SHA256.fullmatch(value) is None
            for value in patch_hashes
        )
        or not isinstance(commands, dict)
        or set(commands)
        != {
            "runtime-installer",
            "patcher",
            "doctor",
            "steam-options",
        }
        or any(
            not isinstance(value, str) or _SHA256.fullmatch(value) is None
            for value in commands.values()
        )
    ):
        raise BootstrapError("Finish recovery plan is invalid")
    before = _licensed_before_record(licensed)
    if (
        set(licensed)
        != {
            "destination_logical",
            "source_size",
            "source_sha256",
            "before",
            "disposition",
        }
        or not isinstance(licensed.get("destination_logical"), str)
        or not os.path.isabs(licensed["destination_logical"])
        or "\x00" in licensed["destination_logical"]
        or type(licensed.get("source_size")) is not int
        or not 0 <= licensed["source_size"] <= MAX_LICENSED_SIZE
        or not isinstance(licensed.get("source_sha256"), str)
        or _SHA256.fullmatch(licensed["source_sha256"]) is None
        or not isinstance(licensed.get("disposition"), str)
        or licensed.get("disposition") not in ("install", "replace", "keep")
        or public_licensed["disposition"] != licensed["disposition"]
    ):
        raise BootstrapError("Finish recovery plan is invalid")
    try:
        licensed_digest = hashlib.sha256(canonical_json(licensed)).hexdigest()
    except (TypeError, ValueError):
        raise BootstrapError("Finish recovery plan is invalid") from None
    if licensed_digest != public_licensed["private_action_sha256"]:
        raise BootstrapError("Finish recovery plan is invalid")
    before_matches_source = (
        before.state == "regular"
        and before.size == licensed["source_size"]
        and before.sha256 == licensed["source_sha256"]
    )
    if not (
        (licensed["disposition"] == "install" and before.state == "absent")
        or (licensed["disposition"] == "keep" and before_matches_source)
        or (
            licensed["disposition"] == "replace"
            and before.state == "regular"
            and not before_matches_source
        )
    ):
        raise BootstrapError("Finish recovery plan is invalid")
    rules: list[ChangeRule] = []
    logical_paths: set[str] = set()
    for value in document["snapshot_rules"]:
        if not isinstance(value, dict) or set(value) != {
            "logical_path",
            "policy",
            "after_sha256",
            "after_mode",
        }:
            raise BootstrapError("Finish recovery plan is invalid")
        logical = value["logical_path"]
        policy = value["policy"]
        after = value["after_sha256"]
        if after == "[private local digest verified]":
            if logical != "prefix/threading":
                raise BootstrapError("Finish recovery plan is invalid")
            after = licensed["source_sha256"]
        if (
            not isinstance(logical, str)
            or not logical
            or logical.startswith("/")
            or any(part in {"", ".", ".."} for part in logical.split("/"))
            or logical in logical_paths
            or not isinstance(policy, str)
            or policy not in ("expected", "unchanged")
            or (
                after is not None
                and (not isinstance(after, str) or _SHA256.fullmatch(after) is None)
            )
            or (after is None) != (value["after_mode"] is None)
            or (
                value["after_mode"] is not None
                and (
                    type(value["after_mode"]) is not int
                    or not 0 <= value["after_mode"] <= 0o7777
                )
            )
        ):
            raise BootstrapError("Finish recovery plan is invalid")
        logical_paths.add(logical)
        rules.append(
            ChangeRule(
                logical,
                policy,
                after,
                value["after_mode"],
            )
        )
    if not rules:
        raise BootstrapError("Finish recovery plan is invalid")
    return FinishPlan(
        document,
        digest,
        document["runtime_plan_sha256"],
        tuple(patch_hashes),
        tuple(rules),
    )


def _append_finish_recovery_boundary(
    state: BootstrapState, boundary: str, **updates: object
) -> BootstrapState:
    if boundary in state.completed_boundaries and not updates:
        return state
    return transition(
        state,
        BootstrapPhase.INSTALLING_FINISH,
        BootstrapPhase.INSTALLING_FINISH,
        completed_boundaries=(
            state.completed_boundaries
            if boundary in state.completed_boundaries
            else (*state.completed_boundaries, boundary)
        ),
        **updates,
    )


def _licensed_action_for_recovery(
    context: FinishContext, state: BootstrapState
) -> LicensedAction:
    value = _read_transaction_json(state, "licensed-plan.json")
    before = _licensed_before_record(value)
    destination = (
        _prefix_directory(context)
        / "drive_c/windows/system32/xgameruntime.dll.threading"
    )
    if (
        value.get("destination_logical") != os.fspath(destination)
        or type(value.get("source_size")) is not int
        or not isinstance(value.get("source_sha256"), str)
        or not isinstance(value.get("disposition"), str)
        or value.get("disposition") not in ("install", "replace", "keep")
        or not isinstance(context.threading_dll, Path)
    ):
        raise BootstrapError("Finish recovery plan is invalid")
    return LicensedAction(
        value["destination_logical"],
        value["source_size"],
        value["source_sha256"],
        before,
        value["disposition"],
        context.threading_dll,
        destination,
    )


def _resume_finish_default(
    context: RecoveryContext, state: BootstrapState
) -> BootstrapState:
    finish_context = _recovery_finish_context(context, state)
    operations = _finish_operations(finish_context)
    plan = _finish_plan_for_recovery(state)
    lease_fd = _acquire_launcher_lease(finish_context.host)
    try:
        working = replace(
            finish_context,
            state=state,
            launcher_lease_fd=lease_fd,
        )
        current_prefix = operations.validate_prefix(working)
        _validate_prefix_inspection(current_prefix)
        recorded_prefix = plan.document["prefix"]
        assert isinstance(recorded_prefix, dict)
        if recorded_prefix != {
            "root_identity": current_prefix.prefix_root_id,
            "system32_identity": current_prefix.system32_id,
        }:
            raise BootstrapError("prefix identity changed during Finish recovery")
        if state.phase is BootstrapPhase.PLANNED_FINISH:
            state = transition(
                state,
                BootstrapPhase.PLANNED_FINISH,
                BootstrapPhase.INSTALLING_FINISH,
                completed_boundaries=(
                    *state.completed_boundaries,
                    *(
                        ()
                        if "installing-finish" in state.completed_boundaries
                        else ("installing-finish",)
                    ),
                ),
            )
            working = replace(working, state=state)
        transaction = context.state_root / state.transaction_id
        recovery = transaction / "recovery"
        ensure_private_directory(recovery)
        licensed_private = _read_transaction_json(state, "licensed-plan.json")
        licensed_action = _licensed_action_for_recovery(working, state)
        if licensed_action.private_json() != licensed_private:
            raise BootstrapError("Finish recovery plan is invalid")
        operations.install_threading(working, licensed_action, recovery / "licensed")
        state = _append_finish_recovery_boundary(state, "licensed-installed")

        runtime_document = plan.document.get("runtime")
        if not isinstance(runtime_document, dict):
            raise BootstrapError("Finish recovery plan is invalid")
        runtime = RuntimePlan(
            plan.runtime_plan_sha256,
            runtime_document.get("evidence_sha256"),
            (),
            runtime_document.get("disposition"),
        )
        child = _runtime_transaction_id(context, state)
        if runtime.disposition != "keep" and child is None:
            child = operations.install_runtime(working, runtime)
            if (
                not isinstance(child, str)
                or re.fullmatch(r"[0-9a-f]{24}", child) is None
            ):
                raise BootstrapError("runtime child returned an invalid transaction id")
        if child is not None and state.child_runtime_transaction is None:
            state = _append_finish_recovery_boundary(
                state, "runtime-installed", child_runtime_transaction=child
            )
        else:
            state = _append_finish_recovery_boundary(state, "runtime-installed")
        working = replace(working, state=state)
        operations.runtime_status(working, runtime, child)
        state = _append_finish_recovery_boundary(state, "runtime-status-verified")

        patch_document = plan.document.get("patch")
        assert isinstance(patch_document, dict)
        patches = PatchInspection(
            tuple(patch_document.get("states", ())),
            (),
            patch_document.get("disposition"),
        )
        patch_boundary = next(
            (
                boundary
                for boundary in ("patches-applied", "patches-verified")
                if boundary in state.completed_boundaries
            ),
            None,
        )
        if finish_context.operations is None:
            status = _default_patch_recovery_status(context, state)
            if patch_boundary is not None:
                expected = (
                    "pending"
                    if patch_boundary == "patches-applied"
                    else "not_applicable"
                )
                if status != expected:
                    raise BootstrapError("patch state changed during Finish recovery")
            elif status == "pending":
                backup = _patch_backup_path(context, state)
                assert backup is not None
                state = _append_finish_recovery_boundary(
                    state,
                    "patches-applied",
                    patch_backup_manifest=str(backup),
                )
                patch_boundary = "patches-applied"
            elif status == "not_applicable":
                state = _append_finish_recovery_boundary(state, "patches-verified")
                patch_boundary = "patches-verified"
            elif status != "rolled_back":
                raise BootstrapError("patch state is ambiguous during Finish recovery")
        if patch_boundary is None:
            patch_root = recovery / "patches"
            if patches.disposition != "keep":
                ensure_private_directory(patch_root)
                backup = operations.apply_patches(working, patches, patch_root)
            else:
                backup = None
            if backup is None:
                state = _append_finish_recovery_boundary(state, "patches-verified")
            else:
                if not isinstance(backup, Path) or not backup.is_absolute():
                    raise BootstrapError("patcher returned an invalid backup manifest")
                try:
                    relative_backup = backup.relative_to(patch_root)
                except ValueError:
                    raise BootstrapError(
                        "patcher returned an invalid backup manifest"
                    ) from None
                if len(relative_backup.parts) != 1:
                    raise BootstrapError("patcher returned an invalid backup manifest")
                _owned_regular(backup, "patch backup manifest")
                state = _append_finish_recovery_boundary(
                    state, "patches-applied", patch_backup_manifest=str(backup)
                )

        operations.doctor(working)
        state = _append_finish_recovery_boundary(state, "doctor-passed")
        after = _validate_finish_snapshot(
            operations.capture_current(working), working, label="after"
        )
        write_snapshot(transaction / "after.json", after)
        state = _append_finish_recovery_boundary(state, "after-snapshot")
        before = _read_before_snapshot(working)
        comparison = compare_snapshots(before, after, plan.snapshot_rules)
        _write_transaction_json(state, "comparison.json", _comparison_value(comparison))
        if not comparison.ok:
            raise BootstrapError("snapshot comparison found unexpected changes")
        state = _append_finish_recovery_boundary(state, "snapshots-compared")
        options = operations.steam_options(working)
        if (
            not isinstance(options, str)
            or not options.endswith("\n")
            or options.count("\n") != 1
        ):
            raise BootstrapError("Steam launch-options command returned invalid output")
        state = _append_finish_recovery_boundary(state, "steam-options-verified")
        return transition(
            state,
            BootstrapPhase.INSTALLING_FINISH,
            BootstrapPhase.READY_TO_ATTEMPT,
            completed_boundaries=(*state.completed_boundaries, "ready-to-attempt"),
        )
    finally:
        os.close(lease_fd)


def _recovery_finish_context(
    context: RecoveryContext, state: BootstrapState
) -> FinishContext:
    finish_context = context.finish_context
    if not isinstance(finish_context, FinishContext):
        raise BootstrapError("Finish recovery context is missing")
    plan = _read_transaction_json(state, "plan.json")
    commands = plan.get("commands")
    if (
        not isinstance(commands, dict)
        or set(commands) != {"runtime-installer", "patcher", "doctor", "steam-options"}
        or any(
            not isinstance(value, str) or _SHA256.fullmatch(value) is None
            for value in commands.values()
        )
    ):
        raise BootstrapError("Finish recovery command identities are invalid")
    return replace(
        finish_context,
        state=state,
        command_sha256=commands,
    )


def _has_finish_plan(state: BootstrapState) -> bool:
    plan = _transaction_json_if_present(state, "plan.json")
    return plan is not None and plan.get("phase") == "finish"


def _default_patch_recovery_status(
    context: RecoveryContext, state: BootstrapState
) -> RecoveryStatus:
    if state.patch_backup_manifest is None and not _has_finish_plan(state):
        return "not_applicable"
    finish_context = _recovery_finish_context(context, state)
    plan = _finish_plan_for_recovery(state)
    patch = plan.document["patch"]
    assert isinstance(patch, dict)
    disposition = patch["disposition"]
    states: list[str] = []
    for _logical, relative, _file in _PATCH_TARGETS:
        result = _run_finish_child(
            finish_context,
            (
                str(finish_context.repository_root / "scripts/patch-known-build"),
                "inspect",
                "--manifest",
                str(finish_context.repository_root / "manifests/supported-builds.toml"),
                "--target",
                str(finish_context.host.steam_root / relative),
            ),
            "patch rollback inspection",
            command="patcher",
        )
        value = result.stdout.rstrip("\n")
        if value not in {"original", "patched"}:
            raise BootstrapError("patch rollback inspection is ambiguous")
        states.append(value)
    if set(states) == {"original"}:
        if disposition != "apply":
            raise BootstrapError("adopted patch targets changed during recovery")
        if _patch_backup_path(context, state) is not None:
            raise BootstrapError("patch backup exists while targets are original")
        return "rolled_back"
    if set(states) != {"patched"}:
        raise BootstrapError("patch targets are mixed during rollback")
    if disposition == "keep":
        if _patch_backup_path(context, state) is not None:
            raise BootstrapError("adopted patches have an unexpected backup")
        return "not_applicable"
    backup = _patch_backup_path(context, state)
    if backup is None:
        raise BootstrapError("patch backup is missing")
    try:
        expected_parent = context.state_root / state.transaction_id / "recovery/patches"
        if backup.parent != expected_parent:
            raise BootstrapError("patch backup escaped its recovery directory")
        _owned_regular(backup, "patch backup manifest")
    except (OSError, BootstrapError):
        raise BootstrapError("patch backup is missing or unsafe") from None
    return "pending"


def _patch_backup_path(context: RecoveryContext, state: BootstrapState) -> Path | None:
    if state.patch_backup_manifest is not None:
        return Path(state.patch_backup_manifest)
    root = context.state_root / state.transaction_id / "recovery/patches"
    try:
        descriptor = open_owned_root(root)
    except FileNotFoundError:
        return None
    try:
        names = sorted(os.listdir(descriptor))
    except OSError:
        raise BootstrapError("patch recovery directory is unsafe") from None
    finally:
        os.close(descriptor)
    candidates = [
        root / name
        for name in names
        if re.fullmatch(
            r"forza-patch-[0-9]{8}T[0-9]{6}Z(?:\.[1-9][0-9]*)?\.json",
            name,
        )
    ]
    if len(candidates) > 1:
        raise BootstrapError("multiple patch backups require recovery")
    return candidates[0] if candidates else None


def _default_restore_patches(context: RecoveryContext, state: BootstrapState) -> None:
    backup = _patch_backup_path(context, state)
    if backup is None:
        raise BootstrapError("patch backup is missing")
    finish_context = _recovery_finish_context(context, state)
    _run_finish_child(
        finish_context,
        (
            str(finish_context.repository_root / "scripts/patch-known-build"),
            "restore-forza",
            "--manifest",
            str(finish_context.repository_root / "manifests/supported-builds.toml"),
            "--steam-root",
            str(finish_context.host.steam_root),
            "--backup-manifest",
            str(backup),
        ),
        "patch rollback",
        command="patcher",
    )


def _runtime_transaction_id(
    context: RecoveryContext, state: BootstrapState
) -> str | None:
    child = state.child_runtime_transaction
    if child is None and not _has_finish_plan(state):
        return None
    finish_context = _recovery_finish_context(context, state)
    if child is not None:
        return child
    plan = _read_transaction_json(state, "plan.json")
    expected_plan = plan.get("runtime_plan_sha256")
    root = (
        finish_context.host.user_root
        / ".local/state/forza-motorsport-linux/runtime-transactions"
    )
    try:
        root_fd = _open_owned_directory(root, "runtime transaction root")
    except FileNotFoundError:
        return None
    except OSError:
        raise BootstrapError("runtime transaction root is unsafe") from None
    try:
        names = sorted(os.listdir(root_fd))
    except OSError:
        os.close(root_fd)
        raise BootstrapError("runtime transaction root is unsafe") from None
    os.close(root_fd)
    matches: list[str] = []
    other_unfinished: list[str] = []
    for name in names:
        if re.fullmatch(r"[0-9a-f]{24}", name) is None:
            continue
        data = _read_owned_regular(
            root / name / "journal.json", "runtime child journal", 1024 * 1024
        )
        if data is None:
            continue
        try:
            value = json.loads(data)
        except (json.JSONDecodeError, UnicodeError):
            raise BootstrapError("runtime child journal is invalid") from None
        if (
            isinstance(value, dict)
            and value.get("transaction_id") == name
            and value.get("plan_sha256") == expected_plan
        ):
            if value.get("state") not in _TERMINAL_RUNTIME_STATES:
                matches.append(name)
        elif isinstance(value, dict) and value.get("state") not in {
            "accepted",
            "rolled_back",
            "runtime_restored",
        }:
            other_unfinished.append(name)
    if len(matches) > 1:
        raise BootstrapError("multiple matching runtime child transactions")
    if other_unfinished:
        raise BootstrapError("unrelated unfinished runtime child transaction exists")
    return matches[0] if matches else None


def _runtime_journal_value(
    context: RecoveryContext, state: BootstrapState
) -> dict[str, object] | None:
    child = _runtime_transaction_id(context, state)
    if child is None:
        return None
    finish_context = _recovery_finish_context(context, state)
    path = (
        finish_context.host.user_root
        / ".local/state/forza-motorsport-linux/runtime-transactions"
        / child
        / "journal.json"
    )
    data = _read_owned_regular(path, "runtime child journal", 1024 * 1024)
    if data is None:
        raise BootstrapError("runtime child journal is missing")
    try:
        value = json.loads(data)
    except (json.JSONDecodeError, UnicodeError):
        raise BootstrapError("runtime child journal is invalid") from None
    plan = _read_transaction_json(state, "plan.json")
    if (
        not isinstance(value, dict)
        or value.get("transaction_id") != child
        or value.get("plan_sha256") != plan.get("runtime_plan_sha256")
        or not isinstance(value.get("state"), str)
    ):
        raise BootstrapError("runtime child journal is invalid")
    return value


def _default_runtime_recovery_status(
    context: RecoveryContext, state: BootstrapState
) -> RecoveryStatus:
    journal = _runtime_journal_value(context, state)
    if journal is None:
        return "not_applicable"
    status = journal["state"]
    if status == "rolled_back":
        return "rolled_back"
    if status in {
        "prepared",
        "installing",
        "installed",
        "rolling_back",
        "runtime_restored",
    }:
        return "pending"
    if status == "recovery_required":
        raise BootstrapError("runtime child requires recovery")
    if status == "accepted":
        return "accepted"
    raise BootstrapError("runtime child journal has an unknown state")


def _default_rollback_runtime(context: RecoveryContext, state: BootstrapState) -> None:
    finish_context = _recovery_finish_context(context, state)
    _run_finish_child(
        finish_context,
        _runtime_argv(finish_context, "rollback"),
        "runtime child rollback",
        command="runtime-installer",
    )


def _licensed_journal_value(
    context: RecoveryContext, state: BootstrapState
) -> dict[str, object] | None:
    journal_root = context.state_root / state.transaction_id / "recovery/licensed"
    try:
        root_fd = open_owned_root(journal_root)
    except FileNotFoundError:
        return None
    try:
        value = read_private_json(root_fd, "licensed.json")
    finally:
        os.close(root_fd)
    if not isinstance(value, dict) or value.get("status") not in {
        "planned",
        "installed",
        "rolled_back",
        "recovery_required",
    }:
        raise BootstrapError("licensed recovery journal is invalid")
    return value


def _default_threading_recovery_status(
    context: RecoveryContext, state: BootstrapState
) -> RecoveryStatus:
    journal = _licensed_journal_value(context, state)
    if journal is None:
        return "not_applicable"
    status = journal["status"]
    if status == "rolled_back":
        return "rolled_back"
    if status == "recovery_required":
        raise BootstrapError("licensed destination requires recovery")
    return "pending"


def _default_rollback_threading(
    context: RecoveryContext, state: BootstrapState
) -> None:
    rollback_threading_copy(
        context.state_root / state.transaction_id / "recovery/licensed"
    )


def _default_tool_recovery_status(
    context: RecoveryContext, state: BootstrapState
) -> RecoveryStatus:
    record_value = _transaction_json_if_present(state, "compatibility-tool.json")
    if record_value is None and state.compatibility_tool_disposition is None:
        return "not_applicable"
    if record_value is None:
        raise BootstrapError("compatibility-tool recovery record is missing")
    record = _tool_record_from_state(state)
    finish_context = context.finish_context
    prepare_context = context.prepare_context
    if isinstance(finish_context, FinishContext):
        host = finish_context.host
        manifest = finish_context.manifest
    elif isinstance(prepare_context, PrepareContext) or (
        prepare_context is not None
        and hasattr(prepare_context, "host")
        and hasattr(prepare_context, "manifest")
    ):
        host = prepare_context.host
        manifest = prepare_context.manifest
    else:
        raise BootstrapError("compatibility-tool recovery context is missing")
    destination = host.compatibility_tools / _FM_NAME
    current = inspect_fm(destination, manifest)
    if record.disposition is ToolDisposition.ADOPTED:
        if (
            current.disposition is ToolDisposition.ADOPTED
            and current.identity == record.identity
            and current.tree_sha256 == record.published_tree_sha256
        ):
            return "not_applicable"
        raise BootstrapError("compatibility tool changed after publication")
    if current.disposition is ToolDisposition.ABSENT:
        return "rolled_back"
    if (
        current.disposition is not ToolDisposition.ADOPTED
        or current.identity != record.identity
        or current.tree_sha256 != record.published_tree_sha256
    ):
        raise BootstrapError("compatibility tool changed after publication")
    try:
        info = destination.lstat()
    except OSError:
        raise BootstrapError("compatibility tool changed after publication") from None
    if (info.st_dev, info.st_ino) != (record.root_device, record.root_inode):
        raise BootstrapError("compatibility tool changed after publication")
    return "pending"


def _default_rollback_tool(context: RecoveryContext, state: BootstrapState) -> None:
    record = _tool_record_from_state(state)
    finish_context = context.finish_context
    prepare_context = context.prepare_context
    host = (
        finish_context.host
        if isinstance(finish_context, FinishContext)
        else prepare_context.host  # type: ignore[union-attr]
    )
    result = rollback_published_fm(
        replace(record, destination=host.compatibility_tools / _FM_NAME)
    )
    if result not in {ToolDisposition.ABSENT, ToolDisposition.ADOPTED}:
        raise BootstrapError("compatibility tool changed during rollback")


def default_recovery_operations(context: RecoveryContext) -> RecoveryOperations:
    """Compose real resume and reverse-order rollback owners."""
    return RecoveryOperations(
        resume_prepare=_resume_prepare_default,
        resume_finish=_resume_finish_default,
        inspect_patches=_default_patch_recovery_status,
        restore_patches=_default_restore_patches,
        inspect_runtime=_default_runtime_recovery_status,
        rollback_runtime=_default_rollback_runtime,
        inspect_threading=_default_threading_recovery_status,
        rollback_threading=_default_rollback_threading,
        inspect_tool=_default_tool_recovery_status,
        rollback_tool=_default_rollback_tool,
    )


def _recovery_operations(context: RecoveryContext) -> RecoveryOperations:
    operations = context.operations
    if operations is None:
        operations = default_recovery_operations(context)
    if not isinstance(operations, RecoveryOperations):
        raise BootstrapError("recovery operations are unavailable")
    return operations


def _durable_recovery_state(context: RecoveryContext) -> BootstrapState:
    if not isinstance(context, RecoveryContext):
        raise BootstrapError("recovery context is invalid")
    if (
        not isinstance(context.state, BootstrapState)
        or not isinstance(context.state_root, Path)
        or not context.state_root.is_absolute()
        or context.state._state_root != context.state_root
    ):
        raise BootstrapError("recovery state is invalid")
    durable = load_transaction(context.state_root, context.state.transaction_id)
    if durable != context.state:
        raise BootstrapError("recovery does not match the last durable state")
    return durable


def _validate_awaiting_plan_handoff(state: BootstrapState) -> None:
    current = _transaction_json_if_present(state, "plan.json")
    if current is None or current.get("phase") != "finish":
        return
    prepare = _transaction_json_if_present(state, "prepare-plan.json")
    if (
        prepare is None
        or prepare.get("schema") != 1
        or prepare.get("phase") != "prepare"
        or prepare.get("manifest_sha256") != state.manifest_sha256
    ):
        raise BootstrapError(
            "Finish plan was published before its state transition and the Prepare plan "
            "cannot be reconstructed"
        )


def resume(context: RecoveryContext) -> BootstrapState:
    """Resume the sole durable coordinator through identity-inspecting owners."""
    state = _durable_recovery_state(context)
    operations = _recovery_operations(context)
    if state.phase in {
        BootstrapPhase.READY_TO_ATTEMPT,
        BootstrapPhase.ACCEPTED,
        BootstrapPhase.ROLLED_BACK,
    }:
        return state
    if state.phase is BootstrapPhase.AWAITING_STEAM_PREFIX:
        try:
            _validate_awaiting_plan_handoff(state)
        except BootstrapError as error:
            _recovery_required(context, state, error)
        return state
    try:
        if state.phase in {BootstrapPhase.NEW, BootstrapPhase.PREPARING}:
            result = operations.resume_prepare(context, state)
            allowed = {BootstrapPhase.AWAITING_STEAM_PREFIX}
        elif state.phase in {
            BootstrapPhase.PLANNED_FINISH,
            BootstrapPhase.INSTALLING_FINISH,
        }:
            result = operations.resume_finish(context, state)
            allowed = {BootstrapPhase.READY_TO_ATTEMPT}
        else:
            raise BootstrapError(
                f"bootstrap transaction requires rollback recovery from {state.phase.value}"
            )
    except BootstrapError as error:
        latest = load_transaction(context.state_root, state.transaction_id)
        _recovery_required(context, latest, error)
    if not isinstance(result, BootstrapState) or result.phase not in allowed:
        latest = load_transaction(context.state_root, state.transaction_id)
        _recovery_required(
            context,
            latest,
            BootstrapError("recovery owner returned an invalid state"),
        )
    durable = load_transaction(context.state_root, state.transaction_id)
    if durable != result:
        latest = load_transaction(context.state_root, state.transaction_id)
        _recovery_required(
            context,
            latest,
            BootstrapError("recovery owner did not persist its result"),
        )
    return result


def _rollback_checkpoint(state: BootstrapState, boundary: str) -> BootstrapState:
    if boundary in state.completed_boundaries:
        return state
    return transition(
        state,
        BootstrapPhase.ROLLING_BACK,
        BootstrapPhase.ROLLING_BACK,
        completed_boundaries=(*state.completed_boundaries, boundary),
    )


def _recovery_required(
    context: RecoveryContext, state: BootstrapState, error: BootstrapError
) -> None:
    if state.phase is not BootstrapPhase.RECOVERY_REQUIRED:
        state = transition(
            state,
            state.phase,
            BootstrapPhase.RECOVERY_REQUIRED,
        )
    journal = context.state_root / state.transaction_id
    context.output.write(f"RECOVERY_REQUIRED: inspect private journal {journal}\n")
    raise error


def _rollback_under_lease(context: RecoveryContext, confirm: str) -> BootstrapState:
    state = _durable_recovery_state(context)
    operations = _recovery_operations(context)
    if state.phase is BootstrapPhase.ACCEPTED:
        raise BootstrapError("accepted transaction refuses ordinary rollback")
    if state.phase is BootstrapPhase.ROLLED_BACK:
        return state
    if confirm != "ROLLBACK":
        return state
    steps = (
        ("rollback-patches", operations.inspect_patches, operations.restore_patches),
        ("rollback-runtime", operations.inspect_runtime, operations.rollback_runtime),
        (
            "rollback-threading",
            operations.inspect_threading,
            operations.rollback_threading,
        ),
        ("rollback-compat-tool", operations.inspect_tool, operations.rollback_tool),
    )
    preflight: dict[str, RecoveryStatus] = {}
    preflight_errors: list[BootstrapError] = []
    for boundary, inspect, _action in steps:
        try:
            status = inspect(context, state)
            if status not in {
                "pending",
                "rolled_back",
                "not_applicable",
                "accepted",
            }:
                raise BootstrapError(f"{boundary} inspection is ambiguous")
            preflight[boundary] = status
        except BootstrapError as error:
            preflight_errors.append(error)
    if preflight.get("rollback-runtime") == "accepted":
        raise BootstrapError("accepted runtime child refuses ordinary rollback")
    if preflight_errors:
        _recovery_required(context, state, preflight_errors[0])
    if state.phase is BootstrapPhase.RECOVERY_REQUIRED:
        state = transition(
            state,
            BootstrapPhase.RECOVERY_REQUIRED,
            BootstrapPhase.ROLLING_BACK,
        )
    elif state.phase is not BootstrapPhase.ROLLING_BACK:
        state = transition(state, state.phase, BootstrapPhase.ROLLING_BACK)

    for boundary, inspect, action in steps:
        try:
            status = inspect(context, state)
            if status not in {"pending", "rolled_back", "not_applicable"}:
                raise BootstrapError(f"{boundary} inspection is ambiguous")
            if status == "pending":
                action(context, state)
                status = inspect(context, state)
                if status not in {"rolled_back", "not_applicable"}:
                    raise BootstrapError(f"{boundary} did not reach an exact state")
            state = _rollback_checkpoint(state, boundary)
            operations.boundary(boundary)
        except KeyboardInterrupt:
            raise
        except BootstrapError as error:
            _recovery_required(context, state, error)
    return transition(
        state,
        BootstrapPhase.ROLLING_BACK,
        BootstrapPhase.ROLLED_BACK,
        completed_boundaries=(*state.completed_boundaries, "rolled-back"),
    )


def rollback(context: RecoveryContext, confirm: str) -> BootstrapState:
    """Compose exact owner rollbacks in reverse order under the launcher lease."""
    state = _durable_recovery_state(context)
    if state.phase is BootstrapPhase.ACCEPTED:
        raise BootstrapError("accepted transaction refuses ordinary rollback")
    if state.phase is BootstrapPhase.ROLLED_BACK or confirm != "ROLLBACK":
        return state
    finish_context = context.finish_context
    prepare_context = context.prepare_context
    host = (
        finish_context.host
        if isinstance(finish_context, FinishContext)
        else prepare_context.host
        if isinstance(prepare_context, PrepareContext)
        else None
    )
    if host is None:
        return _rollback_under_lease(context, confirm)
    lease_fd = _acquire_launcher_lease(host)
    try:
        _validate_held_launcher_lock(host, lease_fd)
        locked_finish = (
            replace(finish_context, launcher_lease_fd=lease_fd)
            if isinstance(finish_context, FinishContext)
            else None
        )
        return _rollback_under_lease(
            replace(context, finish_context=locked_finish), confirm
        )
    finally:
        os.close(lease_fd)
