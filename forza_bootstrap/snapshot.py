# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Finite, descriptor-relative SHA-256 evidence for managed bootstrap paths."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TextIO

from .model import ArtifactSpec, BootstrapError, canonical_json
from .safeio import atomic_write_private_json, open_owned_root, read_private_json

_SNAPSHOT_VERSION = 1
_READ_SIZE = 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TRANSACTION_ID = re.compile(r"[0-9a-f]{24}\Z")
_ROOT_NAME = re.compile(r"[a-z][a-z0-9_-]*\Z")
_ROOT_ID = re.compile(r"(?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*)\Z")
_NONREGULAR_ERRNOS = frozenset({errno.ELOOP, errno.ENOTDIR, errno.ENXIO, errno.EISDIR})
_PRIVATE_CAPTURE_ERROR = "private snapshot capture failed"
_PRIVATE_OUTPUT_ERROR = "private snapshot requires explicit owner-only output"
_FILESYSTEM_ERROR = "snapshot filesystem access failed"


@dataclass(frozen=True)
class FileRecord:
    logical_path: str
    state: Literal["absent", "regular"]
    mode: int | None
    size: int | None
    sha256: str | None
    private: bool = False


@dataclass(frozen=True)
class ChangeRule:
    logical_path: str
    policy: Literal["expected", "unchanged"]
    after_sha256: str | None
    after_mode: int | None


@dataclass(frozen=True)
class Snapshot:
    version: int
    transaction_id: str
    manifest_sha256: str
    steam_root_id: str
    records: tuple[FileRecord, ...]


@dataclass(frozen=True)
class ComparedRecord:
    logical_path: str
    before: FileRecord
    after: FileRecord
    policy: Literal["expected", "unchanged"]


@dataclass(frozen=True)
class Comparison:
    expected_changes: tuple[ComparedRecord, ...]
    required_unchanged: tuple[ComparedRecord, ...]
    unexpected_changes: tuple[ComparedRecord, ...]

    @property
    def ok(self) -> bool:
        return not self.unexpected_changes


@dataclass(frozen=True)
class SnapshotTarget:
    logical_path: str
    root_name: str
    relative_path: str
    private: bool = False


@dataclass(frozen=True)
class SnapshotScope:
    version: int
    transaction_id: str
    manifest_sha256: str
    targets: tuple[SnapshotTarget, ...]


def _logical_path(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise BootstrapError(f"{label} is invalid")
    if value.startswith("/") or any(part in {"", ".", ".."} for part in value.split("/")):
        raise BootstrapError(f"{label} is invalid")
    return value


def _sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise BootstrapError(f"{label} must be a lowercase SHA-256")
    return value


def _validate_identity(snapshot: Snapshot) -> None:
    if not isinstance(snapshot, Snapshot):
        raise BootstrapError("snapshot schema is invalid")
    if type(snapshot.version) is not int or snapshot.version != _SNAPSHOT_VERSION:
        raise BootstrapError("snapshot version is invalid")
    if (
        not isinstance(snapshot.transaction_id, str)
        or _TRANSACTION_ID.fullmatch(snapshot.transaction_id) is None
    ):
        raise BootstrapError("snapshot transaction_id is invalid")
    _sha256(snapshot.manifest_sha256, "snapshot manifest_sha256")
    if (
        not isinstance(snapshot.steam_root_id, str)
        or _ROOT_ID.fullmatch(snapshot.steam_root_id) is None
    ):
        raise BootstrapError("snapshot steam_root_id is invalid")


def _validate_record(item: FileRecord) -> None:
    if not isinstance(item, FileRecord):
        raise BootstrapError("snapshot record schema is invalid")
    _logical_path(item.logical_path, "snapshot logical path")
    if type(item.private) is not bool:
        raise BootstrapError("snapshot private flag is invalid")
    if item.state == "absent":
        if (item.mode, item.size, item.sha256) != (None, None, None):
            raise BootstrapError("absent snapshot record has file metadata")
        return
    if item.state != "regular":
        raise BootstrapError("snapshot record state is invalid")
    if type(item.mode) is not int or not 0 <= item.mode <= 0o7777:
        raise BootstrapError("snapshot record mode is invalid")
    if type(item.size) is not int or item.size < 0:
        raise BootstrapError("snapshot record size is invalid")
    _sha256(item.sha256, "snapshot record sha256")  # type: ignore[arg-type]


def _indexed_records(snapshot: Snapshot) -> dict[str, FileRecord]:
    _validate_identity(snapshot)
    if type(snapshot.records) is not tuple:
        raise BootstrapError("snapshot records are invalid")
    indexed: dict[str, FileRecord] = {}
    for item in snapshot.records:
        _validate_record(item)
        if item.logical_path in indexed:
            if item.private or indexed[item.logical_path].private:
                raise BootstrapError(_PRIVATE_CAPTURE_ERROR)
            raise BootstrapError(f"duplicate snapshot logical path: {item.logical_path}")
        indexed[item.logical_path] = item
    return indexed


def build_snapshot_scope(
    transaction_id: str,
    manifest_sha256: str,
    artifacts_by_root: Mapping[str, Sequence[ArtifactSpec]],
) -> SnapshotScope:
    """Bind an explicit set of artifact paths to named logical roots."""
    if not isinstance(transaction_id, str) or _TRANSACTION_ID.fullmatch(transaction_id) is None:
        raise BootstrapError("snapshot transaction_id is invalid")
    _sha256(manifest_sha256, "snapshot manifest_sha256")
    if not isinstance(artifacts_by_root, Mapping):
        raise BootstrapError("snapshot artifact scope is invalid")
    targets: list[SnapshotTarget] = []
    seen: set[str] = set()
    for root_name, artifacts in artifacts_by_root.items():
        if not isinstance(root_name, str) or _ROOT_NAME.fullmatch(root_name) is None:
            raise BootstrapError("snapshot root name is invalid")
        for artifact in artifacts:
            if not isinstance(artifact, ArtifactSpec) or type(artifact.private) is not bool:
                raise BootstrapError("snapshot artifact is invalid")
            logical_path = _logical_path(artifact.logical_name, "snapshot logical path")
            relative_path = _logical_path(artifact.relative_path, "snapshot relative path")
            if logical_path in seen:
                if artifact.private or any(
                    target.logical_path == logical_path and target.private
                    for target in targets
                ):
                    raise BootstrapError(_PRIVATE_CAPTURE_ERROR)
                raise BootstrapError(f"duplicate snapshot logical path: {logical_path}")
            seen.add(logical_path)
            targets.append(
                SnapshotTarget(logical_path, root_name, relative_path, artifact.private)
            )
    return SnapshotScope(
        _SNAPSHOT_VERSION,
        transaction_id,
        manifest_sha256,
        tuple(sorted(targets, key=lambda item: item.logical_path)),
    )


def _open_root(path: Path, name: str) -> int:
    if not path.is_absolute():
        raise BootstrapError(f"snapshot root must be absolute: {name}")
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        if error.errno in _NONREGULAR_ERRNOS:
            raise BootstrapError(f"snapshot root is not a regular directory: {name}") from error
        raise


def _open_target(root_fd: int, target: SnapshotTarget) -> int | None:
    parts = tuple(target.relative_path.split("/"))
    parent_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                return None
            except OSError as error:
                if error.errno in _NONREGULAR_ERRNOS:
                    raise BootstrapError(
                        f"snapshot target is not regular: {target.logical_path}"
                    ) from error
                raise
            os.close(parent_fd)
            parent_fd = next_fd
        try:
            fd = os.open(
                parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            return None
        except OSError as error:
            if error.errno in _NONREGULAR_ERRNOS:
                raise BootstrapError(
                    f"snapshot target is not regular: {target.logical_path}"
                ) from error
            raise
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(fd)
            raise BootstrapError(f"snapshot target is not regular: {target.logical_path}")
        return fd
    finally:
        os.close(parent_fd)


def _capture_target(root_fd: int, target: SnapshotTarget) -> FileRecord:
    fd = _open_target(root_fd, target)
    if fd is None:
        return FileRecord(target.logical_path, "absent", None, None, None, target.private)
    try:
        before = os.fstat(fd)
        digest = hashlib.sha256()
        while block := os.read(fd, _READ_SIZE):
            digest.update(block)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        stat.S_IMODE(before.st_mode),
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        stat.S_IMODE(after.st_mode),
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if not stat.S_ISREG(after.st_mode) or before_identity != after_identity:
        raise BootstrapError("snapshot target changed while hashing")
    return FileRecord(
        target.logical_path,
        "regular",
        stat.S_IMODE(after.st_mode),
        after.st_size,
        digest.hexdigest(),
        target.private,
    )


def _validated_targets(scope: SnapshotScope) -> tuple[SnapshotTarget, ...]:
    if not isinstance(scope, SnapshotScope):
        raise BootstrapError("snapshot scope is invalid")
    if type(scope.version) is not int or scope.version != _SNAPSHOT_VERSION:
        raise BootstrapError("snapshot scope version is invalid")
    if (
        not isinstance(scope.transaction_id, str)
        or _TRANSACTION_ID.fullmatch(scope.transaction_id) is None
    ):
        raise BootstrapError("snapshot transaction_id is invalid")
    _sha256(scope.manifest_sha256, "snapshot manifest_sha256")
    if type(scope.targets) is not tuple:
        raise BootstrapError("snapshot targets are invalid")
    seen: set[str] = set()
    targets: list[SnapshotTarget] = []
    for target in scope.targets:
        if not isinstance(target, SnapshotTarget):
            raise BootstrapError("snapshot target is invalid")
        logical_path = _logical_path(target.logical_path, "snapshot logical path")
        _logical_path(target.relative_path, "snapshot relative path")
        if (
            not isinstance(target.root_name, str)
            or _ROOT_NAME.fullmatch(target.root_name) is None
        ):
            raise BootstrapError("snapshot root name is invalid")
        if type(target.private) is not bool:
            raise BootstrapError("snapshot private flag is invalid")
        if logical_path in seen:
            raise BootstrapError(f"duplicate snapshot logical path: {logical_path}")
        seen.add(logical_path)
        targets.append(target)
    return tuple(sorted(targets, key=lambda item: item.logical_path))


def capture_snapshot(
    scope: SnapshotScope, roots: Mapping[str, str | os.PathLike[str]]
) -> Snapshot:
    """Capture exactly the files named by *scope*, without walking a tree."""
    private = (
        isinstance(scope, SnapshotScope)
        and type(scope.targets) is tuple
        and any(
            isinstance(target, SnapshotTarget) and target.private is True
            for target in scope.targets
        )
    )
    descriptors: dict[str, int] = {}
    try:
        targets = _validated_targets(scope)
        if not isinstance(roots, Mapping):
            raise BootstrapError("snapshot roots are invalid")
        if "steam" not in roots:
            raise BootstrapError("snapshot steam root is missing")
        required_roots = {target.root_name for target in targets} | {"steam"}
        missing = required_roots - set(roots)
        if missing:
            raise BootstrapError(f"snapshot root is missing: {min(missing)}")
        for name in sorted(required_roots):
            try:
                root_path = Path(roots[name])
            except TypeError as error:
                raise BootstrapError("snapshot root is invalid") from error
            descriptors[name] = _open_root(root_path, name)
        root_info = os.fstat(descriptors["steam"])
        root_id = f"{root_info.st_dev}:{root_info.st_ino}"
        records = tuple(
            _capture_target(descriptors[target.root_name], target)
            for target in targets
        )
    except (BootstrapError, OSError) as error:
        if private:
            raise BootstrapError(_PRIVATE_CAPTURE_ERROR) from None
        if isinstance(error, OSError):
            raise BootstrapError(_FILESYSTEM_ERROR) from None
        raise
    finally:
        for fd in descriptors.values():
            os.close(fd)
    return Snapshot(
        scope.version,
        scope.transaction_id,
        scope.manifest_sha256,
        root_id,
        records,
    )


def _record_value(item: FileRecord) -> dict[str, object]:
    return {
        "logical_path": item.logical_path,
        "state": item.state,
        "mode": item.mode,
        "size": item.size,
        "sha256": item.sha256,
        "private": item.private,
    }


def _snapshot_value(snapshot: Snapshot) -> dict[str, object]:
    records = _indexed_records(snapshot)
    return {
        "version": snapshot.version,
        "transaction_id": snapshot.transaction_id,
        "manifest_sha256": snapshot.manifest_sha256,
        "steam_root_id": snapshot.steam_root_id,
        "records": [_record_value(records[path]) for path in sorted(records)],
    }


def canonical_snapshot_bytes(snapshot: Snapshot) -> bytes:
    """Serialize a validated snapshot in stable logical-path order."""
    value = _snapshot_value(snapshot)
    if any(item.private for item in snapshot.records):
        raise BootstrapError(_PRIVATE_OUTPUT_ERROR)
    return canonical_json(value)


def _snapshot_from_value(value: object) -> Snapshot:
    keys = {"version", "transaction_id", "manifest_sha256", "steam_root_id", "records"}
    if not isinstance(value, dict) or set(value) != keys or not isinstance(value["records"], list):
        raise BootstrapError("snapshot schema is invalid")
    records: list[FileRecord] = []
    record_keys = {"logical_path", "state", "mode", "size", "sha256", "private"}
    for raw in value["records"]:
        if not isinstance(raw, dict) or set(raw) != record_keys:
            raise BootstrapError("snapshot record schema is invalid")
        records.append(
            FileRecord(
                raw["logical_path"],
                raw["state"],
                raw["mode"],
                raw["size"],
                raw["sha256"],
                raw["private"],
            )
        )
    snapshot = Snapshot(
        value["version"],
        value["transaction_id"],
        value["manifest_sha256"],
        value["steam_root_id"],
        tuple(records),
    )
    _indexed_records(snapshot)
    return snapshot


def write_snapshot(path: str | os.PathLike[str], snapshot: Snapshot) -> None:
    """Atomically write one validated snapshot to an absolute private path."""
    output = Path(path)
    if not output.is_absolute():
        raise BootstrapError("snapshot output path must be absolute")
    parent_fd = open_owned_root(output.parent)
    try:
        atomic_write_private_json(parent_fd, output.name, _snapshot_value(snapshot))
    finally:
        os.close(parent_fd)


def read_snapshot(path: str | os.PathLike[str]) -> Snapshot:
    """Read one absolute owner-only snapshot without following links."""
    source = Path(path)
    if not source.is_absolute():
        raise BootstrapError("snapshot input path must be absolute")
    parent_fd = open_owned_root(source.parent)
    try:
        return _snapshot_from_value(read_private_json(parent_fd, source.name))
    finally:
        os.close(parent_fd)


def output_snapshot(snapshot: Snapshot, path: str | os.PathLike[str] | None, stream: TextIO) -> None:
    """Print a snapshot by default, writing only when explicitly requested."""
    if path is None:
        stream.write(canonical_snapshot_bytes(snapshot).decode("ascii") + "\n")
        return
    write_snapshot(path, snapshot)


def _validate_rule(rule: ChangeRule) -> None:
    if not isinstance(rule, ChangeRule):
        raise BootstrapError("comparison rule is invalid")
    _logical_path(rule.logical_path, "comparison rule logical path")
    if not isinstance(rule.policy, str) or rule.policy not in {"expected", "unchanged"}:
        raise BootstrapError("comparison rule policy is invalid")
    if (rule.after_sha256 is None) != (rule.after_mode is None):
        raise BootstrapError("comparison rule after state is invalid")
    if rule.after_sha256 is not None:
        _sha256(rule.after_sha256, "comparison rule after_sha256")
        if type(rule.after_mode) is not int or not 0 <= rule.after_mode <= 0o7777:
            raise BootstrapError("comparison rule after_mode is invalid")


def _matches_rule(item: FileRecord, rule: ChangeRule) -> bool:
    if rule.after_sha256 is None:
        return item.state == "absent"
    return (
        item.state == "regular"
        and item.sha256 == rule.after_sha256
        and item.mode == rule.after_mode
    )


def compare_snapshots(
    before: Snapshot, after: Snapshot, rules: Sequence[ChangeRule]
) -> Comparison:
    """Classify a complete, identity-bound snapshot pair deterministically."""
    before_records = _indexed_records(before)
    after_records = _indexed_records(after)
    for field in ("transaction_id", "manifest_sha256", "steam_root_id"):
        if getattr(before, field) != getattr(after, field):
            raise BootstrapError(f"snapshot {field} does not match")
    if before.version != after.version:
        raise BootstrapError("snapshot version does not match")
    if set(before_records) != set(after_records):
        raise BootstrapError("snapshot record coverage does not match")
    indexed_rules: dict[str, ChangeRule] = {}
    private_paths = {
        logical_path for logical_path, item in before_records.items() if item.private
    }
    for rule in rules:
        _validate_rule(rule)
        if rule.logical_path in indexed_rules:
            if rule.logical_path in private_paths:
                raise BootstrapError(_PRIVATE_CAPTURE_ERROR)
            raise BootstrapError(f"duplicate comparison rule: {rule.logical_path}")
        indexed_rules[rule.logical_path] = rule
    if set(indexed_rules) != set(before_records):
        raise BootstrapError("comparison rule coverage does not match snapshot records")

    expected: list[ComparedRecord] = []
    unchanged: list[ComparedRecord] = []
    unexpected: list[ComparedRecord] = []
    for logical_path in sorted(before_records):
        before_item = before_records[logical_path]
        after_item = after_records[logical_path]
        rule = indexed_rules[logical_path]
        compared = ComparedRecord(logical_path, before_item, after_item, rule.policy)
        exact_after = _matches_rule(after_item, rule)
        if rule.policy == "expected" and before_item != after_item and exact_after:
            expected.append(compared)
        elif rule.policy == "unchanged" and before_item == after_item and exact_after:
            unchanged.append(compared)
        else:
            unexpected.append(compared)
    return Comparison(tuple(expected), tuple(unchanged), tuple(unexpected))


def render_comparison(comparison: Comparison) -> str:
    """Render a digest-free human report, fully redacting private records."""
    lines: list[str] = []
    categories = (
        ("expected changes", comparison.expected_changes),
        ("required unchanged", comparison.required_unchanged),
        ("unexpected changes", comparison.unexpected_changes),
    )
    for label, records in categories:
        lines.append(f"{label}:")
        for item in records:
            if item.before.private or item.after.private:
                lines.append("- [private local digest verified]")
            else:
                lines.append(f"- {item.logical_path}")
    lines.append(f"result: {'ok' if comparison.ok else 'unexpected changes'}")
    return "\n".join(lines) + "\n"
