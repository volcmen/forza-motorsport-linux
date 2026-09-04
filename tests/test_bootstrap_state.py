# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
import os
import socket
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
    load_latest_transaction_read_only,
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
        "finish_plan_sha256": None,
    }
    value.update(updates)
    return value


def write_transaction(
    root: Path, directory_name: str, value: dict[str, object]
) -> None:
    transaction = root / directory_name
    transaction.mkdir(mode=0o700)
    transaction.chmod(0o700)
    state = transaction / "state.json"
    state.write_bytes(json.dumps(value, separators=(",", ":")).encode("ascii"))
    state.chmod(0o600)


def write_prepublication_transaction(root: Path, transaction_id: str) -> None:
    write_transaction(
        root,
        f".bootstrap-staging-{transaction_id}",
        state_value(transaction_id),
    )


def assert_rejects_hostile_special_file_promptly(operation: object) -> None:
    outcome: list[BaseException] = []

    def invoke() -> None:
        try:
            operation()  # type: ignore[operator]
        except (BootstrapError, OSError) as error:
            outcome.append(error)

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    worker.join(timeout=0.25)

    assert not worker.is_alive(), "hostile special file blocked before validation"
    assert len(outcome) == 1
    assert isinstance(outcome[0], BootstrapError)


def test_new_transaction_is_private_and_single(tmp_path: Path) -> None:
    state = create_transaction(tmp_path, "1" * 64)

    root = tmp_path / state.transaction_id
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "state.json").stat().st_mode) == 0o600
    assert load_unfinished_transaction(tmp_path) == state
    with pytest.raises(BootstrapError, match="unfinished bootstrap transaction"):
        create_transaction(tmp_path, "1" * 64)


def test_read_only_latest_transaction_does_not_create_missing_state_root(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "missing-bootstrap"

    assert load_latest_transaction_read_only(state_root) is None
    assert not state_root.exists()


def test_read_only_latest_transaction_prefers_the_current_durable_transaction(
    tmp_path: Path,
) -> None:
    first = create_transaction(tmp_path, "1" * 64)
    first = transition(first, BootstrapPhase.NEW, BootstrapPhase.ROLLING_BACK)
    transition(first, BootstrapPhase.ROLLING_BACK, BootstrapPhase.ROLLED_BACK)
    current = create_transaction(tmp_path, "1" * 64)
    old_mtime = 1_700_000_000_000_000_000
    os.utime(tmp_path / current.transaction_id / "state.json", ns=(old_mtime,) * 2)

    assert load_latest_transaction_read_only(tmp_path) == current


def test_read_only_latest_transaction_selects_newest_terminal_without_writes(
    tmp_path: Path,
) -> None:
    first = create_transaction(tmp_path, "1" * 64)
    first = transition(first, BootstrapPhase.NEW, BootstrapPhase.ROLLING_BACK)
    first = transition(first, BootstrapPhase.ROLLING_BACK, BootstrapPhase.ROLLED_BACK)
    second = create_transaction(tmp_path, "1" * 64)
    second = transition(second, BootstrapPhase.NEW, BootstrapPhase.ROLLING_BACK)
    second = transition(second, BootstrapPhase.ROLLING_BACK, BootstrapPhase.ROLLED_BACK)
    first_path = tmp_path / first.transaction_id / "state.json"
    second_path = tmp_path / second.transaction_id / "state.json"
    os.utime(first_path, ns=(1_700_000_000_000_000_000,) * 2)
    os.utime(second_path, ns=(1_700_000_001_000_000_000,) * 2)
    before = {
        path: (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes())
        for path in (first_path, second_path)
    }

    assert load_latest_transaction_read_only(tmp_path) == second
    assert {
        path: (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes())
        for path in (first_path, second_path)
    } == before


def test_read_only_latest_transaction_never_recovers_staging_state(
    tmp_path: Path,
) -> None:
    create_transaction(tmp_path, "1" * 64)
    transaction_id = "f" * 24
    write_prepublication_transaction(tmp_path, transaction_id)

    with pytest.raises(BootstrapError, match="interrupted publication"):
        load_latest_transaction_read_only(tmp_path)

    assert (tmp_path / f".bootstrap-staging-{transaction_id}").is_dir()
    assert not (tmp_path / transaction_id).exists()


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
        (
            json.dumps(state_value("b" * 24, completed_boundaries=[1])).encode("ascii"),
            "completed_boundaries",
        ),
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


def test_open_owned_root_rejects_foreign_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forza_bootstrap import safeio

    monkeypatch.setattr(
        safeio.os,
        "fstat",
        lambda _fd: SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o700, st_uid=os.getuid() + 1
        ),
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
        transition(
            state, BootstrapPhase.PREPARING, BootstrapPhase.AWAITING_STEAM_PREFIX
        )


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


def test_concurrent_transitions_from_same_durable_state_allow_only_one_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forza_bootstrap import state as state_module

    state = create_transaction(tmp_path, "a" * 64)
    entered_write = threading.Event()
    release_write = threading.Event()
    first_outcome: list[object] = []
    second_outcome: list[object] = []
    real_write = state_module.atomic_write_private_json

    def pause_first_writer(*args: object, **kwargs: object) -> None:
        if threading.current_thread().name == "first-transition":
            entered_write.set()
            assert release_write.wait(timeout=2)
        real_write(*args, **kwargs)

    monkeypatch.setattr(state_module, "atomic_write_private_json", pause_first_writer)

    def first_transition() -> None:
        try:
            first_outcome.append(
                transition(
                    state,
                    BootstrapPhase.NEW,
                    BootstrapPhase.PREPARING,
                    completed_boundaries=("first-writer",),
                )
            )
        except BootstrapError as error:  # pragma: no cover - asserted below
            first_outcome.append(error)

    worker = threading.Thread(target=first_transition, name="first-transition")
    worker.start()
    assert entered_write.wait(timeout=2)
    try:
        try:
            second_outcome.append(
                transition(
                    state,
                    BootstrapPhase.NEW,
                    BootstrapPhase.PREPARING,
                    completed_boundaries=("second-writer",),
                )
            )
        except BootstrapError as error:
            second_outcome.append(error)
    finally:
        release_write.set()
        worker.join(timeout=2)

    assert not worker.is_alive()
    assert len(first_outcome) == 1
    assert len(second_outcome) == 1
    assert not isinstance(first_outcome[0], BootstrapError)
    assert isinstance(second_outcome[0], BootstrapError)
    assert load_unfinished_transaction(tmp_path) == first_outcome[0]
    assert first_outcome[0].completed_boundaries == ("first-writer",)  # type: ignore[union-attr]


@pytest.mark.parametrize("descriptor_mode", ("same", "dup"))
def test_transition_locked_serializes_callers_sharing_root_lock_description(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    descriptor_mode: str,
) -> None:
    from forza_bootstrap import state as state_module

    state = create_transaction(tmp_path, "b" * 64)
    root_lock_fd = acquire_bootstrap_lock(tmp_path)
    second_lock_fd = root_lock_fd if descriptor_mode == "same" else os.dup(root_lock_fd)
    validated_callers = threading.Barrier(2, timeout=1)
    stale_writers = threading.Barrier(2, timeout=1)
    outcomes: dict[str, object] = {}
    real_assert_lock = state_module._assert_bootstrap_lock
    real_write = state_module.atomic_write_private_json

    def synchronize_root_validation(*args: object, **kwargs: object) -> None:
        real_assert_lock(*args, **kwargs)
        validated_callers.wait()

    def synchronize_stale_writers(*args: object, **kwargs: object) -> None:
        try:
            stale_writers.wait()
        except threading.BrokenBarrierError:
            pass
        real_write(*args, **kwargs)

    monkeypatch.setattr(
        state_module, "_assert_bootstrap_lock", synchronize_root_validation
    )
    monkeypatch.setattr(
        state_module, "atomic_write_private_json", synchronize_stale_writers
    )

    def invoke(boundary: str, lock_fd: int) -> None:
        try:
            outcomes[boundary] = state_module.transition_locked(
                state,
                BootstrapPhase.NEW,
                BootstrapPhase.PREPARING,
                lock_fd=lock_fd,
                completed_boundaries=(boundary,),
            )
        except BootstrapError as error:  # pragma: no cover - asserted below
            outcomes[boundary] = error

    first = threading.Thread(target=invoke, args=("first-writer", root_lock_fd))
    second = threading.Thread(target=invoke, args=("second-writer", second_lock_fd))
    try:
        first.start()
        second.start()
        first.join(timeout=3)
        second.join(timeout=3)
    finally:
        if second_lock_fd != root_lock_fd:
            os.close(second_lock_fd)
        os.close(root_lock_fd)

    assert not first.is_alive()
    assert not second.is_alive()
    successes = [
        outcome
        for outcome in outcomes.values()
        if not isinstance(outcome, BootstrapError)
    ]
    failures = [
        outcome for outcome in outcomes.values() if isinstance(outcome, BootstrapError)
    ]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], BootstrapError)
    assert "last durable state" in str(failures[0])
    winner = successes[0]
    assert load_unfinished_transaction(tmp_path) == winner
    assert winner.completed_boundaries in {("first-writer",), ("second-writer",)}  # type: ignore[union-attr]
    journal_info = (tmp_path / "journal.lock").lstat()
    assert stat.S_ISREG(journal_info.st_mode)
    assert journal_info.st_uid == os.getuid()
    assert stat.S_IMODE(journal_info.st_mode) == 0o600


def test_transition_locked_reuses_coordinator_lock_without_self_deadlock(
    tmp_path: Path,
) -> None:
    from forza_bootstrap import state as state_module

    state = create_transaction(tmp_path, "b" * 64)
    lock_fd = acquire_bootstrap_lock(tmp_path)
    try:
        prepared = state_module.transition_locked(
            state,
            BootstrapPhase.NEW,
            BootstrapPhase.PREPARING,
            lock_fd=lock_fd,
            completed_boundaries=("coordinator-owned-lock",),
        )
    finally:
        os.close(lock_fd)

    assert load_unfinished_transaction(tmp_path) == prepared


def test_same_phase_transition_appends_multiple_durable_checkpoints(
    tmp_path: Path,
) -> None:
    state = create_transaction(tmp_path, "c" * 64)
    state = transition(state, BootstrapPhase.NEW, BootstrapPhase.PREPARING)
    state = transition(
        state,
        BootstrapPhase.PREPARING,
        BootstrapPhase.AWAITING_STEAM_PREFIX,
    )
    state = transition(
        state,
        BootstrapPhase.AWAITING_STEAM_PREFIX,
        BootstrapPhase.PLANNED_FINISH,
    )
    state = transition(
        state,
        BootstrapPhase.PLANNED_FINISH,
        BootstrapPhase.INSTALLING_FINISH,
        completed_boundaries=("finish-started",),
    )

    first_checkpoint = transition(
        state,
        BootstrapPhase.INSTALLING_FINISH,
        BootstrapPhase.INSTALLING_FINISH,
        completed_boundaries=("finish-started", "runtime-installed"),
    )
    second_checkpoint = transition(
        first_checkpoint,
        BootstrapPhase.INSTALLING_FINISH,
        BootstrapPhase.INSTALLING_FINISH,
        completed_boundaries=(
            "finish-started",
            "runtime-installed",
            "patch-installed",
        ),
    )

    assert second_checkpoint.completed_boundaries == (
        "finish-started",
        "runtime-installed",
        "patch-installed",
    )
    assert load_unfinished_transaction(tmp_path) == second_checkpoint


def test_same_phase_transition_rejects_no_op(tmp_path: Path) -> None:
    state = create_transaction(tmp_path, "d" * 64)

    with pytest.raises(BootstrapError, match="no-op"):
        transition(state, BootstrapPhase.NEW, BootstrapPhase.NEW)

    assert load_unfinished_transaction(tmp_path) == state


@pytest.mark.parametrize(
    "boundaries",
    (
        ("snapshot", "preflight"),
        ("preflight",),
        ("preflight", "replacement"),
    ),
)
def test_transition_completed_boundaries_only_append_to_existing_order(
    tmp_path: Path, boundaries: tuple[str, ...]
) -> None:
    state = create_transaction(tmp_path, "e" * 64)
    prepared = transition(
        state,
        BootstrapPhase.NEW,
        BootstrapPhase.PREPARING,
        completed_boundaries=("preflight", "snapshot"),
    )

    with pytest.raises(BootstrapError, match="only append"):
        transition(
            prepared,
            BootstrapPhase.PREPARING,
            BootstrapPhase.AWAITING_STEAM_PREFIX,
            completed_boundaries=boundaries,
        )

    assert load_unfinished_transaction(tmp_path) == prepared


@pytest.mark.parametrize(
    ("field", "initial", "replacement"),
    (
        ("child_runtime_transaction", "1" * 24, "2" * 24),
        (
            "patch_backup_manifest",
            "/private/forza-backups/forza-patch-20260903T120000Z.json",
            "/private/forza-backups/forza-patch-20260903T120001Z.json",
        ),
        ("compatibility_tool_disposition", "created", "adopted"),
        ("finish_plan_sha256", "1" * 64, "2" * 64),
    ),
)
def test_transition_recovery_evidence_is_write_once_or_idempotently_equal(
    tmp_path: Path, field: str, initial: str, replacement: str
) -> None:
    state = create_transaction(tmp_path, "f" * 64)
    prepared = transition(
        state,
        BootstrapPhase.NEW,
        BootstrapPhase.PREPARING,
        **{field: initial},
    )
    idempotent = transition(
        prepared,
        BootstrapPhase.PREPARING,
        BootstrapPhase.AWAITING_STEAM_PREFIX,
        **{field: initial},
    )

    assert getattr(idempotent, field) == initial
    for forbidden in (None, replacement):
        with pytest.raises(BootstrapError, match="write-once"):
            transition(
                idempotent,
                BootstrapPhase.AWAITING_STEAM_PREFIX,
                BootstrapPhase.PLANNED_FINISH,
                **{field: forbidden},
            )
        assert load_unfinished_transaction(tmp_path) == idempotent


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


def test_patch_backup_manifest_accepts_exact_patcher_manifest_reference(
    tmp_path: Path,
) -> None:
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
    monkeypatch: pytest.MonkeyPatch,
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


def test_bootstrap_lock_closes_file_after_flock_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


def test_journal_lock_closes_existing_file_after_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from forza_bootstrap import safeio

    closes: list[int] = []
    opens: list[object] = [FileExistsError(), 20]

    def open_existing(*_args: object, **_kwargs: object) -> int:
        result = opens.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result  # type: ignore[return-value]

    monkeypatch.setattr(safeio, "open_owned_root", lambda _path: 10)
    monkeypatch.setattr(safeio.os, "open", open_existing)
    monkeypatch.setattr(safeio.os, "close", closes.append)
    monkeypatch.setattr(
        safeio,
        "_validate_private_regular",
        lambda *_args: (_ for _ in ()).throw(BootstrapError("unsafe journal lock")),
    )

    with pytest.raises(BootstrapError, match="unsafe journal lock"):
        safeio._acquire_journal_lock("/private/runtime")

    assert 20 in closes
    assert 10 in closes


def test_journal_lock_closes_file_after_flock_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from forza_bootstrap import safeio

    closes: list[int] = []
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

    with pytest.raises(
        BootstrapError, match="journal lock descriptor cannot be locked"
    ):
        safeio._acquire_journal_lock("/private/runtime")

    assert 20 in closes
    assert 10 in closes


@pytest.mark.parametrize("transaction_id", ("../escape", "short", "a" * 25))
def test_open_transaction_rejects_invalid_id_before_filesystem_access(
    monkeypatch: pytest.MonkeyPatch, transaction_id: str
) -> None:
    from forza_bootstrap import state as state_module

    monkeypatch.setattr(
        state_module.os,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("filesystem accessed")
        ),
    )

    with pytest.raises(BootstrapError, match="transaction_id"):
        state_module._open_transaction(99, transaction_id)


def test_prepublication_recovery_returns_the_final_transaction_on_first_and_later_load(
    tmp_path: Path,
) -> None:
    transaction_id = "6" * 24
    write_prepublication_transaction(tmp_path, transaction_id)

    first = load_unfinished_transaction(tmp_path)

    assert first is not None
    assert first.transaction_id == transaction_id
    assert load_unfinished_transaction(tmp_path) == first


@pytest.mark.parametrize("when", ("before", "after"))
def test_prepublication_recovery_resumes_after_crash_around_publish_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    from forza_bootstrap import state as state_module

    transaction_id = "7" * 24
    write_prepublication_transaction(tmp_path, transaction_id)
    real_rename = state_module.rename_noreplace

    def interrupt_recovery_rename(*args: object, **kwargs: object) -> None:
        if when == "before":
            raise KeyboardInterrupt
        real_rename(*args, **kwargs)
        raise KeyboardInterrupt

    with monkeypatch.context() as interrupted:
        interrupted.setattr(state_module, "rename_noreplace", interrupt_recovery_rename)
        with pytest.raises(KeyboardInterrupt):
            load_unfinished_transaction(tmp_path)

    recovered = load_unfinished_transaction(tmp_path)
    assert recovered is not None
    assert recovered.transaction_id == transaction_id
    assert load_unfinished_transaction(tmp_path) == recovered


def test_concurrent_loader_cannot_recover_while_creator_holds_root_lock(
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

    def creator() -> None:
        try:
            result.append(create_transaction(tmp_path, "8" * 64))
        except BootstrapError as error:  # pragma: no cover - asserted below
            result.append(error)

    worker = threading.Thread(target=creator)
    worker.start()
    assert entered.wait(timeout=2)
    try:
        with pytest.raises(BootstrapError, match="bootstrap lock"):
            load_unfinished_transaction(tmp_path)
    finally:
        release.set()
        worker.join(timeout=2)

    assert len(result) == 1
    assert load_unfinished_transaction(tmp_path) == result[0]


@pytest.mark.parametrize("disposition", ([], {}))
def test_state_rejects_unhashable_compatibility_tool_disposition(
    tmp_path: Path, disposition: object
) -> None:
    transaction_id = "9" * 24
    write_transaction(
        tmp_path,
        transaction_id,
        state_value(transaction_id, compatibility_tool_disposition=disposition),
    )

    with pytest.raises(BootstrapError, match="compatibility_tool_disposition"):
        load_unfinished_transaction(tmp_path)


def test_private_json_fifo_is_rejected_before_it_can_block(tmp_path: Path) -> None:
    fifo = tmp_path / "state.json"
    os.mkfifo(fifo, 0o600)

    def read_fifo() -> None:
        parent_fd = os.open(tmp_path, DIRECTORY)
        try:
            read_private_json(parent_fd, "state.json")
        finally:
            os.close(parent_fd)

    assert_rejects_hostile_special_file_promptly(read_fifo)


def test_staging_temp_fifo_is_rejected_before_it_can_block(tmp_path: Path) -> None:
    transaction_id = "a" * 24
    staging = tmp_path / f".bootstrap-staging-{transaction_id}"
    staging.mkdir(mode=0o700)
    staging.chmod(0o700)
    os.mkfifo(staging / f".state.json.{'b' * 24}.tmp", 0o600)

    assert_rejects_hostile_special_file_promptly(
        lambda: load_unfinished_transaction(tmp_path)
    )


def test_root_lock_fifo_is_rejected_before_it_can_block(tmp_path: Path) -> None:
    from forza_bootstrap import state as state_module

    os.mkfifo(tmp_path / "bootstrap.lock", 0o600)
    root_fd = os.open(tmp_path, DIRECTORY)
    try:
        assert_rejects_hostile_special_file_promptly(
            lambda: state_module._validate_root_lock(root_fd)
        )
    finally:
        os.close(root_fd)


def test_root_directory_symlink_is_a_bootstrap_error(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(BootstrapError, match="symlink"):
        load_unfinished_transaction(root)


def test_transaction_directory_symlink_is_a_bootstrap_error(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ("c" * 24)).symlink_to(outside, target_is_directory=True)

    with pytest.raises(BootstrapError, match="symlink"):
        load_unfinished_transaction(tmp_path)


def test_staging_directory_symlink_is_a_bootstrap_error(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / f".bootstrap-staging-{'d' * 24}").symlink_to(
        outside, target_is_directory=True
    )

    with pytest.raises(BootstrapError, match="symlink"):
        load_unfinished_transaction(tmp_path)


def test_staging_temp_symlink_is_a_bootstrap_error(tmp_path: Path) -> None:
    transaction_id = "e" * 24
    staging = tmp_path / f".bootstrap-staging-{transaction_id}"
    staging.mkdir(mode=0o700)
    staging.chmod(0o700)
    outside = tmp_path / "outside"
    outside.write_text("outside", encoding="ascii")
    (staging / f".state.json.{'f' * 24}.tmp").symlink_to(outside)

    with pytest.raises(BootstrapError, match="symlink"):
        load_unfinished_transaction(tmp_path)


def test_private_json_unix_socket_is_a_prompt_bootstrap_error(tmp_path: Path) -> None:
    private_socket = socket.socket(socket.AF_UNIX)
    private_socket.bind(str(tmp_path / "state.json"))
    (tmp_path / "state.json").chmod(0o600)

    def read_socket() -> None:
        parent_fd = os.open(tmp_path, DIRECTORY)
        try:
            read_private_json(parent_fd, "state.json")
        finally:
            os.close(parent_fd)

    try:
        assert_rejects_hostile_special_file_promptly(read_socket)
    finally:
        private_socket.close()


def test_bootstrap_lock_directory_is_a_prompt_bootstrap_error(tmp_path: Path) -> None:
    (tmp_path / "bootstrap.lock").mkdir(mode=0o700)

    assert_rejects_hostile_special_file_promptly(
        lambda: acquire_bootstrap_lock(tmp_path)
    )


@pytest.mark.parametrize("kind", ("fifo", "socket", "directory"))
def test_transition_locked_rejects_hostile_journal_lock_promptly(
    tmp_path: Path,
    kind: str,
) -> None:
    from forza_bootstrap import state as state_module

    state = create_transaction(tmp_path, "5" * 64)
    root_lock_fd = acquire_bootstrap_lock(tmp_path)
    journal_lock = tmp_path / "journal.lock"
    journal_socket: socket.socket | None = None
    if kind == "fifo":
        os.mkfifo(journal_lock, 0o600)
    elif kind == "socket":
        journal_socket = socket.socket(socket.AF_UNIX)
        journal_socket.bind(str(journal_lock))
        journal_lock.chmod(0o600)
    else:
        journal_lock.mkdir(mode=0o700)

    def invoke() -> None:
        state_module.transition_locked(
            state,
            BootstrapPhase.NEW,
            BootstrapPhase.PREPARING,
            lock_fd=root_lock_fd,
        )

    try:
        assert_rejects_hostile_special_file_promptly(invoke)
    finally:
        os.close(root_lock_fd)
        if journal_socket is not None:
            journal_socket.close()


@pytest.mark.parametrize("kind", ("symlink", "wrong_mode"))
def test_transition_locked_rejects_journal_lock_symlink_or_wrong_mode(
    tmp_path: Path,
    kind: str,
) -> None:
    from forza_bootstrap import state as state_module

    state = create_transaction(tmp_path, "6" * 64)
    journal_lock = tmp_path / "journal.lock"
    if kind == "symlink":
        outside = tmp_path / "outside-journal.lock"
        outside.write_text("", encoding="ascii")
        outside.chmod(0o600)
        journal_lock.symlink_to(outside)
        message = "symlink"
    else:
        journal_lock.write_text("", encoding="ascii")
        journal_lock.chmod(0o640)
        message = "mode"
    root_lock_fd = acquire_bootstrap_lock(tmp_path)
    try:
        with pytest.raises(BootstrapError, match=message):
            state_module.transition_locked(
                state,
                BootstrapPhase.NEW,
                BootstrapPhase.PREPARING,
                lock_fd=root_lock_fd,
            )
    finally:
        os.close(root_lock_fd)


def test_transition_locked_rejects_foreign_owned_journal_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from forza_bootstrap import safeio
    from forza_bootstrap import state as state_module

    state = create_transaction(tmp_path, "7" * 64)
    journal_lock = tmp_path / "journal.lock"
    journal_lock.write_text("", encoding="ascii")
    journal_lock.chmod(0o600)
    identity = (journal_lock.stat().st_dev, journal_lock.stat().st_ino)
    real_fstat = safeio.os.fstat

    def foreign_journal_owner(fd: int) -> object:
        info = real_fstat(fd)
        if (info.st_dev, info.st_ino) == identity:
            return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1)
        return info

    monkeypatch.setattr(safeio.os, "fstat", foreign_journal_owner)
    root_lock_fd = acquire_bootstrap_lock(tmp_path)
    try:
        with pytest.raises(BootstrapError, match="ownership"):
            state_module.transition_locked(
                state,
                BootstrapPhase.NEW,
                BootstrapPhase.PREPARING,
                lock_fd=root_lock_fd,
            )
    finally:
        os.close(root_lock_fd)


def test_root_lock_unix_socket_is_a_prompt_bootstrap_error(tmp_path: Path) -> None:
    from forza_bootstrap import state as state_module

    root_socket = socket.socket(socket.AF_UNIX)
    root_socket.bind(str(tmp_path / "bootstrap.lock"))
    (tmp_path / "bootstrap.lock").chmod(0o600)
    root_fd = os.open(tmp_path, DIRECTORY)
    try:
        assert_rejects_hostile_special_file_promptly(
            lambda: state_module._validate_root_lock(root_fd)
        )
    finally:
        os.close(root_fd)
        root_socket.close()


def test_staging_temp_unix_socket_is_a_prompt_bootstrap_error(tmp_path: Path) -> None:
    transaction_id = "3" * 24
    staging = tmp_path / f".bootstrap-staging-{transaction_id}"
    staging.mkdir(mode=0o700)
    staging.chmod(0o700)
    temporary = staging / f".state.json.{'4' * 24}.tmp"
    staging_socket = socket.socket(socket.AF_UNIX)
    staging_fd = os.open(staging, DIRECTORY)
    try:
        staging_socket.bind(f"/proc/self/fd/{staging_fd}/{temporary.name}")
    finally:
        os.close(staging_fd)
    temporary.chmod(0o600)
    try:
        assert_rejects_hostile_special_file_promptly(
            lambda: load_unfinished_transaction(tmp_path)
        )
    finally:
        staging_socket.close()
