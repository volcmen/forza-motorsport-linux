from __future__ import annotations

import errno
import importlib.util
import os
import stat
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

import pytest

HELPER_PATH = Path(__file__).parents[1] / "scripts" / "secure-user-files"
SPEC = importlib.util.spec_from_loader(
    "secure_user_files", SourceFileLoader("secure_user_files", str(HELPER_PATH))
)
assert SPEC is not None and SPEC.loader is not None
secure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(secure)


def make_sources(tmp_path: Path) -> dict[str, str]:
    sources_root = tmp_path / "sources"
    sources_root.mkdir()
    sources: dict[str, str] = {}
    for index, relative in enumerate(secure.PAYLOADS):
        source = sources_root / str(index)
        source.write_bytes(f"payload {index}\n".encode())
        source.chmod(0o755 if relative.startswith(".local/bin/") or "/libexec/" in relative else 0o644)
        sources[relative] = str(source)
    return sources


def source_arguments(sources: dict[str, str]) -> list[str]:
    arguments: list[str] = []
    for relative, source in sources.items():
        arguments.extend(("--source", f"{relative}={source}"))
    return arguments


def test_write_new_at_fsyncs_file_before_close(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, int]] = []
    monkeypatch.setattr(secure.os, "open", lambda *args, **kwargs: 17)
    monkeypatch.setattr(secure.os, "write", lambda fd, data: len(data))
    monkeypatch.setattr(secure.os, "fchmod", lambda fd, mode: events.append(("chmod", fd)))
    monkeypatch.setattr(secure.os, "fsync", lambda fd: events.append(("fsync", fd)))
    monkeypatch.setattr(secure.os, "fstat", lambda fd: SimpleNamespace(st_mode=stat.S_IFREG | 0o600))
    monkeypatch.setattr(secure.os, "close", lambda fd: events.append(("close", fd)))

    secure.write_new_at(5, "payload", b"payload", 0o600)

    assert events == [("chmod", 17), ("fsync", 17), ("close", 17)]


def test_publish_fsyncs_both_rename_directories(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, int]] = []
    parents = iter(((11, "payload"), (22, "payload")))
    monkeypatch.setattr(secure, "open_parent", lambda *args, **kwargs: next(parents))
    monkeypatch.setattr(secure, "rename_noreplace", lambda *args: events.append(("rename", 0)))
    expected = (1, 2, 0o600, secure.digest(b"payload"))
    monkeypatch.setattr(
        secure,
        "read_regular_at",
        lambda *args, **kwargs: (
            b"payload",
            0o600,
            SimpleNamespace(st_dev=1, st_ino=2, st_mode=stat.S_IFREG | 0o600),
        ),
    )
    monkeypatch.setattr(secure.os, "fsync", lambda fd: events.append(("fsync", fd)))
    monkeypatch.setattr(secure.os, "close", lambda fd: events.append(("close", fd)))

    assert secure.publish(3, 4, ".local/bin/forza-linux", expected, "a" * 24) == expected

    assert events == [
        ("rename", 0),
        ("fsync", 11),
        ("fsync", 22),
        ("close", 11),
        ("close", 22),
    ]


def test_hostile_journal_stage_is_rejected() -> None:
    transaction = "a" * 24
    lines = [f"forza-motorsport-linux-journal-v2\t{transaction}\t.local/bin\t1\t2"]
    lines.extend(f"{relative}\t1\t2\t755\t{'0' * 64}" for relative in secure.PAYLOADS)

    with pytest.raises(secure.SecureFilesError):
        secure.parse_journal(("\n".join(lines) + "\n").encode())


def test_journal_v2_requires_exact_identity_bound_records() -> None:
    transaction = "e" * 24
    stage = f".forza-install-{transaction}"
    records = {relative: (1, index + 2, 0o755, "0" * 64) for index, relative in enumerate(secure.PAYLOADS)}
    valid = secure.journal_bytes(transaction, stage, (11, 12), records)
    parsed_transaction, parsed_stage, parsed_stage_identity, parsed_records = secure.parse_journal(valid)
    assert (parsed_transaction, parsed_stage, parsed_stage_identity, parsed_records) == (
        transaction,
        stage,
        (11, 12),
        records,
    )

    lines = valid.decode().splitlines()
    hostile_inputs = (
        "\n".join([lines[0].replace("journal-v2", "journal-v1"), *lines[1:]]) + "\n",
        "\n".join(lines[:-1]) + "\n",
        "\n".join([*lines, lines[1]]) + "\n",
        "\n".join([*lines, f".local/bin/hostile\t1\t2\t755\t{'0' * 64}"]) + "\n",
    )
    for hostile in hostile_inputs:
        with pytest.raises(secure.SecureFilesError):
            secure.parse_journal(hostile.encode())


def test_recovery_directory_rejects_wrong_mode(tmp_path: Path) -> None:
    recovery = tmp_path / secure.RECOVERY_ROOT
    recovery.mkdir(parents=True, mode=0o755)
    recovery.chmod(0o755)
    root_fd = os.open(tmp_path, secure.DIRECTORY)
    try:
        with pytest.raises(secure.SecureFilesError):
            secure.recovery_directory(root_fd, "b" * 24)
    finally:
        os.close(root_fd)


def test_retire_name_retries_collision_and_fsyncs_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction = "c" * 24
    recovery = tmp_path / secure.RECOVERY_ROOT / transaction
    recovery.mkdir(parents=True, mode=0o700)
    for directory in (recovery.parent, recovery):
        directory.chmod(0o700)
    collision = recovery / f"owned-{'1' * 24}"
    collision.write_bytes(b"collision")
    source = tmp_path / "payload"
    source.write_bytes(b"owned")
    tokens = iter(("1" * 24, "2" * 24))
    monkeypatch.setattr(secure.secrets, "token_hex", lambda count: next(tokens))
    real_fsync = os.fsync
    fsynced: list[int] = []

    def record_fsync(fd: int) -> None:
        fsynced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(secure.os, "fsync", record_fsync)
    root_fd = os.open(tmp_path, secure.DIRECTORY)
    try:
        secure.retire_name(root_fd, "payload", root_fd, transaction, "owned")
    finally:
        os.close(root_fd)

    assert collision.read_bytes() == b"collision"
    assert (recovery / f"owned-{'2' * 24}").read_bytes() == b"owned"
    assert len(fsynced) >= 2


def test_retire_stage_is_atomic_first_and_preserves_boundary_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction = "d" * 24
    stage = f".forza-install-{transaction}"
    stage_path = tmp_path / stage
    stage_path.mkdir(mode=0o700)
    expected = stage_path.stat()
    real_rename = secure.rename_noreplace
    real_stat = os.stat

    def boundary_rename(source_fd: int, source: str, destination_fd: int, destination: str) -> None:
        real_rename(source_fd, source, destination_fd, destination)
        if source == stage and destination.startswith("stage-"):
            stage_path.mkdir(mode=0o700)
            (stage_path / "replacement").write_text("user replacement\n")

    def no_precheck(path: str, *args: object, **kwargs: object) -> os.stat_result:
        if path == stage and kwargs.get("dir_fd") is not None:
            raise AssertionError("stage name was checked before its atomic retirement")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(secure, "rename_noreplace", boundary_rename)
    monkeypatch.setattr(secure.os, "stat", no_precheck)
    root_fd = os.open(tmp_path, secure.DIRECTORY)
    try:
        secure.retire_stage(root_fd, stage, expected, transaction)
    finally:
        os.close(root_fd)

    assert (stage_path / "replacement").read_text() == "user replacement\n"
    retired = list((tmp_path / secure.RECOVERY_ROOT / transaction).glob("stage-*"))
    assert len(retired) == 1
    assert retired[0].stat().st_ino == expected.st_ino


def test_retire_stage_restores_a_substitution_without_traversing_it(tmp_path: Path) -> None:
    transaction = "f" * 24
    stage = f".forza-install-{transaction}"
    original = tmp_path / stage
    original.mkdir(mode=0o700)
    expected = original.stat()
    moved_original = tmp_path / "original-stage"
    original.rename(moved_original)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("outside\n")
    original.symlink_to(outside, target_is_directory=True)

    root_fd = os.open(tmp_path, secure.DIRECTORY)
    try:
        secure.retire_stage(root_fd, stage, expected, transaction)
    finally:
        os.close(root_fd)

    assert original.is_symlink()
    assert original.resolve() == outside
    assert sentinel.read_text() == "outside\n"
    assert moved_original.stat().st_ino == expected.st_ino


@pytest.mark.parametrize("journal_kind", ["symlink", "directory"])
def test_nonregular_journal_fails_before_any_payload_move(tmp_path: Path, journal_kind: str) -> None:
    state = tmp_path / secure.STATE_ROOT
    state.mkdir(parents=True)
    payload = tmp_path / secure.PAYLOADS[0]
    payload.parent.mkdir(parents=True)
    payload.write_text("user payload\n")
    outside = tmp_path / "outside-journal"
    outside.write_text("outside\n")
    journal = tmp_path / secure.JOURNAL
    if journal_kind == "symlink":
        journal.symlink_to(outside)
    else:
        journal.mkdir()

    with pytest.raises((OSError, secure.SecureFilesError)):
        secure.uninstall(str(tmp_path))

    assert payload.read_text() == "user payload\n"
    assert outside.read_text() == "outside\n"


def test_rename_collision_is_not_treated_as_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secure, "RENAMEAT2", lambda *args: -1)
    monkeypatch.setattr(secure.ctypes, "get_errno", lambda: errno.EEXIST)

    with pytest.raises(OSError) as error:
        secure.rename_noreplace(1, "source", 2, "destination")

    assert error.value.errno == errno.EEXIST


def test_install_orders_durable_journal_and_manifest_around_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    sources = make_sources(tmp_path)

    events: list[str] = []
    real_fsync = os.fsync
    real_publish = secure.publish
    real_retire_name = secure.retire_name
    real_retire_stage = secure.retire_stage

    def recording_fsync(fd: int) -> None:
        target = os.readlink(f"/proc/self/fd/{fd}")
        if target.endswith("/transaction-journal"):
            events.append("journal-file-fsync")
        elif target.endswith("/install-manifest"):
            events.append("manifest-file-fsync")
        real_fsync(fd)

    def recording_publish(
        stage_fd: int,
        root_fd: int,
        relative: str,
        expected_record: tuple[int, int, int, str],
        transaction: str,
    ) -> tuple[int, int, int, str]:
        result = real_publish(stage_fd, root_fd, relative, expected_record, transaction)
        events.append(f"published:{relative}")
        return result

    def recording_retire(
        parent_fd: int, leaf: str, root_fd: int, transaction: str, label: str
    ) -> None:
        if label == "journal":
            events.append("journal-retire")
        real_retire_name(parent_fd, leaf, root_fd, transaction, label)

    def recording_retire_stage(
        root_fd: int,
        stage: str,
        stage_info: os.stat_result | tuple[int, int] | None,
        transaction: str,
    ) -> None:
        events.append("stage-retire")
        real_retire_stage(root_fd, stage, stage_info, transaction)

    monkeypatch.setattr(secure.os, "fsync", recording_fsync)
    monkeypatch.setattr(secure, "publish", recording_publish)
    monkeypatch.setattr(secure, "retire_name", recording_retire)
    monkeypatch.setattr(secure, "retire_stage", recording_retire_stage)

    secure.install(str(root), sources, explicit_root=True)

    first_payload = events.index(f"published:{secure.PAYLOADS[0]}")
    manifest_publish = events.index(f"published:{secure.MANIFEST}")
    stage_retire = events.index("stage-retire")
    journal_retire = events.index("journal-retire")
    assert events.index("journal-file-fsync") < first_payload
    assert manifest_publish < events.index("manifest-file-fsync", manifest_publish)
    assert events.index("manifest-file-fsync", manifest_publish) < stage_retire
    assert stage_retire < journal_retire


def test_restart_keeps_journal_when_committed_stage_retirement_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    sources = make_sources(tmp_path)
    env = os.environ.copy()
    env["FORZA_INSTALL_CRASH_AFTER_MANIFEST"] = "1"

    result = subprocess.run(
        [
            str(HELPER_PATH),
            "install",
            "--root",
            str(root),
            "--explicit-root",
            *source_arguments(sources),
        ],
        env=env,
        check=False,
    )

    assert result.returncode == 99
    journal = root / secure.JOURNAL
    assert journal.is_file()
    stage = secure.parse_journal(journal.read_bytes())[1]
    assert (root / stage).is_dir()

    def crash_stage_retirement(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated stage retirement crash")

    monkeypatch.setattr(secure, "retire_stage", crash_stage_retirement)
    root_fd = os.open(root, secure.DIRECTORY)
    try:
        with pytest.raises(RuntimeError, match="simulated stage retirement crash"):
            secure.recover_interrupted(root_fd)
    finally:
        os.close(root_fd)

    assert journal.is_file()
