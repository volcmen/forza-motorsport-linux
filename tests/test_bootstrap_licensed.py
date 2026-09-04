# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import forza_bootstrap.licensed as licensed_module
from forza_bootstrap.licensed import (
    MAX_LICENSED_SIZE,
    install_threading_copy,
    plan_threading_copy,
    rollback_threading_copy,
    validate_pe64,
)
from forza_bootstrap.model import BootstrapError


def make_synthetic_pe(
    path: Path,
    *,
    machine: int = 0x8664,
    pe_offset: int = 0x40,
    signature: bytes = b"PE\0\0",
    payload: bytes = b"synthetic licensed payload",
) -> Path:
    size = max(0x80, pe_offset + 6)
    data = bytearray(size)
    data[:2] = b"MZ"
    data[0x3C:0x40] = pe_offset.to_bytes(4, "little")
    if pe_offset + 6 <= len(data):
        data[pe_offset : pe_offset + 4] = signature
        data[pe_offset + 4 : pe_offset + 6] = machine.to_bytes(2, "little")
    data.extend(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def licensed_fixture(tmp_path: Path, *, before: bytes | None) -> tuple[Path, Path]:
    source = make_synthetic_pe(tmp_path / "input" / "licensed.dll")
    destination = tmp_path / "prefix" / "xgameruntime.dll.threading"
    destination.parent.mkdir(parents=True)
    if before is not None:
        destination.write_bytes(before)
        destination.chmod(0o640)
    return source, destination


def recovery_payloads(destination: Path) -> list[bytes]:
    return [
        path.read_bytes()
        for path in destination.parent.iterdir()
        if path.name.startswith(f".{destination.name}.")
        and (path.name.endswith(".recovery") or path.name.endswith(".rollback-held"))
    ]


def test_valid_pe64_is_identified_without_persisting_source_path(
    tmp_path: Path,
) -> None:
    source, destination = licensed_fixture(tmp_path, before=None)

    identity = validate_pe64(source)
    action = plan_threading_copy(source, destination)

    assert identity.size == source.stat().st_size
    assert identity.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert action.source_sha256 == identity.sha256
    assert str(source) not in json.dumps(action.private_json())
    assert str(source) not in repr(action)
    assert identity.sha256 not in action.public_summary()
    assert "[private local digest verified]" in action.public_summary()


@pytest.mark.parametrize("machine", [0x014C, 0xAA64])
def test_non_x86_64_pe_is_refused(tmp_path: Path, machine: int) -> None:
    source = make_synthetic_pe(tmp_path / "wrong.dll", machine=machine)

    with pytest.raises(BootstrapError, match="x86_64 PE"):
        validate_pe64(source)


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (lambda data: data.__setitem__(slice(0, 2), b"NO"), "DOS"),
        (
            lambda data: data.__setitem__(
                slice(0x3C, 0x40), (4096).to_bytes(4, "little")
            ),
            "offset",
        ),
        (lambda data: data.__setitem__(slice(0x40, 0x44), b"NOPE"), "signature"),
    ),
)
def test_malformed_pe_is_refused_without_disclosing_input(
    tmp_path: Path,
    mutate: object,
    message: str,
) -> None:
    source = make_synthetic_pe(tmp_path / "sensitive-name.dll")
    data = bytearray(source.read_bytes())
    mutate(data)  # type: ignore[operator]
    source.write_bytes(data)

    with pytest.raises(BootstrapError, match=message) as failure:
        validate_pe64(source)

    assert str(source) not in str(failure.value)


def test_too_short_pe_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "short.dll"
    source.write_bytes(b"MZ")

    with pytest.raises(BootstrapError, match="DOS"):
        validate_pe64(source)


def test_oversize_pe_is_refused_before_reading_payload(tmp_path: Path) -> None:
    source = tmp_path / "oversize.dll"
    with source.open("wb") as stream:
        stream.truncate(MAX_LICENSED_SIZE + 1)

    with pytest.raises(BootstrapError, match="size limit"):
        validate_pe64(source)


def test_relative_and_symlink_sources_are_refused(tmp_path: Path) -> None:
    source = make_synthetic_pe(tmp_path / "licensed.dll")
    link = tmp_path / "source-link"
    link.symlink_to(source)

    with pytest.raises(BootstrapError, match="absolute"):
        validate_pe64(Path("licensed.dll"))
    with pytest.raises(BootstrapError, match="symlink"):
        validate_pe64(link)


def test_source_must_be_owned_by_current_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_synthetic_pe(tmp_path / "licensed.dll")
    source_inode = source.stat().st_ino
    real_fstat = licensed_module.os.fstat

    def foreign_source(fd: int) -> object:
        info = real_fstat(fd)
        if info.st_ino != source_inode:
            return info
        return SimpleNamespace(
            st_mode=info.st_mode,
            st_uid=os.getuid() + 1,
            st_dev=info.st_dev,
            st_ino=info.st_ino,
            st_size=info.st_size,
            st_mtime_ns=info.st_mtime_ns,
            st_ctime_ns=info.st_ctime_ns,
        )

    monkeypatch.setattr(licensed_module.os, "fstat", foreign_source)

    with pytest.raises(BootstrapError, match="ownership"):
        validate_pe64(source)


def test_pe_header_and_digest_are_bound_to_one_file_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_synthetic_pe(tmp_path / "licensed.dll")
    real_pread = licensed_module.os.pread
    calls = 0

    def mutate_after_parsing(fd: int, size: int, offset: int) -> bytes:
        nonlocal calls
        result = real_pread(fd, size, offset)
        calls += 1
        if calls == 2:
            changed = bytearray(source.read_bytes())
            changed[:2] = b"NO"
            source.write_bytes(changed)
        return result

    monkeypatch.setattr(licensed_module.os, "pread", mutate_after_parsing)

    with pytest.raises(BootstrapError, match="changed"):
        validate_pe64(source)


def test_destination_symlink_is_refused_and_target_is_unchanged(tmp_path: Path) -> None:
    source, destination = licensed_fixture(tmp_path, before=None)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    destination.symlink_to(outside)

    with pytest.raises(BootstrapError, match="symlink"):
        plan_threading_copy(source, destination)

    assert outside.read_bytes() == b"outside"


def test_exact_existing_destination_is_a_noop(tmp_path: Path) -> None:
    source, destination = licensed_fixture(tmp_path, before=None)
    destination.write_bytes(source.read_bytes())
    inode = destination.stat().st_ino
    action = plan_threading_copy(source, destination)

    assert action.disposition == "keep"
    install_threading_copy(action, tmp_path / "journal")
    rollback_threading_copy(tmp_path / "journal")

    assert destination.stat().st_ino == inode
    assert destination.read_bytes() == source.read_bytes()
    assert not list(destination.parent.glob(f".{destination.name}.*"))


def test_exact_existing_destination_keeps_its_nonstandard_mode(tmp_path: Path) -> None:
    source, destination = licensed_fixture(tmp_path, before=None)
    destination.write_bytes(source.read_bytes())
    destination.chmod(0o777)
    inode = destination.stat().st_ino
    journal = tmp_path / "journal"

    install_threading_copy(plan_threading_copy(source, destination), journal)
    rollback_threading_copy(journal)

    assert destination.stat().st_ino == inode
    assert stat.S_IMODE(destination.stat().st_mode) == 0o777


def test_rollback_never_moves_a_changed_exact_existing_noop(tmp_path: Path) -> None:
    source, destination = licensed_fixture(tmp_path, before=None)
    destination.write_bytes(source.read_bytes())
    journal = tmp_path / "journal"
    install_threading_copy(plan_threading_copy(source, destination), journal)
    replacement = b"changed after no-op adoption"
    destination.write_bytes(replacement)

    with pytest.raises(BootstrapError, match="changed"):
        rollback_threading_copy(journal)

    assert destination.read_bytes() == replacement
    assert not list(destination.parent.glob(f".{destination.name}.*"))


def test_install_then_rollback_restores_exact_previous_destination(
    tmp_path: Path,
) -> None:
    source, destination = licensed_fixture(tmp_path, before=b"previous")
    action = plan_threading_copy(source, destination)

    install_threading_copy(action, tmp_path / "journal")
    assert destination.read_bytes() == source.read_bytes()
    rollback_threading_copy(tmp_path / "journal")

    assert destination.read_bytes() == b"previous"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o640


def test_install_then_rollback_removes_a_proven_new_destination(tmp_path: Path) -> None:
    source, destination = licensed_fixture(tmp_path, before=None)
    action = plan_threading_copy(source, destination)

    install_threading_copy(action, tmp_path / "journal")
    rollback_threading_copy(tmp_path / "journal")

    assert not destination.exists()
    assert source.read_bytes() in recovery_payloads(destination)


@pytest.mark.parametrize(
    ("rename_kind", "timing"),
    (
        ("backup", "before"),
        ("backup", "after"),
        ("publish", "before"),
        ("publish", "after"),
    ),
)
def test_install_resumes_after_interruption_at_every_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rename_kind: str,
    timing: str,
) -> None:
    source, destination = licensed_fixture(tmp_path, before=b"previous")
    action = plan_threading_copy(source, destination)
    real_rename = licensed_module.rename_noreplace
    interrupted = False

    def interrupt_selected(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
    ) -> None:
        nonlocal interrupted
        current_kind = (
            "backup"
            if source_name == destination.name
            and destination_name.endswith(".licensed-backup")
            else "publish"
            if source_name.endswith(".licensed-stage")
            and destination_name == destination.name
            else "other"
        )
        if current_kind != rename_kind or interrupted:
            real_rename(source_fd, source_name, destination_fd, destination_name)
            return
        interrupted = True
        if timing == "before":
            raise KeyboardInterrupt
        real_rename(source_fd, source_name, destination_fd, destination_name)
        raise KeyboardInterrupt

    monkeypatch.setattr(licensed_module, "rename_noreplace", interrupt_selected)
    with pytest.raises(KeyboardInterrupt):
        install_threading_copy(action, tmp_path / "journal")

    monkeypatch.setattr(licensed_module, "rename_noreplace", real_rename)
    install_threading_copy(action, tmp_path / "journal")
    assert destination.read_bytes() == source.read_bytes()
    assert not list(destination.parent.glob(f".{destination.name}.*.licensed-stage"))
    rollback_threading_copy(tmp_path / "journal")
    assert destination.read_bytes() == b"previous"


def test_resume_refuses_a_chmodded_complete_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = licensed_fixture(tmp_path, before=None)
    action = plan_threading_copy(source, destination)
    journal = tmp_path / "journal"
    real_rename = licensed_module.rename_noreplace

    def interrupt_before_publish(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
    ) -> None:
        if (
            source_name.endswith(".licensed-stage")
            and destination_name == destination.name
        ):
            raise KeyboardInterrupt
        real_rename(source_fd, source_name, destination_fd, destination_name)

    monkeypatch.setattr(licensed_module, "rename_noreplace", interrupt_before_publish)
    with pytest.raises(KeyboardInterrupt):
        install_threading_copy(action, journal)
    stage = next(destination.parent.glob(f".{destination.name}.*.licensed-stage"))
    stage.chmod(0o777)
    monkeypatch.setattr(licensed_module, "rename_noreplace", real_rename)

    with pytest.raises(BootstrapError, match="stage"):
        install_threading_copy(action, journal)

    assert not destination.exists()
    assert stat.S_IMODE(stage.stat().st_mode) == 0o777


def test_post_publication_mode_change_is_quarantined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = licensed_fixture(tmp_path, before=None)
    action = plan_threading_copy(source, destination)
    real_rename = licensed_module.rename_noreplace

    def chmod_after_publish(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
    ) -> None:
        real_rename(source_fd, source_name, destination_fd, destination_name)
        if (
            source_name.endswith(".licensed-stage")
            and destination_name == destination.name
        ):
            os.chmod(destination, 0o777)

    monkeypatch.setattr(licensed_module, "rename_noreplace", chmod_after_publish)

    with pytest.raises(BootstrapError, match="recovery"):
        install_threading_copy(action, tmp_path / "journal")

    assert not destination.exists()
    recoveries = list(destination.parent.glob(f".{destination.name}.*.recovery"))
    assert len(recoveries) == 1
    assert stat.S_IMODE(recoveries[0].stat().st_mode) == 0o777


def test_stage_mode_is_rechecked_at_the_immediate_pre_rename_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = licensed_fixture(tmp_path, before=None)
    action = plan_threading_copy(source, destination)
    real_same_named = licensed_module._same_named_file

    def chmod_after_name_binding(parent_fd: int, name: str, held_fd: int) -> bool:
        result = real_same_named(parent_fd, name, held_fd)
        if name.endswith(".licensed-stage") and result:
            os.chmod(destination.parent / name, 0o777)
        return result

    monkeypatch.setattr(licensed_module, "_same_named_file", chmod_after_name_binding)

    with pytest.raises(BootstrapError, match="stage"):
        install_threading_copy(action, tmp_path / "journal")

    stage = next(destination.parent.glob(f".{destination.name}.*.licensed-stage"))
    assert stat.S_IMODE(stage.stat().st_mode) == 0o777
    assert not destination.exists()
    assert not list(destination.parent.glob(f".{destination.name}.*.recovery"))


def test_post_rename_record_binds_mode_and_hash_to_one_descriptor_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = licensed_fixture(tmp_path, before=None)
    action = plan_threading_copy(source, destination)
    real_hash_fd = licensed_module._hash_fd
    hash_calls = 0

    def chmod_between_record_metadata_and_hash(
        fd: int, *, capped: bool = False
    ) -> object:
        nonlocal hash_calls
        hash_calls += 1
        if hash_calls == 3:
            os.fchmod(fd, 0o777)
        return real_hash_fd(fd, capped=capped)

    monkeypatch.setattr(
        licensed_module, "_hash_fd", chmod_between_record_metadata_and_hash
    )

    with pytest.raises(BootstrapError, match="recovery"):
        install_threading_copy(action, tmp_path / "journal")

    assert not destination.exists()
    recoveries = list(destination.parent.glob(f".{destination.name}.*.recovery"))
    assert len(recoveries) == 1
    assert recoveries[0].read_bytes() == source.read_bytes()
    assert stat.S_IMODE(recoveries[0].stat().st_mode) == 0o777


def test_open_record_closes_owned_descriptor_when_recording_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "xgameruntime.dll.threading"
    destination.write_bytes(b"original")
    original = destination.stat()
    opened: list[int] = []

    def fail_record(fd: int, logical: str) -> object:
        opened.append(fd)
        raise BootstrapError("simulated record failure")

    monkeypatch.setattr(licensed_module, "_record_fd", fail_record)
    parent_fd = os.open(tmp_path, licensed_module.DIRECTORY)
    try:
        with pytest.raises(BootstrapError, match="simulated record failure"):
            licensed_module._open_record(
                parent_fd, destination.name, os.fspath(destination)
            )
    finally:
        os.close(parent_fd)

    assert len(opened) == 1
    closed = False
    try:
        os.fstat(opened[0])
    except OSError as failure:
        closed = failure.errno == errno.EBADF
    finally:
        if not closed:
            os.close(opened[0])
    assert closed
    current = destination.stat()
    assert destination.read_bytes() == b"original"
    assert (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino)
    assert [path.name for path in tmp_path.iterdir()] == [destination.name]


def test_open_record_transfers_owned_descriptor_to_caller_on_success(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "xgameruntime.dll.threading"
    destination.write_bytes(b"original")
    original = destination.stat()
    parent_fd = os.open(tmp_path, licensed_module.DIRECTORY)
    try:
        record, destination_fd = licensed_module._open_record(
            parent_fd, destination.name, os.fspath(destination)
        )
    finally:
        os.close(parent_fd)

    assert destination_fd is not None
    try:
        held = os.fstat(destination_fd)
        assert (held.st_dev, held.st_ino) == (original.st_dev, original.st_ino)
        assert record.mode == stat.S_IMODE(original.st_mode)
    finally:
        os.close(destination_fd)


@pytest.mark.parametrize(
    ("rename_kind", "timing"),
    (
        ("remove-current", "before"),
        ("remove-current", "after"),
        ("restore-backup", "before"),
        ("restore-backup", "after"),
    ),
)
def test_rollback_resumes_after_interruption_at_every_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rename_kind: str,
    timing: str,
) -> None:
    source, destination = licensed_fixture(tmp_path, before=b"previous")
    action = plan_threading_copy(source, destination)
    journal = tmp_path / "journal"
    install_threading_copy(action, journal)
    real_rename = licensed_module.rename_noreplace
    interrupted = False

    def interrupt_selected(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
    ) -> None:
        nonlocal interrupted
        current_kind = (
            "remove-current"
            if source_name == destination.name
            and destination_name.endswith(".rollback-held")
            else "restore-backup"
            if source_name.endswith(".licensed-backup")
            and destination_name == destination.name
            else "other"
        )
        if current_kind != rename_kind or interrupted:
            real_rename(source_fd, source_name, destination_fd, destination_name)
            return
        interrupted = True
        if timing == "before":
            raise KeyboardInterrupt
        real_rename(source_fd, source_name, destination_fd, destination_name)
        raise KeyboardInterrupt

    monkeypatch.setattr(licensed_module, "rename_noreplace", interrupt_selected)
    with pytest.raises(KeyboardInterrupt):
        rollback_threading_copy(journal)

    monkeypatch.setattr(licensed_module, "rename_noreplace", real_rename)
    rollback_threading_copy(journal)
    assert destination.read_bytes() == b"previous"


def test_unknown_replacement_is_preserved_as_recovery_evidence(tmp_path: Path) -> None:
    source, destination = licensed_fixture(tmp_path, before=b"previous")
    journal = tmp_path / "journal"
    install_threading_copy(plan_threading_copy(source, destination), journal)
    replacement = b"concurrent unknown replacement"
    destination.write_bytes(replacement)

    with pytest.raises(BootstrapError, match="recovery"):
        rollback_threading_copy(journal)

    assert not destination.exists()
    assert replacement in recovery_payloads(destination)
    assert any(
        path.read_bytes() == b"previous"
        for path in destination.parent.glob(f".{destination.name}.*.licensed-backup")
    )


def test_publication_race_preserves_foreign_destination_as_recovery_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = licensed_fixture(tmp_path, before=None)
    action = plan_threading_copy(source, destination)
    real_rename = licensed_module.rename_noreplace
    foreign = b"foreign publication replacement"

    def replace_after_publish(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
    ) -> None:
        real_rename(source_fd, source_name, destination_fd, destination_name)
        if (
            source_name.endswith(".licensed-stage")
            and destination_name == destination.name
        ):
            os.rename(destination, tmp_path / "published-displaced")
            destination.write_bytes(foreign)

    monkeypatch.setattr(licensed_module, "rename_noreplace", replace_after_publish)

    with pytest.raises(BootstrapError, match="recovery"):
        install_threading_copy(action, tmp_path / "journal")

    assert not destination.exists()
    assert foreign in recovery_payloads(destination)
    assert (tmp_path / "published-displaced").read_bytes() == source.read_bytes()


def test_backup_race_fails_closed_and_preserves_the_swapped_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = licensed_fixture(tmp_path, before=b"previous")
    action = plan_threading_copy(source, destination)
    real_rename = licensed_module.rename_noreplace
    foreign = b"foreign at backup boundary"

    def swap_before_backup(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
    ) -> None:
        if source_name == destination.name and destination_name.endswith(
            ".licensed-backup"
        ):
            os.rename(destination, tmp_path / "original-displaced")
            destination.write_bytes(foreign)
        real_rename(source_fd, source_name, destination_fd, destination_name)

    monkeypatch.setattr(licensed_module, "rename_noreplace", swap_before_backup)

    with pytest.raises(BootstrapError, match="recovery"):
        install_threading_copy(action, tmp_path / "journal")

    assert not destination.exists()
    assert foreign in [path.read_bytes() for path in destination.parent.iterdir()]
    assert (tmp_path / "original-displaced").read_bytes() == b"previous"


def test_restore_race_quarantines_the_swapped_file_outside_the_live_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = licensed_fixture(tmp_path, before=b"previous")
    journal = tmp_path / "journal"
    install_threading_copy(plan_threading_copy(source, destination), journal)
    real_rename = licensed_module.rename_noreplace
    foreign = b"foreign at restore boundary"

    def swap_before_restore(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
    ) -> None:
        if (
            source_name.endswith(".licensed-backup")
            and destination_name == destination.name
        ):
            backup = next(
                destination.parent.glob(f".{destination.name}.*.licensed-backup")
            )
            os.rename(backup, tmp_path / "backup-displaced")
            backup.write_bytes(foreign)
        real_rename(source_fd, source_name, destination_fd, destination_name)

    monkeypatch.setattr(licensed_module, "rename_noreplace", swap_before_restore)

    with pytest.raises(BootstrapError, match="recovery"):
        rollback_threading_copy(journal)

    assert not destination.exists()
    assert foreign in recovery_payloads(destination)
    assert (tmp_path / "backup-displaced").read_bytes() == b"previous"


def test_stage_file_and_every_rename_are_durably_fsynced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = licensed_fixture(tmp_path, before=b"previous")
    action = plan_threading_copy(source, destination)
    events: list[str] = []
    real_fsync = licensed_module.os.fsync
    real_rename = licensed_module.rename_noreplace

    def record_fsync(fd: int) -> None:
        kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        events.append(f"fsync:{kind}")
        real_fsync(fd)

    def record_rename(
        source_fd: int,
        source_name: str,
        destination_fd: int,
        destination_name: str,
    ) -> None:
        events.append(f"rename:{source_name}:{destination_name}")
        real_rename(source_fd, source_name, destination_fd, destination_name)

    monkeypatch.setattr(licensed_module.os, "fsync", record_fsync)
    monkeypatch.setattr(licensed_module, "rename_noreplace", record_rename)
    install_threading_copy(action, tmp_path / "journal")

    rename_indexes = [
        index for index, event in enumerate(events) if event.startswith("rename:")
    ]
    assert len(rename_indexes) == 2
    assert any(event == "fsync:file" for event in events[: rename_indexes[0]])
    assert all(events[index + 1] == "fsync:dir" for index in rename_indexes)


def test_private_journal_is_mode_0600_and_redacts_process_only_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, destination = licensed_fixture(tmp_path, before=None)
    action = plan_threading_copy(source, destination)
    journal = tmp_path / "journal"

    install_threading_copy(action, journal)

    state_path = journal / "licensed.json"
    state_text = state_path.read_text(encoding="ascii")
    assert stat.S_IMODE(journal.stat().st_mode) == 0o700
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert str(source) not in state_text
    captured = capsys.readouterr()
    assert str(source) not in captured.out + captured.err
    assert action.source_sha256 not in captured.out + captured.err


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value.update(version=True),
        lambda value: value.update(disposition=[]),
        lambda value: value.update(status=[]),
    ),
)
def test_private_journal_rejects_nonexact_scalar_types(
    tmp_path: Path, mutate: object
) -> None:
    source, destination = licensed_fixture(tmp_path, before=None)
    journal = tmp_path / "journal"
    install_threading_copy(plan_threading_copy(source, destination), journal)
    state_path = journal / "licensed.json"
    value = json.loads(state_path.read_text(encoding="ascii"))
    mutate(value)  # type: ignore[operator]
    state_path.write_text(json.dumps(value), encoding="ascii")
    state_path.chmod(0o600)

    with pytest.raises(BootstrapError, match="journal"):
        rollback_threading_copy(journal)


def test_private_journal_rejects_disposition_incoherent_with_before(
    tmp_path: Path,
) -> None:
    source, destination = licensed_fixture(tmp_path, before=b"previous")
    journal = tmp_path / "journal"
    install_threading_copy(plan_threading_copy(source, destination), journal)
    state_path = journal / "licensed.json"
    value = json.loads(state_path.read_text(encoding="ascii"))
    value["disposition"] = "install"
    state_path.write_text(json.dumps(value), encoding="ascii")
    state_path.chmod(0o600)

    with pytest.raises(BootstrapError, match="journal"):
        rollback_threading_copy(journal)

    assert destination.read_bytes() == source.read_bytes()
    assert any(
        path.read_bytes() == b"previous"
        for path in destination.parent.glob(f".{destination.name}.*.licensed-backup")
    )


def test_private_journal_rejects_unbound_same_content_backup_name(
    tmp_path: Path,
) -> None:
    source, destination = licensed_fixture(tmp_path, before=b"previous")
    journal = tmp_path / "journal"
    install_threading_copy(plan_threading_copy(source, destination), journal)
    unrelated = (
        destination.parent / ".unrelated.0123456789abcdef01234567.licensed-backup"
    )
    unrelated.write_bytes(b"previous")
    unrelated.chmod(0o640)
    state_path = journal / "licensed.json"
    value = json.loads(state_path.read_text(encoding="ascii"))
    value["backup_name"] = unrelated.name
    state_path.write_text(json.dumps(value), encoding="ascii")
    state_path.chmod(0o600)

    with pytest.raises(BootstrapError, match="journal"):
        rollback_threading_copy(journal)

    assert destination.read_bytes() == source.read_bytes()
    assert unrelated.read_bytes() == b"previous"


def test_fresh_action_is_validated_before_journal_or_destination_mutation(
    tmp_path: Path,
) -> None:
    source, destination = licensed_fixture(tmp_path, before=None)
    action = plan_threading_copy(source, destination)
    other_destination = tmp_path / "other-prefix" / destination.name
    other_destination.parent.mkdir()
    mismatched = replace(action, _destination=other_destination)
    journal = tmp_path / "journal"

    with pytest.raises(BootstrapError, match="action"):
        install_threading_copy(mismatched, journal)

    assert not journal.exists()
    assert not destination.exists()
    assert not other_destination.exists()


@pytest.mark.parametrize(
    "mutate",
    (
        lambda action: replace(action, source_size=True),
        lambda action: replace(action, source_sha256="A" * 64),
        lambda action: replace(action, disposition=[]),
        lambda action: replace(action, disposition="replace"),
        lambda action: replace(action, before=replace(action.before, private=False)),
        lambda action: replace(action, _source=Path("relative.dll")),
    ),
)
def test_fresh_action_rejects_nonexact_or_incoherent_fields_before_writes(
    tmp_path: Path, mutate: object
) -> None:
    source, destination = licensed_fixture(tmp_path, before=None)
    action = mutate(plan_threading_copy(source, destination))  # type: ignore[operator]
    journal = tmp_path / "journal"

    with pytest.raises(BootstrapError, match="action"):
        install_threading_copy(action, journal)

    assert not journal.exists()
    assert not destination.exists()


def test_source_change_after_planning_fails_without_touching_destination(
    tmp_path: Path,
) -> None:
    source, destination = licensed_fixture(tmp_path, before=b"previous")
    action = plan_threading_copy(source, destination)
    make_synthetic_pe(source, payload=b"changed after planning")

    with pytest.raises(BootstrapError, match="changed"):
        install_threading_copy(action, tmp_path / "journal")

    assert destination.read_bytes() == b"previous"


def test_partial_stage_is_preserved_and_resume_creates_a_fresh_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = licensed_fixture(tmp_path, before=None)
    action = plan_threading_copy(source, destination)
    journal = tmp_path / "journal"
    real_write_all = licensed_module.write_all
    interrupted = False

    def interrupt_partial_write(fd: int, data: bytes) -> None:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            os.write(fd, data[:16])
            raise KeyboardInterrupt
        real_write_all(fd, data)

    monkeypatch.setattr(licensed_module, "write_all", interrupt_partial_write)
    with pytest.raises(KeyboardInterrupt):
        install_threading_copy(action, journal)
    monkeypatch.setattr(licensed_module, "write_all", real_write_all)

    install_threading_copy(action, journal)

    assert destination.read_bytes() == source.read_bytes()
    partial_recoveries = list(
        destination.parent.glob(f".{destination.name}.*.partial.recovery")
    )
    assert len(partial_recoveries) == 1
    assert partial_recoveries[0].read_bytes() == source.read_bytes()[:16]


def test_source_metadata_change_during_stage_copy_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = licensed_fixture(tmp_path, before=b"previous")
    action = plan_threading_copy(source, destination)
    real_write_all = licensed_module.write_all
    changed = False

    def change_source_during_write(fd: int, data: bytes) -> None:
        nonlocal changed
        real_write_all(fd, data)
        if not changed:
            changed = True
            info = source.stat()
            os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns + 1))

    monkeypatch.setattr(licensed_module, "write_all", change_source_during_write)

    with pytest.raises(BootstrapError, match="changed"):
        install_threading_copy(action, tmp_path / "journal")

    assert destination.read_bytes() == b"previous"
