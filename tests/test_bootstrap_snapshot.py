# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

import forza_bootstrap.cli as cli_module
from forza_bootstrap.cli import main
from forza_bootstrap.model import ArtifactSpec, BootstrapError, sha256_bytes
from forza_bootstrap.snapshot import (
    ChangeRule,
    ComparedRecord,
    Comparison,
    FileRecord,
    Snapshot,
    SnapshotScope,
    SnapshotTarget,
    build_snapshot_scope,
    canonical_snapshot_bytes,
    capture_snapshot,
    compare_snapshots,
    output_snapshot,
    render_comparison,
    write_snapshot,
)

TRANSACTION = "1" * 24
MANIFEST = "2" * 64
ROOT_ID = "3:4"


def record(
    logical_path: str,
    content: bytes | None,
    *,
    mode: int = 0o644,
    private: bool = False,
) -> FileRecord:
    if content is None:
        return FileRecord(logical_path, "absent", None, None, None, private)
    return FileRecord(
        logical_path,
        "regular",
        mode,
        len(content),
        sha256_bytes(content),
        private,
    )


def snapshot(*records: FileRecord, **updates: object) -> Snapshot:
    values = {
        "version": 1,
        "transaction_id": TRANSACTION,
        "manifest_sha256": MANIFEST,
        "steam_root_id": ROOT_ID,
        "records": tuple(records),
    }
    values.update(updates)
    return Snapshot(**values)  # type: ignore[arg-type]


def test_compare_classifies_exact_expected_unchanged_and_unexpected() -> None:
    before = snapshot(
        record("managed/a", b"old"),
        record("guard/b", b"same"),
        record("guard/c", b"old"),
    )
    after = snapshot(
        record("managed/a", b"new"),
        record("guard/b", b"same"),
        record("guard/c", b"surprise"),
    )
    rules = (
        ChangeRule("managed/a", "expected", sha256_bytes(b"new"), 0o644),
        ChangeRule("guard/b", "unchanged", sha256_bytes(b"same"), 0o644),
        ChangeRule("guard/c", "unchanged", sha256_bytes(b"old"), 0o644),
    )

    result = compare_snapshots(before, after, rules)

    assert [item.logical_path for item in result.expected_changes] == ["managed/a"]
    assert [item.logical_path for item in result.required_unchanged] == ["guard/b"]
    assert [item.logical_path for item in result.unexpected_changes] == ["guard/c"]
    assert result.ok is False


@pytest.mark.parametrize(
    ("before_record", "after_record", "after_sha256", "after_mode"),
    (
        (record("managed/a", None), record("managed/a", b"new"), sha256_bytes(b"new"), 0o644),
        (record("managed/a", b"old"), record("managed/a", None), None, None),
    ),
)
def test_compare_accepts_exact_present_absent_transitions(
    before_record: FileRecord,
    after_record: FileRecord,
    after_sha256: str | None,
    after_mode: int | None,
) -> None:
    result = compare_snapshots(
        snapshot(before_record),
        snapshot(after_record),
        (ChangeRule("managed/a", "expected", after_sha256, after_mode),),
    )

    assert result.ok is True
    assert [item.logical_path for item in result.expected_changes] == ["managed/a"]


def test_compare_rejects_wrong_mode_as_unexpected() -> None:
    result = compare_snapshots(
        snapshot(record("managed/a", b"old")),
        snapshot(record("managed/a", b"new", mode=0o600)),
        (ChangeRule("managed/a", "expected", sha256_bytes(b"new"), 0o644),),
    )

    assert [item.logical_path for item in result.unexpected_changes] == ["managed/a"]
    assert result.ok is False


@pytest.mark.parametrize(
    ("field", "different"),
    (
        ("transaction_id", "4" * 24),
        ("manifest_sha256", "5" * 64),
        ("steam_root_id", "5:6"),
    ),
)
def test_compare_requires_matching_snapshot_identity(field: str, different: str) -> None:
    item = record("guard/a", b"same")
    after = snapshot(item, **{field: different})

    with pytest.raises(BootstrapError, match=field):
        compare_snapshots(
            snapshot(item),
            after,
            (ChangeRule("guard/a", "unchanged", item.sha256, item.mode),),
        )


def test_compare_rejects_duplicate_logical_paths() -> None:
    item = record("guard/a", b"same")
    rule = ChangeRule("guard/a", "unchanged", item.sha256, item.mode)

    with pytest.raises(BootstrapError, match="duplicate snapshot logical path"):
        compare_snapshots(snapshot(item, item), snapshot(item), (rule,))
    with pytest.raises(BootstrapError, match="duplicate comparison rule"):
        compare_snapshots(snapshot(item), snapshot(item), (rule, rule))


def test_compare_requires_one_rule_for_every_record() -> None:
    item = record("guard/a", b"same")

    with pytest.raises(BootstrapError, match="rule coverage"):
        compare_snapshots(snapshot(item), snapshot(item), ())


def test_compare_rejects_extra_after_record() -> None:
    first = record("guard/a", b"same")
    extra = record("guard/b", b"extra")

    with pytest.raises(BootstrapError, match="record coverage"):
        compare_snapshots(
            snapshot(first),
            snapshot(first, extra),
            (ChangeRule("guard/a", "unchanged", first.sha256, first.mode),),
        )


def test_private_digest_and_path_never_enter_human_report() -> None:
    digest = "f" * 64
    private_path = "private/local-component"
    before = FileRecord(private_path, "absent", None, None, None, True)
    after = FileRecord(private_path, "regular", 0o600, 4, digest, True)
    result = Comparison(
        expected_changes=(ComparedRecord(private_path, before, after, "expected"),),
        required_unchanged=(),
        unexpected_changes=(),
    )

    report = render_comparison(result)

    assert digest not in report
    assert private_path not in report
    assert "[private local digest verified]" in report


def test_canonical_snapshot_bytes_sort_records_and_use_only_logical_paths() -> None:
    value = snapshot(record("z/item", None), record("a/item", b"x"))

    data = canonical_snapshot_bytes(value)

    assert data == (
        b'{"manifest_sha256":"' + MANIFEST.encode() +
        b'","records":[{"logical_path":"a/item","mode":420,"private":false,'
        b'"sha256":"2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881",'
        b'"size":1,"state":"regular"},{"logical_path":"z/item","mode":null,'
        b'"private":false,"sha256":null,"size":null,"state":"absent"}],'
        b'"steam_root_id":"3:4","transaction_id":"111111111111111111111111","version":1}'
    )


def artifact(logical_path: str, relative_path: str, *, private: bool = False) -> ArtifactSpec:
    return ArtifactSpec(logical_path, relative_path, 1, "a" * 64, 0o644, private)


def test_capture_hashes_only_supplied_paths_and_binds_descriptor_root_id(tmp_path: Path) -> None:
    steam_root = tmp_path / "steam"
    user_root = tmp_path / "user"
    steam_root.mkdir()
    user_root.mkdir()
    (steam_root / "managed").mkdir()
    (steam_root / "managed/one").write_bytes(b"one")
    (steam_root / "unrelated").write_bytes(b"do not capture")
    (user_root / "two").write_bytes(b"two")
    scope = build_snapshot_scope(
        TRANSACTION,
        MANIFEST,
        {
            "steam": (artifact("managed/one", "managed/one"),),
            "user": (artifact("managed/two", "two"),),
        },
    )

    result = capture_snapshot(scope, {"steam": steam_root, "user": user_root})

    root_stat = os.stat(steam_root, follow_symlinks=False)
    assert result.steam_root_id == f"{root_stat.st_dev}:{root_stat.st_ino}"
    assert [item.logical_path for item in result.records] == ["managed/one", "managed/two"]
    assert [item.sha256 for item in result.records] == [sha256_bytes(b"one"), sha256_bytes(b"two")]
    serialized = canonical_snapshot_bytes(result)
    assert os.fsencode(tmp_path) not in serialized
    assert b"unrelated" not in serialized


def test_capture_streams_regular_files_in_one_mib_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steam_root = tmp_path / "steam"
    steam_root.mkdir()
    (steam_root / "large").write_bytes(b"x" * (1024 * 1024 + 1))
    scope = build_snapshot_scope(
        TRANSACTION,
        MANIFEST,
        {"steam": (artifact("managed/large", "large"),)},
    )
    reads: list[int] = []
    real_read = os.read

    def observed_read(fd: int, size: int) -> bytes:
        reads.append(size)
        return real_read(fd, size)

    monkeypatch.setattr("forza_bootstrap.snapshot.os.read", observed_read)

    capture_snapshot(scope, {"steam": steam_root})

    assert reads == [1024 * 1024, 1024 * 1024, 1024 * 1024]


@pytest.mark.parametrize("kind", ("directory", "symlink", "fifo"))
def test_capture_rejects_non_regular_target(tmp_path: Path, kind: str) -> None:
    steam_root = tmp_path / "steam"
    steam_root.mkdir()
    target = steam_root / "target"
    if kind == "directory":
        target.mkdir()
    elif kind == "symlink":
        target.symlink_to("missing")
    else:
        os.mkfifo(target)
    scope = build_snapshot_scope(
        TRANSACTION,
        MANIFEST,
        {"steam": (artifact("managed/target", "target"),)},
    )

    with pytest.raises(BootstrapError, match="regular"):
        capture_snapshot(scope, {"steam": steam_root})


def test_capture_records_absent_target(tmp_path: Path) -> None:
    steam_root = tmp_path / "steam"
    steam_root.mkdir()
    scope = build_snapshot_scope(
        TRANSACTION,
        MANIFEST,
        {"steam": (artifact("managed/missing", "missing"),)},
    )

    result = capture_snapshot(scope, {"steam": steam_root})

    assert result.records == (record("managed/missing", None),)


def test_capture_revalidates_scope_before_descriptor_access(tmp_path: Path) -> None:
    steam_root = tmp_path / "steam"
    steam_root.mkdir()
    (tmp_path / "outside").write_bytes(b"must not be read")
    scope = SnapshotScope(
        1,
        TRANSACTION,
        MANIFEST,
        (SnapshotTarget("managed/outside", "steam", "../outside"),),
    )

    with pytest.raises(BootstrapError, match="relative path"):
        capture_snapshot(scope, {"steam": steam_root})


def test_stdout_snapshot_causes_no_filesystem_writes(tmp_path: Path) -> None:
    stream = io.StringIO()
    value = snapshot(record("managed/a", b"same"))

    output_snapshot(value, None, stream)

    assert stream.getvalue() == canonical_snapshot_bytes(value).decode("ascii") + "\n"
    assert list(tmp_path.iterdir()) == []


def test_explicit_snapshot_output_is_absolute_atomic_and_mode_0600(tmp_path: Path) -> None:
    os.chmod(tmp_path, 0o700)
    output = tmp_path / "snapshot.json"

    write_snapshot(output, snapshot(record("managed/a", b"same")))

    assert output.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".*.tmp"))


def test_snapshot_output_rejects_relative_path_without_writing(tmp_path: Path) -> None:
    with pytest.raises(BootstrapError, match="absolute"):
        write_snapshot(Path("snapshot.json"), snapshot(record("managed/a", b"same")))

    assert list(tmp_path.iterdir()) == []


def test_cli_snapshot_prints_by_default_and_writes_only_with_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    os.chmod(tmp_path, 0o700)
    value = snapshot(record("managed/a", b"same"))
    monkeypatch.setattr(cli_module, "_capture_current_snapshot", lambda: value)

    assert main(["snapshot"]) == 0
    assert capsys.readouterr().out == canonical_snapshot_bytes(value).decode("ascii") + "\n"
    assert list(tmp_path.iterdir()) == []

    output = tmp_path / "explicit.json"
    assert main(["snapshot", "--output", str(output)]) == 0
    assert capsys.readouterr().out == ""
    assert output.stat().st_mode & 0o777 == 0o600


def test_cli_compare_returns_zero_only_for_ok_comparison(tmp_path: Path) -> None:
    os.chmod(tmp_path, 0o700)
    before_path = tmp_path / "before.json"
    same_path = tmp_path / "same.json"
    changed_path = tmp_path / "changed.json"
    before = snapshot(record("guard/a", b"same"))
    write_snapshot(before_path, before)
    write_snapshot(same_path, before)
    write_snapshot(changed_path, snapshot(record("guard/a", b"changed")))

    assert main(["compare", "--before", str(before_path), "--after", str(same_path)]) == 0
    assert main(["compare", "--before", str(before_path), "--after", str(changed_path)]) == 1
