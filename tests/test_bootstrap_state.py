# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from forza_bootstrap.model import BootstrapError
from forza_bootstrap.safeio import (
    DIRECTORY,
    acquire_bootstrap_lock,
    atomic_write_private_json,
    open_owned_root,
    read_private_json,
    rename_noreplace,
)
from forza_bootstrap.state import (
    BootstrapPhase,
    create_transaction,
    load_unfinished_transaction,
    transition,
)


def state_value(transaction_id: str, **updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "version": 1,
        "transaction_id": transaction_id,
        "manifest_sha256": "a" * 64,
        "phase": "NEW",
        "completed_boundaries": [],
        "child_runtime_transaction": None,
        "patch_backup_manifest": None,
        "compatibility_tool_disposition": None,
    }
    value.update(updates)
    return value


def write_transaction(root: Path, directory_name: str, value: dict[str, object]) -> None:
    transaction = root / directory_name
    transaction.mkdir(mode=0o700)
    transaction.chmod(0o700)
    state = transaction / "state.json"
    state.write_bytes(json.dumps(value, separators=(",", ":")).encode("ascii"))
    state.chmod(0o600)


def test_new_transaction_is_private_and_single(tmp_path: Path) -> None:
    state = create_transaction(tmp_path, "1" * 64)

    root = tmp_path / state.transaction_id
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "state.json").stat().st_mode) == 0o600
    assert load_unfinished_transaction(tmp_path) == state
    with pytest.raises(BootstrapError, match="unfinished bootstrap transaction"):
        create_transaction(tmp_path, "1" * 64)


@pytest.mark.parametrize("kind", ("symlink", "wrong_mode"))
def test_state_reader_rejects_symlink_and_wrong_mode(tmp_path: Path, kind: str) -> None:
    outside = tmp_path / "outside"
    outside.write_text("{}", encoding="ascii")
    state = tmp_path / "state.json"
    if kind == "symlink":
        state.symlink_to(outside)
        message = "symlink"
    else:
        state.write_text("{}", encoding="ascii")
        state.chmod(0o640)
        message = "mode"

    parent_fd = os.open(tmp_path, DIRECTORY)
    try:
        with pytest.raises(BootstrapError, match=message):
            read_private_json(parent_fd, "state.json")
    finally:
        os.close(parent_fd)


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (b"{", "JSON"),
        (json.dumps(state_value("b" * 24, version=2)).encode("ascii"), "state schema"),
        (json.dumps(state_value("b" * 24, extra=True)).encode("ascii"), "state schema"),
        (json.dumps(state_value("b" * 24, completed_boundaries=[1])).encode("ascii"), "completed_boundaries"),
    ),
)
def test_unfinished_state_rejects_corrupt_or_nonexact_schema(
    tmp_path: Path, payload: bytes, message: str
) -> None:
    transaction = tmp_path / ("b" * 24)
    transaction.mkdir(mode=0o700)
    transaction.chmod(0o700)
    state = transaction / "state.json"
    state.write_bytes(payload)
    state.chmod(0o600)

    with pytest.raises(BootstrapError, match=message):
        load_unfinished_transaction(tmp_path)


def test_unfinished_state_rejects_duplicate_transaction_ids(tmp_path: Path) -> None:
    transaction_id = "c" * 24
    write_transaction(tmp_path, transaction_id, state_value(transaction_id))
    write_transaction(tmp_path, "d" * 24, state_value(transaction_id))

    with pytest.raises(BootstrapError, match="duplicate transaction id"):
        load_unfinished_transaction(tmp_path)


def test_state_root_rejects_wrong_mode(tmp_path: Path) -> None:
    state_root = tmp_path / "bootstrap"
    state_root.mkdir(mode=0o755)
    state_root.chmod(0o755)

    with pytest.raises(BootstrapError, match="mode"):
        create_transaction(state_root, "1" * 64)


def test_open_owned_root_rejects_foreign_uid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from forza_bootstrap import safeio

    monkeypatch.setattr(
        safeio.os,
        "fstat",
        lambda _fd: SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=os.getuid() + 1),
    )

    with pytest.raises(BootstrapError, match="ownership"):
        open_owned_root(tmp_path)


def test_bootstrap_lock_rejects_concurrent_holder(tmp_path: Path) -> None:
    first = acquire_bootstrap_lock(tmp_path)
    try:
        with pytest.raises(BootstrapError, match="bootstrap lock"):
            acquire_bootstrap_lock(tmp_path)
    finally:
        os.close(first)


def test_atomic_private_json_fsyncs_file_before_rename_and_directory_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forza_bootstrap import safeio

    events: list[str] = []
    real_fsync = safeio.os.fsync
    real_replace = safeio.os.replace

    def record_fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def record_replace(*args: object, **kwargs: object) -> None:
        events.append("rename")
        real_replace(*args, **kwargs)

    monkeypatch.setattr(safeio.os, "fsync", record_fsync)
    monkeypatch.setattr(safeio.os, "replace", record_replace)
    parent_fd = os.open(tmp_path, DIRECTORY)
    try:
        atomic_write_private_json(parent_fd, "state.json", {"version": 1})
    finally:
        os.close(parent_fd)

    assert events.index("rename") > events.index("fsync")
    assert events[-1] == "fsync"


@pytest.mark.parametrize("when", ("before", "after"))
def test_atomic_private_json_leaves_a_readable_boundary_across_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    from forza_bootstrap import safeio

    parent_fd = os.open(tmp_path, DIRECTORY)
    try:
        atomic_write_private_json(parent_fd, "state.json", {"generation": "old"})
        real_replace = safeio.os.replace

        def interrupted_replace(*args: object, **kwargs: object) -> None:
            if when == "before":
                raise KeyboardInterrupt
            real_replace(*args, **kwargs)
            raise KeyboardInterrupt

        monkeypatch.setattr(safeio.os, "replace", interrupted_replace)
        with pytest.raises(KeyboardInterrupt):
            atomic_write_private_json(parent_fd, "state.json", {"generation": "new"})
        assert read_private_json(parent_fd, "state.json") == {
            "generation": "old" if when == "before" else "new"
        }
    finally:
        os.close(parent_fd)


def test_rename_noreplace_preserves_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_text("source", encoding="ascii")
    destination.write_text("destination", encoding="ascii")
    parent_fd = os.open(tmp_path, DIRECTORY)
    try:
        with pytest.raises(FileExistsError):
            rename_noreplace(parent_fd, "source", parent_fd, "destination")
    finally:
        os.close(parent_fd)

    assert source.read_text(encoding="ascii") == "source"
    assert destination.read_text(encoding="ascii") == "destination"


def test_transition_requires_last_durable_phase(tmp_path: Path) -> None:
    state = create_transaction(tmp_path, "2" * 64)

    with pytest.raises(BootstrapError, match="expected NEW"):
        transition(state, BootstrapPhase.PREPARING, BootstrapPhase.AWAITING_STEAM_PREFIX)


def test_transition_persists_only_allowed_typed_updates(tmp_path: Path) -> None:
    state = create_transaction(tmp_path, "3" * 64)
    prepared = transition(
        state,
        BootstrapPhase.NEW,
        BootstrapPhase.PREPARING,
        completed_boundaries=("preflight",),
    )

    assert prepared.phase is BootstrapPhase.PREPARING
    assert load_unfinished_transaction(tmp_path) == prepared
    with pytest.raises(BootstrapError, match="allowed"):
        transition(
            prepared,
            BootstrapPhase.PREPARING,
            BootstrapPhase.AWAITING_STEAM_PREFIX,
            manifest_sha256="4" * 64,
        )


def test_create_recovers_from_crash_before_initial_state_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forza_bootstrap import safeio

    real_replace = safeio.os.replace

    def interrupt_initial_state_publish(*args: object, **kwargs: object) -> None:
        if args[1] == "state.json":
            raise KeyboardInterrupt
        real_replace(*args, **kwargs)

    with monkeypatch.context() as interrupted:
        interrupted.setattr(safeio.os, "replace", interrupt_initial_state_publish)
        with pytest.raises(KeyboardInterrupt):
            create_transaction(tmp_path, "4" * 64)

    assert load_unfinished_transaction(tmp_path) is None
    resumed = create_transaction(tmp_path, "4" * 64)
    assert load_unfinished_transaction(tmp_path) == resumed


def test_patch_backup_manifest_accepts_exact_patcher_manifest_reference(tmp_path: Path) -> None:
    transaction_id = "e" * 24
    reference = "/private/forza-backups/forza-patch-20260903T123456Z.1.json"
    write_transaction(
        tmp_path,
        transaction_id,
        state_value(transaction_id, patch_backup_manifest=reference),
    )

    assert load_unfinished_transaction(tmp_path).patch_backup_manifest == reference  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "reference",
    (
        "forza-patch-20260903T123456Z.json",
        "/private/forza-backups/../forza-patch-20260903T123456Z.json",
        "/private/forza-backups/not-a-patch-manifest.json",
        "/private/forza-backups/forza-patch-20260903T123456Z.json\x00suffix",
        "a" * 64,
    ),
)
def test_patch_backup_manifest_rejects_unsafe_or_wrong_reference(
    tmp_path: Path, reference: str
) -> None:
    transaction_id = "f" * 24
    write_transaction(
        tmp_path,
        transaction_id,
        state_value(transaction_id, patch_backup_manifest=reference),
    )

    with pytest.raises(BootstrapError, match="patch_backup_manifest"):
        load_unfinished_transaction(tmp_path)


def test_concurrent_transaction_creators_do_not_both_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forza_bootstrap import state as state_module

    entered = threading.Event()
    release = threading.Event()
    result: list[object] = []
    real_write = state_module.atomic_write_private_json

    def pause_initial_write(*args: object, **kwargs: object) -> None:
        entered.set()
        assert release.wait(timeout=2)
        real_write(*args, **kwargs)

    monkeypatch.setattr(state_module, "atomic_write_private_json", pause_initial_write)

    def first_creator() -> None:
        try:
            result.append(create_transaction(tmp_path, "5" * 64))
        except BootstrapError as error:  # pragma: no cover - asserted below
            result.append(error)

    worker = threading.Thread(target=first_creator)
    worker.start()
    assert entered.wait(timeout=2)
    with pytest.raises(BootstrapError, match="bootstrap lock"):
        create_transaction(tmp_path, "5" * 64)
    release.set()
    worker.join(timeout=2)

    assert len(result) == 1
    assert isinstance(result[0], type(load_unfinished_transaction(tmp_path)))
    assert load_unfinished_transaction(tmp_path) == result[0]


def test_bootstrap_lock_closes_existing_file_after_validation_failure(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    from forza_bootstrap import safeio

    closes: list[int] = []
    opens: list[object] = [FileExistsError(), 20]

    def open_existing(*_args: object, **_kwargs: object) -> int:
        result = opens.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result  # type: ignore[return-value]

    monkeypatch.setattr(safeio, "ensure_private_directory", lambda _path: None)
    monkeypatch.setattr(safeio, "open_owned_root", lambda _path: 10)
    monkeypatch.setattr(safeio.os, "open", open_existing)
    monkeypatch.setattr(safeio.os, "close", closes.append)
    monkeypatch.setattr(
        safeio,
        "_validate_private_regular",
        lambda *_args: (_ for _ in ()).throw(BootstrapError("unsafe lock")),
    )

    with pytest.raises(BootstrapError, match="unsafe lock"):
        acquire_bootstrap_lock("/private/runtime")

    assert 20 in closes


def test_bootstrap_lock_closes_file_after_flock_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    from forza_bootstrap import safeio

    closes: list[int] = []
    monkeypatch.setattr(safeio, "ensure_private_directory", lambda _path: None)
    monkeypatch.setattr(safeio, "open_owned_root", lambda _path: 10)
    monkeypatch.setattr(safeio.os, "open", lambda *args, **kwargs: 20)
    monkeypatch.setattr(safeio.os, "close", closes.append)
    monkeypatch.setattr(safeio.os, "fchmod", lambda *_args: None)
    monkeypatch.setattr(safeio.os, "fsync", lambda *_args: None)
    monkeypatch.setattr(safeio, "_validate_private_regular", lambda *_args: None)
    monkeypatch.setattr(
        safeio.fcntl,
        "flock",
        lambda *_args: (_ for _ in ()).throw(OSError("flock failed")),
    )

    with pytest.raises(OSError, match="flock failed"):
        acquire_bootstrap_lock("/private/runtime")

    assert 20 in closes


@pytest.mark.parametrize("transaction_id", ("../escape", "short", "a" * 25))
def test_open_transaction_rejects_invalid_id_before_filesystem_access(
    monkeypatch: pytest.MonkeyPatch, transaction_id: str
) -> None:
    from forza_bootstrap import state as state_module

    monkeypatch.setattr(
        state_module.os,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("filesystem accessed")),
    )

    with pytest.raises(BootstrapError, match="transaction_id"):
        state_module._open_transaction(99, transaction_id)
