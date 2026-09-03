# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import errno
import io
import json
import os
import stat
from pathlib import Path

import pytest

import forza_bootstrap.cli as cli_module
import forza_bootstrap.snapshot as snapshot_module
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
    read_snapshot,
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


def write_snapshot_value(path: Path, value: object) -> None:
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def snapshot_value(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "version": 1,
        "transaction_id": TRANSACTION,
        "manifest_sha256": MANIFEST,
        "steam_root_id": ROOT_ID,
        "records": [],
    }
    values.update(updates)
    return values


def raw_record_value(
    logical_path: object,
    *,
    private: object = False,
) -> dict[str, object]:
    return {
        "logical_path": logical_path,
        "state": "absent",
        "mode": None,
        "size": None,
        "sha256": None,
        "private": private,
    }


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


def assert_private_comparison_error_is_redacted(
    before: Snapshot, after: Snapshot, rules: object, private_path: str
) -> None:
    with pytest.raises(BootstrapError) as failure:
        compare_snapshots(before, after, rules)  # type: ignore[arg-type]

    assert str(failure.value) == "private snapshot comparison failed"
    assert private_path not in str(failure.value)


def test_after_private_path_redacts_earlier_before_duplicate_error() -> None:
    private_path = "private/sensitive-component"
    public = record(private_path, b"same")
    private = record(private_path, b"same", private=True)

    assert_private_comparison_error_is_redacted(
        snapshot(public, public),
        snapshot(private),
        (ChangeRule(private_path, "unchanged", private.sha256, private.mode),),
        private_path,
    )


def test_after_private_path_redacts_missing_rule_error() -> None:
    private_path = "private/sensitive-component"

    assert_private_comparison_error_is_redacted(
        snapshot(record(private_path, b"same")),
        snapshot(record(private_path, b"same", private=True)),
        (),
        private_path,
    )


def test_after_private_path_redacts_extra_rule_error() -> None:
    private_path = "private/sensitive-component"
    private = record(private_path, b"same", private=True)

    assert_private_comparison_error_is_redacted(
        snapshot(record(private_path, b"same")),
        snapshot(private),
        (
            ChangeRule(private_path, "unchanged", private.sha256, private.mode),
            ChangeRule("guard/extra", "unchanged", None, None),
        ),
        private_path,
    )


@pytest.mark.parametrize(
    ("before_private", "after_private"), ((False, True), (True, False))
)
def test_compare_rejects_privacy_only_drift_as_private_comparison_error(
    before_private: bool, after_private: bool
) -> None:
    private_path = "private/sensitive-component"
    before = record(private_path, b"same", private=before_private)
    after = record(private_path, b"same", private=after_private)

    with pytest.raises(BootstrapError) as failure:
        compare_snapshots(
            snapshot(before),
            snapshot(after),
            (ChangeRule(private_path, "expected", after.sha256, after.mode),),
        )

    assert str(failure.value) == "private snapshot comparison failed"
    assert private_path not in str(failure.value)
    assert before.sha256 not in str(failure.value)


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


def test_cli_refuses_private_snapshot_stdout_without_leaking(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    private_path = "private/sensitive-component"
    private_digest = "f" * 64
    value = snapshot(
        FileRecord(private_path, "regular", 0o600, 4, private_digest, True)
    )
    monkeypatch.setattr(cli_module, "_capture_current_snapshot", lambda: value)

    assert main(["snapshot"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: private snapshot requires explicit owner-only output\n"
    assert private_path not in captured.out + captured.err
    assert private_digest not in captured.out + captured.err


def test_private_canonical_bytes_require_explicit_owner_only_file(tmp_path: Path) -> None:
    os.chmod(tmp_path, 0o700)
    private_path = "private/sensitive-component"
    private_digest = "f" * 64
    value = snapshot(
        FileRecord(private_path, "regular", 0o600, 4, private_digest, True)
    )

    with pytest.raises(BootstrapError) as failure:
        canonical_snapshot_bytes(value)

    assert str(failure.value) == "private snapshot requires explicit owner-only output"
    assert private_path not in str(failure.value)
    assert private_digest not in str(failure.value)

    output = tmp_path / "private.json"
    write_snapshot(output, value)
    assert output.stat().st_mode & 0o777 == 0o600
    assert read_snapshot(output) == value


def test_private_scan_precedes_validation_of_malformed_record_list() -> None:
    private_path = "private/sensitive-component"
    public = record(private_path, b"same")
    private = record(private_path, b"same", private=True)
    malformed = snapshot(records=[public, public, private])

    with pytest.raises(BootstrapError) as failure:
        canonical_snapshot_bytes(malformed)

    assert str(failure.value) == "private snapshot requires explicit owner-only output"
    assert private_path not in str(failure.value)


@pytest.mark.parametrize("kind", ("relative", "missing-parent"))
def test_private_scan_redacts_explicit_output_failures(tmp_path: Path, kind: str) -> None:
    private_path = "private/sensitive-component"
    value = snapshot(record(private_path, b"same", private=True))
    path = Path("relative.json") if kind == "relative" else tmp_path / "missing" / "out"

    with pytest.raises(BootstrapError) as failure:
        write_snapshot(path, value)

    assert str(failure.value) == "private snapshot requires explicit owner-only output"
    assert private_path not in str(failure.value)
    assert str(path) not in str(failure.value)


def test_private_comparison_scan_handles_late_private_in_malformed_list() -> None:
    private_path = "private/sensitive-component"
    public = record(private_path, b"same")
    private = record(private_path, b"same", private=True)
    malformed_before = snapshot(records=[public, public, private])

    assert_private_comparison_error_is_redacted(
        malformed_before,
        snapshot(public),
        (ChangeRule(private_path, "unchanged", public.sha256, public.mode),),
        private_path,
    )


@pytest.mark.parametrize(
    "malformation",
    ("top-level", "record-shape", "identity", "early-duplicate"),
)
def test_read_private_native_json_scan_precedes_all_schema_validation(
    tmp_path: Path, malformation: str
) -> None:
    os.chmod(tmp_path, 0o700)
    private_path = "private/sensitive-persisted-component"
    private_record = raw_record_value(private_path, private=True)
    if malformation == "top-level":
        value = snapshot_value(records=[private_record], unexpected=True)
    elif malformation == "record-shape":
        value = snapshot_value(records=[{"logical_path": private_path, "private": True}])
    elif malformation == "identity":
        value = snapshot_value(transaction_id=private_path, records=[private_record])
    else:
        duplicate = raw_record_value("guard/duplicate")
        value = snapshot_value(records=[duplicate, duplicate, private_record])
    source = tmp_path / "snapshot.json"
    write_snapshot_value(source, value)

    with pytest.raises(BootstrapError) as failure:
        read_snapshot(source)

    assert str(failure.value) == "private snapshot input is invalid"
    assert private_path not in str(failure.value)


def test_cli_redacts_malformed_private_native_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    os.chmod(tmp_path, 0o700)
    private_path = "private/sensitive-persisted-component"
    duplicate = raw_record_value("guard/duplicate")
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    write_snapshot_value(
        before,
        snapshot_value(
            records=[
                duplicate,
                duplicate,
                raw_record_value(private_path, private=True),
            ]
        ),
    )
    write_snapshot(after, snapshot())

    assert main(["compare", "--before", str(before), "--after", str(after)]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: private snapshot input is invalid\n"
    assert private_path not in captured.err
    assert "guard/duplicate" not in captured.err


def test_privacy_scan_does_not_iterate_arbitrary_record_object() -> None:
    class ExplodingRecords:
        iterated = False

        def __iter__(self):
            self.iterated = True
            raise AssertionError("arbitrary record object was iterated")

    records = ExplodingRecords()
    malformed = snapshot(records=records)

    with pytest.raises(BootstrapError, match="records are invalid"):
        canonical_snapshot_bytes(malformed)

    assert records.iterated is False


def test_private_duplicate_scope_error_is_fixed_and_redacted() -> None:
    private_path = "private/sensitive-component"

    with pytest.raises(BootstrapError) as failure:
        build_snapshot_scope(
            TRANSACTION,
            MANIFEST,
            {
                "steam": (
                    artifact(private_path, "first", private=True),
                    artifact(private_path, "second", private=True),
                )
            },
        )

    assert str(failure.value) == "private snapshot capture failed"
    assert private_path not in str(failure.value)


def test_private_nonregular_error_is_fixed_and_redacted(tmp_path: Path) -> None:
    steam_root = tmp_path / "steam"
    steam_root.mkdir()
    (steam_root / "sensitive-source").mkdir()
    private_path = "private/sensitive-component"
    scope = build_snapshot_scope(
        TRANSACTION,
        MANIFEST,
        {"steam": (artifact(private_path, "sensitive-source", private=True),)},
    )

    with pytest.raises(BootstrapError) as failure:
        capture_snapshot(scope, {"steam": steam_root})

    message = str(failure.value)
    assert message == "private snapshot capture failed"
    assert private_path not in message
    assert str(steam_root) not in message


def test_private_filesystem_error_discards_oserror_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steam_root = tmp_path / "steam"
    steam_root.mkdir()
    (steam_root / "sensitive-source").write_bytes(b"data")
    private_path = "private/sensitive-component"
    physical_path = str(steam_root / "sensitive-source")
    scope = build_snapshot_scope(
        TRANSACTION,
        MANIFEST,
        {"steam": (artifact(private_path, "sensitive-source", private=True),)},
    )

    def fail_read(_fd: int, _size: int) -> bytes:
        raise OSError(errno.EIO, "simulated private read failure", physical_path)

    monkeypatch.setattr(snapshot_module.os, "read", fail_read)

    with pytest.raises(BootstrapError) as failure:
        capture_snapshot(scope, {"steam": steam_root})

    message = str(failure.value)
    assert message == "private snapshot capture failed"
    assert private_path not in message
    assert physical_path not in message


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


@pytest.mark.parametrize(
    "invalid", (None, 1, "artifact", {"bad": "shape"}, artifact("a", "a"))
)
def test_scope_construction_rejects_invalid_artifact_collections(invalid: object) -> None:
    with pytest.raises(BootstrapError) as failure:
        build_snapshot_scope(
            TRANSACTION,
            MANIFEST,
            {"steam": invalid},  # type: ignore[dict-item]
        )

    assert str(failure.value) == "snapshot artifact collection is invalid"


@pytest.mark.parametrize("invalid", (None, 1, "scope", []))
def test_scope_construction_rejects_invalid_root_collection(invalid: object) -> None:
    with pytest.raises(BootstrapError) as failure:
        build_snapshot_scope(
            TRANSACTION,
            MANIFEST,
            invalid,  # type: ignore[arg-type]
        )

    assert str(failure.value) == "snapshot artifact scope is invalid"


@pytest.mark.parametrize("invalid", (".", "a//b", "a/", "a/./b"))
def test_scope_construction_rejects_noncanonical_logical_and_relative_paths(
    invalid: str,
) -> None:
    with pytest.raises(BootstrapError, match="logical path"):
        build_snapshot_scope(
            TRANSACTION,
            MANIFEST,
            {"steam": (artifact(invalid, "valid"),)},
        )
    with pytest.raises(BootstrapError, match="relative path"):
        build_snapshot_scope(
            TRANSACTION,
            MANIFEST,
            {"steam": (artifact("valid", invalid),)},
        )


@pytest.mark.parametrize(
    "invalid",
    (
        "managed/line\nbreak",
        "managed/unit\x1fseparator",
        "managed/delete\x7fcharacter",
        "managed/c1\x85character",
        "managed/high-surrogate\ud800",
        "managed/low-surrogate\udc80",
    ),
)
def test_scope_construction_rejects_controls_and_unpaired_surrogates(
    invalid: str,
) -> None:
    with pytest.raises(BootstrapError, match="logical path"):
        build_snapshot_scope(
            TRANSACTION,
            MANIFEST,
            {"steam": (artifact(invalid, "valid"),)},
        )
    with pytest.raises(BootstrapError, match="relative path"):
        build_snapshot_scope(
            TRANSACTION,
            MANIFEST,
            {"steam": (artifact("valid", invalid),)},
        )


def test_scope_construction_normalizes_filesystem_encoding_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid = "managed/unencodable"
    real_fsencode = os.fsencode

    def fail_selected_value(
        value: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ) -> bytes:
        if value == invalid:
            raise UnicodeEncodeError("utf-8", invalid, 0, 1, "injected failure")
        return real_fsencode(value)

    monkeypatch.setattr(snapshot_module.os, "fsencode", fail_selected_value)

    with pytest.raises(BootstrapError) as failure:
        build_snapshot_scope(
            TRANSACTION,
            MANIFEST,
            {"steam": (artifact(invalid, "valid"),)},
        )

    assert str(failure.value) == "snapshot logical path is invalid"
    assert invalid not in str(failure.value)


@pytest.mark.parametrize("invalid", (".", "a//b"))
def test_capture_rejects_noncanonical_path_without_indexerror(
    tmp_path: Path, invalid: str
) -> None:
    steam_root = tmp_path / "steam"
    steam_root.mkdir()
    scope = SnapshotScope(
        1,
        TRANSACTION,
        MANIFEST,
        (SnapshotTarget("managed/item", "steam", invalid),),
    )

    with pytest.raises(BootstrapError, match="relative path"):
        capture_snapshot(scope, {"steam": steam_root})


@pytest.mark.parametrize("invalid", (".", "a//b", [], {}, Path("path-like")))
def test_rule_rejects_noncanonical_or_nonstr_logical_path(invalid: object) -> None:
    item = record("guard/a", None)

    with pytest.raises(BootstrapError, match="logical path"):
        compare_snapshots(
            snapshot(item),
            snapshot(item),
            (ChangeRule(invalid, "unchanged", None, None),),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("invalid", (".", "a//b", [], {}, True))
def test_deserialize_rejects_noncanonical_or_nonstr_logical_path(
    tmp_path: Path, invalid: object
) -> None:
    os.chmod(tmp_path, 0o700)
    source = tmp_path / "snapshot.json"
    write_snapshot_value(
        source,
        snapshot_value(
            records=[
                {
                    "logical_path": invalid,
                    "state": "absent",
                    "mode": None,
                    "size": None,
                    "sha256": None,
                    "private": False,
                }
            ]
        ),
    )

    with pytest.raises(BootstrapError, match="logical path"):
        read_snapshot(source)


@pytest.mark.parametrize(
    "invalid",
    ("managed/line\nbreak", "managed/c1\x85character", "managed/surrogate\ud800"),
)
@pytest.mark.parametrize("private", (False, True))
def test_read_rejects_unsafe_record_identifier_without_leaking(
    tmp_path: Path, invalid: str, private: bool
) -> None:
    os.chmod(tmp_path, 0o700)
    source = tmp_path / "snapshot.json"
    write_snapshot_value(
        source,
        snapshot_value(records=[raw_record_value(invalid, private=private)]),
    )

    with pytest.raises(BootstrapError) as failure:
        read_snapshot(source)

    expected = (
        "private snapshot input is invalid"
        if private
        else "snapshot logical path is invalid"
    )
    assert str(failure.value) == expected
    assert invalid not in str(failure.value)


@pytest.mark.parametrize(
    ("field", "invalid"),
    (
        ("transaction_id", True),
        ("transaction_id", []),
        ("transaction_id", {}),
        ("manifest_sha256", True),
        ("manifest_sha256", []),
        ("steam_root_id", True),
        ("steam_root_id", []),
        ("steam_root_id", {}),
        ("steam_root_id", "/private/absolute/root"),
        ("steam_root_id", "01:2"),
        ("steam_root_id", "1:-2"),
    ),
)
def test_deserialize_normalizes_adversarial_identity_types(
    tmp_path: Path, field: str, invalid: object
) -> None:
    os.chmod(tmp_path, 0o700)
    source = tmp_path / "snapshot.json"
    write_snapshot_value(source, snapshot_value(**{field: invalid}))

    with pytest.raises(BootstrapError, match="snapshot"):
        read_snapshot(source)


@pytest.mark.parametrize(
    ("field", "invalid"),
    (
        ("transaction_id", Path("not-a-token")),
        ("manifest_sha256", Path("not-a-digest")),
        ("steam_root_id", Path("1:2")),
    ),
)
def test_direct_snapshot_validation_normalizes_pathlike_identity_values(
    field: str, invalid: object
) -> None:
    value = snapshot(**{field: invalid})

    with pytest.raises(BootstrapError, match="snapshot"):
        canonical_snapshot_bytes(value)


def test_rule_validation_normalizes_unhashable_policy() -> None:
    item = record("guard/a", b"same")
    malformed = ChangeRule("guard/a", [], item.sha256, item.mode)  # type: ignore[arg-type]

    with pytest.raises(BootstrapError, match="policy"):
        compare_snapshots(snapshot(item), snapshot(item), (malformed,))


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


def test_capture_rejects_file_mutated_during_streaming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steam_root = tmp_path / "steam"
    steam_root.mkdir()
    target = steam_root / "changing"
    target.write_bytes(b"before")
    scope = build_snapshot_scope(
        TRANSACTION,
        MANIFEST,
        {"steam": (artifact("managed/changing", "changing"),)},
    )
    real_read = os.read
    changed = False

    def mutate_after_read(fd: int, size: int) -> bytes:
        nonlocal changed
        block = real_read(fd, size)
        if block and not changed:
            changed = True
            target.write_bytes(b"replacement with different size")
        return block

    monkeypatch.setattr(snapshot_module.os, "read", mutate_after_read)

    with pytest.raises(BootstrapError, match="changed while hashing"):
        capture_snapshot(scope, {"steam": steam_root})


def test_capture_rejects_parent_swap_during_streaming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steam_root = tmp_path / "steam"
    parent = steam_root / "managed"
    parent.mkdir(parents=True)
    target = parent / "target"
    target.write_bytes(b"same-size")
    scope = build_snapshot_scope(
        TRANSACTION,
        MANIFEST,
        {"steam": (artifact("managed/target", "managed/target"),)},
    )
    real_read = os.read
    swapped = False
    before_fds = len(os.listdir("/proc/self/fd"))

    def swap_parent_after_read(fd: int, size: int) -> bytes:
        nonlocal swapped
        block = real_read(fd, size)
        if block and not swapped:
            swapped = True
            parent.rename(steam_root / "old-managed")
            parent.mkdir()
            (parent / "target").write_bytes(b"new-bytes")
        return block

    monkeypatch.setattr(snapshot_module.os, "read", swap_parent_after_read)

    with pytest.raises(BootstrapError, match="changed while hashing"):
        capture_snapshot(scope, {"steam": steam_root})

    assert len(os.listdir("/proc/self/fd")) == before_fds


def test_capture_rejects_parent_swap_when_new_leaf_is_same_inode_hardlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steam_root = tmp_path / "steam"
    parent = steam_root / "managed"
    replacement = steam_root / "replacement-managed"
    parent.mkdir(parents=True)
    replacement.mkdir()
    target = parent / "target"
    target.write_bytes(b"same-inode")
    os.link(target, replacement / "target")
    scope = build_snapshot_scope(
        TRANSACTION,
        MANIFEST,
        {"steam": (artifact("managed/target", "managed/target"),)},
    )
    real_read = os.read
    swapped = False
    before_fds = len(os.listdir("/proc/self/fd"))

    def swap_parent_after_read(fd: int, size: int) -> bytes:
        nonlocal swapped
        block = real_read(fd, size)
        if block and not swapped:
            swapped = True
            parent.rename(steam_root / "old-managed")
            replacement.rename(parent)
        return block

    monkeypatch.setattr(snapshot_module.os, "read", swap_parent_after_read)

    with pytest.raises(BootstrapError, match="changed while hashing"):
        capture_snapshot(scope, {"steam": steam_root})

    assert len(os.listdir("/proc/self/fd")) == before_fds


def test_capture_rejects_absent_target_after_parent_chain_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steam_root = tmp_path / "steam"
    parent = steam_root / "managed"
    parent.mkdir(parents=True)
    scope = build_snapshot_scope(
        TRANSACTION,
        MANIFEST,
        {"steam": (artifact("managed/missing", "managed/missing"),)},
    )
    real_open = os.open
    swapped = False
    before_fds = len(os.listdir("/proc/self/fd"))

    def swap_parent_after_absence(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        try:
            return real_open(path, flags, mode, dir_fd=dir_fd)
        except FileNotFoundError:
            if path == "missing" and not swapped:
                swapped = True
                parent.rename(steam_root / "old-managed")
                parent.mkdir()
            raise

    monkeypatch.setattr(snapshot_module.os, "open", swap_parent_after_absence)

    with pytest.raises(BootstrapError, match="changed while hashing"):
        capture_snapshot(scope, {"steam": steam_root})

    assert len(os.listdir("/proc/self/fd")) == before_fds


def test_capture_rejects_symlink_in_absolute_root_ancestor(tmp_path: Path) -> None:
    actual_parent = tmp_path / "actual-parent"
    steam_root = actual_parent / "steam"
    steam_root.mkdir(parents=True)
    (steam_root / "target").write_bytes(b"must not be captured through link")
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(actual_parent, target_is_directory=True)
    scope = build_snapshot_scope(
        TRANSACTION,
        MANIFEST,
        {"steam": (artifact("managed/target", "target"),)},
    )
    before_fds = len(os.listdir("/proc/self/fd"))

    with pytest.raises(BootstrapError, match="root is not a regular directory"):
        capture_snapshot(scope, {"steam": linked_parent / "steam"})

    assert len(os.listdir("/proc/self/fd")) == before_fds


@pytest.mark.parametrize("failure_point", ("fstat", "validation"))
def test_capture_closes_target_fd_when_post_open_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    steam_root = tmp_path / "steam"
    steam_root.mkdir()
    (steam_root / "target").write_bytes(b"data")
    scope = build_snapshot_scope(
        TRANSACTION,
        MANIFEST,
        {"steam": (artifact("managed/target", "target"),)},
    )
    before = len(os.listdir("/proc/self/fd"))
    if failure_point == "fstat":
        real_fstat = os.fstat

        def fail_regular_fstat(fd: int) -> os.stat_result:
            info = real_fstat(fd)
            if stat.S_ISREG(info.st_mode):
                raise OSError(errno.EIO, "injected fstat failure")
            return info

        monkeypatch.setattr(snapshot_module.os, "fstat", fail_regular_fstat)
        expected = BootstrapError
    else:

        def fail_validation(_mode: int) -> bool:
            raise RuntimeError("injected validation failure")

        monkeypatch.setattr(snapshot_module.stat, "S_ISREG", fail_validation)
        expected = RuntimeError

    with pytest.raises(expected):
        capture_snapshot(scope, {"steam": steam_root})

    assert len(os.listdir("/proc/self/fd")) == before


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


@pytest.mark.parametrize(
    "invalid",
    ("managed/line\nbreak", "managed/c1\x85character", "managed/surrogate\ud800"),
)
@pytest.mark.parametrize("private", (False, True))
def test_capture_rejects_unsafe_direct_relative_path_without_leaking(
    tmp_path: Path, invalid: str, private: bool
) -> None:
    steam_root = tmp_path / "steam"
    steam_root.mkdir()
    scope = SnapshotScope(
        1,
        TRANSACTION,
        MANIFEST,
        (SnapshotTarget("managed/item", "steam", invalid, private),),
    )

    with pytest.raises(BootstrapError) as failure:
        capture_snapshot(scope, {"steam": steam_root})

    expected = (
        "private snapshot capture failed"
        if private
        else "snapshot relative path is invalid"
    )
    assert str(failure.value) == expected
    assert invalid not in str(failure.value)


@pytest.mark.parametrize("invalid", (None, 1, "targets", [], {}))
def test_capture_rejects_invalid_target_collections(tmp_path: Path, invalid: object) -> None:
    steam_root = tmp_path / "steam"
    steam_root.mkdir()
    scope = SnapshotScope(1, TRANSACTION, MANIFEST, invalid)  # type: ignore[arg-type]

    with pytest.raises(BootstrapError) as failure:
        capture_snapshot(scope, {"steam": steam_root})

    assert str(failure.value) == "snapshot targets are invalid"


@pytest.mark.parametrize("private", (False, True))
def test_capture_normalizes_embedded_nul_root_without_path_leak(
    private: bool,
) -> None:
    logical_path = "private/sensitive-component" if private else "managed/item"
    root = "/private/root\x00hidden"
    scope = build_snapshot_scope(
        TRANSACTION,
        MANIFEST,
        {"steam": (artifact(logical_path, "item", private=private),)},
    )

    with pytest.raises(BootstrapError) as failure:
        capture_snapshot(scope, {"steam": root})

    expected = (
        "private snapshot capture failed" if private else "snapshot root is invalid"
    )
    assert str(failure.value) == expected
    assert root not in str(failure.value)
    assert logical_path not in str(failure.value)


@pytest.mark.parametrize("invalid", (None, 1, "rules", {"bad": "shape"}, {1, 2}))
def test_compare_rejects_invalid_rule_collections(invalid: object) -> None:
    item = record("guard/a", b"same")

    with pytest.raises(BootstrapError) as failure:
        compare_snapshots(snapshot(item), snapshot(item), invalid)  # type: ignore[arg-type]

    assert str(failure.value) == "comparison rules are invalid"


def test_compare_rejects_invalid_rule_inside_valid_collection() -> None:
    item = record("guard/a", b"same")

    with pytest.raises(BootstrapError) as failure:
        compare_snapshots(snapshot(item), snapshot(item), (object(),))  # type: ignore[arg-type]

    assert str(failure.value) == "comparison rule is invalid"


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


@pytest.mark.parametrize("operation", ("read", "write"))
def test_snapshot_file_io_normalizes_missing_and_nul_paths(
    tmp_path: Path, operation: str
) -> None:
    os.chmod(tmp_path, 0o700)
    missing = tmp_path / "missing" / "sensitive-name.json"
    nul_path = f"{tmp_path}/sensitive-name.json\x00hidden"
    action = (
        (lambda path: read_snapshot(path))
        if operation == "read"
        else (lambda path: write_snapshot(path, snapshot()))
    )
    expected = (
        "snapshot input could not be read"
        if operation == "read"
        else "snapshot output could not be written"
    )

    for path in (missing, nul_path):
        with pytest.raises(BootstrapError) as failure:
            action(path)
        assert str(failure.value) == expected
        assert "sensitive-name" not in str(failure.value)
        assert str(tmp_path) not in str(failure.value)


@pytest.mark.parametrize("operation", ("read", "write"))
def test_snapshot_file_io_normalizes_unencodable_surrogate_path(
    tmp_path: Path, operation: str
) -> None:
    os.chmod(tmp_path, 0o700)
    unsafe = f"{tmp_path}/sensitive-surrogate-\ud800.json"
    action = (
        (lambda: read_snapshot(unsafe))
        if operation == "read"
        else (lambda: write_snapshot(unsafe, snapshot()))
    )

    with pytest.raises(BootstrapError) as failure:
        action()

    expected = (
        "snapshot input could not be read"
        if operation == "read"
        else "snapshot output could not be written"
    )
    assert str(failure.value) == expected
    assert "sensitive-surrogate" not in str(failure.value)


@pytest.mark.parametrize("private", (False, True))
def test_capture_normalizes_unencodable_surrogate_root(
    private: bool,
) -> None:
    logical_path = "private/item" if private else "managed/item"
    scope = build_snapshot_scope(
        TRANSACTION,
        MANIFEST,
        {"steam": (artifact(logical_path, "item", private=private),)},
    )
    root = "/sensitive-root-\ud800"

    with pytest.raises(BootstrapError) as failure:
        capture_snapshot(scope, {"steam": root})

    expected = "private snapshot capture failed" if private else "snapshot root is invalid"
    assert str(failure.value) == expected
    assert "sensitive-root" not in str(failure.value)


@pytest.mark.parametrize(
    "invalid",
    ("managed/line\nbreak", "managed/c1\x85character", "managed/surrogate\ud800"),
)
@pytest.mark.parametrize("private", (False, True))
def test_render_rejects_unsafe_identifier_without_leaking(
    invalid: str, private: bool
) -> None:
    item = record(invalid, b"same", private=private)
    comparison = Comparison(
        expected_changes=(),
        required_unchanged=(ComparedRecord(invalid, item, item, "unchanged"),),
        unexpected_changes=(),
    )

    with pytest.raises(BootstrapError) as failure:
        render_comparison(comparison)

    expected = (
        "private snapshot comparison failed"
        if private
        else "comparison report logical path is invalid"
    )
    assert str(failure.value) == expected
    assert invalid not in str(failure.value)


def test_render_escapes_non_ascii_quotes_backslashes_and_line_separator() -> None:
    logical_path = 'managed/caf\u00e9"item\\tail\u2028next'
    item = record(logical_path, b"same")
    comparison = Comparison(
        expected_changes=(),
        required_unchanged=(ComparedRecord(logical_path, item, item, "unchanged"),),
        unexpected_changes=(),
    )

    report = render_comparison(comparison)

    assert '- managed/caf\\u00e9\\"item\\\\tail\\u2028next\n' in report
    assert "caf\u00e9" not in report
    assert "\u2028" not in report


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


def test_cli_snapshot_failed_publication_is_controlled_and_nonidentifying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    os.chmod(tmp_path, 0o700)
    output = tmp_path / "missing" / "sensitive-output.json"
    monkeypatch.setattr(cli_module, "_capture_current_snapshot", lambda: snapshot())

    assert main(["snapshot", "--output", str(output)]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: snapshot output could not be written\n"
    assert output.name not in captured.err
    assert str(tmp_path) not in captured.err


def test_cli_compare_missing_input_is_controlled_and_nonidentifying(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    os.chmod(tmp_path, 0o700)
    before = tmp_path / "sensitive-before.json"
    after = tmp_path / "sensitive-after.json"

    assert main(["compare", "--before", str(before), "--after", str(after)]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: snapshot input could not be read\n"
    assert before.name not in captured.err
    assert after.name not in captured.err
    assert str(tmp_path) not in captured.err


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
