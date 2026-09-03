# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Durable, private bootstrap transaction state."""

from __future__ import annotations

import errno
import os
import re
import secrets
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal

from .model import BootstrapError
from .safeio import (
    BOOTSTRAP_LOCK_NAME,
    DIRECTORY,
    _validate_private_directory,
    _validate_private_regular,
    acquire_bootstrap_lock,
    atomic_write_private_json,
    ensure_private_directory,
    open_owned_root,
    read_private_json,
    rename_noreplace,
)

_STATE_VERSION = 1
_TRANSACTION_ID = re.compile(r"[0-9a-f]{24}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PATCH_BACKUP_MANIFEST = re.compile(r"forza-patch-[0-9]{8}T[0-9]{6}Z(?:\.[1-9][0-9]*)?\.json\Z")
_PREPUBLICATION_DIRECTORY = re.compile(r"\.bootstrap-staging-([0-9a-f]{24})\Z")
_STATE_KEYS = frozenset(
    {
        "version",
        "transaction_id",
        "manifest_sha256",
        "phase",
        "completed_boundaries",
        "child_runtime_transaction",
        "patch_backup_manifest",
        "compatibility_tool_disposition",
    }
)


class BootstrapPhase(str, Enum):
    NEW = "NEW"
    PREPARING = "PREPARING"
    AWAITING_STEAM_PREFIX = "AWAITING_STEAM_PREFIX"
    PLANNED_FINISH = "PLANNED_FINISH"
    INSTALLING_FINISH = "INSTALLING_FINISH"
    READY_TO_ATTEMPT = "READY_TO_ATTEMPT"
    ACCEPTED = "ACCEPTED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


@dataclass(frozen=True)
class BootstrapState:
    version: int
    transaction_id: str
    manifest_sha256: str
    phase: BootstrapPhase
    completed_boundaries: tuple[str, ...]
    child_runtime_transaction: str | None
    patch_backup_manifest: str | None
    compatibility_tool_disposition: Literal["created", "adopted"] | None
    _state_root: Path | None = field(default=None, repr=False, compare=False)


_ALLOWED_TRANSITIONS = {
    BootstrapPhase.NEW: {
        BootstrapPhase.PREPARING,
        BootstrapPhase.ROLLING_BACK,
        BootstrapPhase.RECOVERY_REQUIRED,
    },
    BootstrapPhase.PREPARING: {
        BootstrapPhase.AWAITING_STEAM_PREFIX,
        BootstrapPhase.ROLLING_BACK,
        BootstrapPhase.RECOVERY_REQUIRED,
    },
    BootstrapPhase.AWAITING_STEAM_PREFIX: {
        BootstrapPhase.PLANNED_FINISH,
        BootstrapPhase.ROLLING_BACK,
        BootstrapPhase.RECOVERY_REQUIRED,
    },
    BootstrapPhase.PLANNED_FINISH: {
        BootstrapPhase.INSTALLING_FINISH,
        BootstrapPhase.ROLLING_BACK,
        BootstrapPhase.RECOVERY_REQUIRED,
    },
    BootstrapPhase.INSTALLING_FINISH: {
        BootstrapPhase.READY_TO_ATTEMPT,
        BootstrapPhase.ROLLING_BACK,
        BootstrapPhase.RECOVERY_REQUIRED,
    },
    BootstrapPhase.READY_TO_ATTEMPT: {
        BootstrapPhase.ACCEPTED,
        BootstrapPhase.ROLLING_BACK,
        BootstrapPhase.RECOVERY_REQUIRED,
    },
    BootstrapPhase.ACCEPTED: set(),
    BootstrapPhase.ROLLING_BACK: {
        BootstrapPhase.ROLLED_BACK,
        BootstrapPhase.RECOVERY_REQUIRED,
    },
    BootstrapPhase.ROLLED_BACK: set(),
    BootstrapPhase.RECOVERY_REQUIRED: {BootstrapPhase.ROLLING_BACK},
}
_TERMINAL_PHASES = frozenset({BootstrapPhase.ACCEPTED, BootstrapPhase.ROLLED_BACK})
_UPDATABLE_FIELDS = frozenset(
    {
        "completed_boundaries",
        "child_runtime_transaction",
        "patch_backup_manifest",
        "compatibility_tool_disposition",
    }
)


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise BootstrapError(f"state {label} must be a string")
    return value


def _require_token(value: object, label: str) -> str:
    result = _require_string(value, label)
    if _TRANSACTION_ID.fullmatch(result) is None:
        raise BootstrapError(f"state {label} is invalid")
    return result


def _require_sha256(value: object, label: str) -> str:
    result = _require_string(value, label)
    if _SHA256.fullmatch(result) is None:
        raise BootstrapError(f"state {label} must be a lowercase SHA-256")
    return result


def _require_patch_backup_manifest(value: object) -> str:
    result = _require_string(value, "patch_backup_manifest")
    if not os.path.isabs(result) or "\x00" in result:
        raise BootstrapError("state patch_backup_manifest is invalid")
    parts = result.split("/")[1:]
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise BootstrapError("state patch_backup_manifest is invalid")
    if _PATCH_BACKUP_MANIFEST.fullmatch(parts[-1]) is None:
        raise BootstrapError("state patch_backup_manifest is invalid")
    return result


def _state_from_value(value: object, state_root: Path) -> BootstrapState:
    if not isinstance(value, dict) or set(value) != _STATE_KEYS:
        raise BootstrapError("state schema is invalid")
    version = value["version"]
    if type(version) is not int or version != _STATE_VERSION:
        raise BootstrapError("state schema version is invalid")
    transaction_id = _require_token(value["transaction_id"], "transaction_id")
    manifest_sha256 = _require_sha256(value["manifest_sha256"], "manifest_sha256")
    try:
        phase = BootstrapPhase(_require_string(value["phase"], "phase"))
    except ValueError as error:
        raise BootstrapError("state phase is invalid") from error
    boundaries = value["completed_boundaries"]
    if (
        not isinstance(boundaries, list)
        or any(not isinstance(boundary, str) or not boundary for boundary in boundaries)
        or len(set(boundaries)) != len(boundaries)
    ):
        raise BootstrapError("state completed_boundaries is invalid")
    child = value["child_runtime_transaction"]
    if child is not None:
        child = _require_token(child, "child_runtime_transaction")
    backup = value["patch_backup_manifest"]
    if backup is not None:
        backup = _require_patch_backup_manifest(backup)
    disposition = value["compatibility_tool_disposition"]
    if disposition is not None and not isinstance(disposition, str):
        raise BootstrapError("state compatibility_tool_disposition is invalid")
    if disposition not in {None, "created", "adopted"}:
        raise BootstrapError("state compatibility_tool_disposition is invalid")
    return BootstrapState(
        version=version,
        transaction_id=transaction_id,
        manifest_sha256=manifest_sha256,
        phase=phase,
        completed_boundaries=tuple(boundaries),
        child_runtime_transaction=child,
        patch_backup_manifest=backup,
        compatibility_tool_disposition=disposition,
        _state_root=state_root,
    )


def _state_value(state: BootstrapState) -> dict[str, object]:
    return {
        "version": state.version,
        "transaction_id": state.transaction_id,
        "manifest_sha256": state.manifest_sha256,
        "phase": state.phase.value,
        "completed_boundaries": list(state.completed_boundaries),
        "child_runtime_transaction": state.child_runtime_transaction,
        "patch_backup_manifest": state.patch_backup_manifest,
        "compatibility_tool_disposition": state.compatibility_tool_disposition,
    }


def _open_private_child(root_fd: int, name: str, label: str) -> int:
    try:
        fd = os.open(name, DIRECTORY, dir_fd=root_fd)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise BootstrapError(f"symlink refused for {label}: {name}") from error
        raise
    try:
        _validate_private_directory(fd, label)
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_transaction(root_fd: int, transaction_id: str) -> int:
    _require_token(transaction_id, "transaction_id")
    return _open_private_child(root_fd, transaction_id, "bootstrap transaction directory")


def _validate_root_lock(root_fd: int) -> None:
    try:
        fd = os.open(BOOTSTRAP_LOCK_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise BootstrapError("symlink refused for bootstrap lock") from error
        raise
    try:
        _validate_private_regular(fd, BOOTSTRAP_LOCK_NAME)
    finally:
        os.close(fd)


def _prepublication_name(transaction_id: str) -> str:
    return f".bootstrap-staging-{transaction_id}"


def _recover_prepublication_transaction(
    root_fd: int, state_root: Path, name: str, transaction_id: str
) -> BootstrapState | None:
    transaction_fd = _open_private_child(root_fd, name, "bootstrap staging directory")
    try:
        entries = sorted(os.listdir(transaction_fd))
        if not entries:
            return None
        temporary = re.compile(r"\.state\.json\.[0-9a-f]{24}\.tmp\Z")
        if entries == ["state.json"]:
            state = _state_from_value(read_private_json(transaction_fd, "state.json"), state_root)
            if state.transaction_id != transaction_id:
                raise BootstrapError("staging directory and state transaction id differ")
        elif len(entries) == 1 and temporary.fullmatch(entries[0]) is not None:
            temporary_fd = os.open(entries[0], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=transaction_fd)
            try:
                _validate_private_regular(temporary_fd, entries[0])
            finally:
                os.close(temporary_fd)
            return None
        else:
            raise BootstrapError("bootstrap staging directory has unsafe contents")
    finally:
        os.close(transaction_fd)
    try:
        rename_noreplace(root_fd, name, root_fd, transaction_id)
    except FileExistsError as error:
        raise BootstrapError("staging transaction conflicts with durable transaction") from error
    os.fsync(root_fd)
    return state


def _load_transaction(root_fd: int, state_root: Path, transaction_id: str) -> BootstrapState:
    transaction_fd = _open_transaction(root_fd, transaction_id)
    try:
        state = _state_from_value(read_private_json(transaction_fd, "state.json"), state_root)
    finally:
        os.close(transaction_fd)
    return state


def _load_unfinished_transaction_locked(root: Path) -> BootstrapState | None:
    """Discover or recover state while the caller holds the root bootstrap lock."""
    root_fd = open_owned_root(root)
    try:
        try:
            names = sorted(os.listdir(root_fd))
        except OSError as error:
            raise BootstrapError("cannot list bootstrap state root") from error
        states: list[BootstrapState] = []
        transaction_ids: set[str] = set()
        for name in names:
            durable_name = name
            if name == BOOTSTRAP_LOCK_NAME:
                _validate_root_lock(root_fd)
                continue
            staging = _PREPUBLICATION_DIRECTORY.fullmatch(name)
            if staging is not None:
                durable_name = staging.group(1)
                state = _recover_prepublication_transaction(root_fd, root, name, durable_name)
                if state is None:
                    continue
            else:
                if _TRANSACTION_ID.fullmatch(name) is None:
                    raise BootstrapError(f"unsafe entry in bootstrap state root: {name}")
                state = _load_transaction(root_fd, root, name)
            if state.transaction_id in transaction_ids:
                raise BootstrapError("duplicate transaction id in bootstrap state")
            transaction_ids.add(state.transaction_id)
            if state.transaction_id != durable_name:
                raise BootstrapError("transaction directory and state transaction id differ")
            if state.phase not in _TERMINAL_PHASES:
                states.append(state)
        if len(states) > 1:
            raise BootstrapError("multiple unfinished bootstrap transactions")
        return states[0] if states else None
    finally:
        os.close(root_fd)


def load_unfinished_transaction(state_root: str | os.PathLike[str]) -> BootstrapState | None:
    """Return the only unfinished private transaction, if state has been initialized."""
    root = Path(state_root)
    try:
        root_fd = open_owned_root(root)
    except FileNotFoundError:
        return None
    else:
        os.close(root_fd)
    lock_fd = acquire_bootstrap_lock(root)
    try:
        return _load_unfinished_transaction_locked(root)
    finally:
        os.close(lock_fd)


def create_transaction(state_root: str | os.PathLike[str], manifest_sha256: str) -> BootstrapState:
    """Create the sole unfinished state directory and its initial durable record."""
    manifest_sha256 = _require_sha256(manifest_sha256, "manifest_sha256")
    root = Path(state_root)
    ensure_private_directory(root)
    lock_fd = acquire_bootstrap_lock(root)
    try:
        if _load_unfinished_transaction_locked(root) is not None:
            raise BootstrapError("unfinished bootstrap transaction already exists")
        root_fd = open_owned_root(root)
        try:
            for _ in range(100):
                transaction_id = secrets.token_hex(12)
                staging = _prepublication_name(transaction_id)
                try:
                    os.mkdir(staging, 0o700, dir_fd=root_fd)
                except FileExistsError:
                    continue
                os.fsync(root_fd)
                transaction_fd = _open_private_child(
                    root_fd, staging, "bootstrap staging directory"
                )
                try:
                    state = BootstrapState(
                        version=_STATE_VERSION,
                        transaction_id=transaction_id,
                        manifest_sha256=manifest_sha256,
                        phase=BootstrapPhase.NEW,
                        completed_boundaries=(),
                        child_runtime_transaction=None,
                        patch_backup_manifest=None,
                        compatibility_tool_disposition=None,
                        _state_root=root,
                    )
                    atomic_write_private_json(transaction_fd, "state.json", _state_value(state))
                finally:
                    os.close(transaction_fd)
                try:
                    rename_noreplace(root_fd, staging, root_fd, transaction_id)
                except FileExistsError as error:
                    raise BootstrapError("bootstrap transaction id collision") from error
                os.fsync(root_fd)
                return state
            raise BootstrapError("cannot reserve bootstrap transaction id")
        finally:
            os.close(root_fd)
    finally:
        os.close(lock_fd)


def transition(
    state: BootstrapState,
    expected: BootstrapPhase,
    target: BootstrapPhase,
    **updates: object,
) -> BootstrapState:
    """Persist one legal state transition only from the last durable state."""
    if not isinstance(expected, BootstrapPhase) or not isinstance(target, BootstrapPhase):
        raise BootstrapError("bootstrap transition phases are invalid")
    if state.phase != expected:
        raise BootstrapError(f"transition expected {state.phase.value}, not {expected.value}")
    if target not in _ALLOWED_TRANSITIONS[expected]:
        raise BootstrapError(f"transition from {expected.value} to {target.value} is not allowed")
    invalid_updates = set(updates) - _UPDATABLE_FIELDS
    if invalid_updates:
        raise BootstrapError("bootstrap state update is not allowed")
    if state._state_root is None:
        raise BootstrapError("bootstrap state is not bound to private storage")

    root_fd = open_owned_root(state._state_root)
    try:
        transaction_fd = _open_transaction(root_fd, state.transaction_id)
        try:
            durable = _state_from_value(
                read_private_json(transaction_fd, "state.json"), state._state_root
            )
            if durable != state:
                raise BootstrapError(
                    f"transition does not match last durable state; expected {durable.phase.value}"
                )
            value = _state_value(state)
            value["phase"] = target.value
            for key, update in updates.items():
                if key == "completed_boundaries" and isinstance(update, tuple):
                    value[key] = list(update)
                else:
                    value[key] = update
            next_state = _state_from_value(value, state._state_root)
            atomic_write_private_json(transaction_fd, "state.json", _state_value(next_state))
            return next_state
        finally:
            os.close(transaction_fd)
    finally:
        os.close(root_fd)
