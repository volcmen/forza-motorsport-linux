# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Private local transaction for the licensed Microsoft threading runtime."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .model import BootstrapError
from .safeio import (
    DIRECTORY,
    atomic_write_private_json,
    ensure_private_directory,
    open_owned_root,
    read_private_json,
    rename_noreplace,
    write_all,
)
from .snapshot import FileRecord

MAX_LICENSED_SIZE = 256 * 1024 * 1024
_READ_SIZE = 1024 * 1024
_JOURNAL_NAME = "licensed.json"
_JOURNAL_VERSION = 1
_SOURCE_MODE = 0o644
_NONREGULAR_ERRNOS = frozenset(
    {errno.ELOOP, errno.ENOTDIR, errno.ENXIO, errno.EISDIR, errno.ENODEV}
)
_FileIdentity = tuple[int, int, int, int, int, int, int]


@dataclass(frozen=True)
class PrivateFileIdentity:
    size: int
    sha256: str


@dataclass(frozen=True)
class LicensedAction:
    destination_logical: str
    source_size: int
    source_sha256: str
    before: FileRecord
    disposition: Literal["install", "replace", "keep"]
    _source: Path = field(repr=False, compare=False)
    _destination: Path = field(repr=False, compare=False)

    def private_json(self) -> dict[str, object]:
        """Return the persistable private action without its process-only source."""
        return {
            "destination_logical": self.destination_logical,
            "source_size": self.source_size,
            "source_sha256": self.source_sha256,
            "before": _record_value(self.before),
            "disposition": self.disposition,
        }

    def public_summary(self) -> str:
        """Describe the action without exposing private paths or digests."""
        return (
            f"licensed threading runtime: {self.disposition}; "
            "[private local digest verified]"
        )


@dataclass(frozen=True)
class _Journal:
    destination_logical: str
    source_size: int
    source_sha256: str
    before: FileRecord
    disposition: Literal["install", "replace", "keep"]
    stage_name: str
    backup_name: str
    rollback_name: str
    recovery_name: str
    status: Literal["planned", "installed", "rolled_back", "recovery_required"]


def _absolute_path(path: str | os.PathLike[str], label: str) -> Path:
    try:
        value = os.fspath(path)
    except TypeError:
        raise BootstrapError(f"{label} must be an absolute path") from None
    if not isinstance(value, str) or not os.path.isabs(value) or "\x00" in value:
        raise BootstrapError(f"{label} must be an absolute path")
    parts = value.split("/")[1:]
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise BootstrapError(f"{label} must be an absolute path")
    return Path(value)


def _file_identity(info: os.stat_result) -> _FileIdentity:
    return (
        stat.S_IFMT(info.st_mode),
        info.st_dev,
        info.st_ino,
        info.st_size,
        stat.S_IMODE(info.st_mode),
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _open_parent(path: Path, label: str) -> int:
    fd = os.open("/", DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            try:
                next_fd = os.open(part, DIRECTORY, dir_fd=fd)
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise BootstrapError(f"symlink refused for {label}") from error
                raise BootstrapError(f"{label} parent is unavailable") from None
            os.close(fd)
            fd = next_fd
        info = os.fstat(fd)
        if info.st_uid != os.getuid():
            raise BootstrapError(f"{label} parent has unsafe ownership")
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_regular(parent_fd: int, name: str, label: str) -> int:
    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_fd,
        )
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise BootstrapError(f"symlink refused for {label}") from error
        if error.errno in _NONREGULAR_ERRNOS:
            raise BootstrapError(f"{label} is not a regular file") from error
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise BootstrapError(f"{label} is not a regular file")
        if info.st_uid != os.getuid():
            raise BootstrapError(f"{label} has unsafe ownership")
        return fd
    except Exception:
        os.close(fd)
        raise


def _hash_fd(fd: int, *, capped: bool = False) -> PrivateFileIdentity:
    before = os.fstat(fd)
    if capped and before.st_size > MAX_LICENSED_SIZE:
        raise BootstrapError("licensed input exceeds size limit")
    digest = hashlib.sha256()
    size = 0
    os.lseek(fd, 0, os.SEEK_SET)
    while block := os.read(fd, _READ_SIZE):
        size += len(block)
        if capped and size > MAX_LICENSED_SIZE:
            raise BootstrapError("licensed input exceeds size limit")
        digest.update(block)
    after = os.fstat(fd)
    if _file_identity(before) != _file_identity(after) or size != after.st_size:
        raise BootstrapError("licensed input changed while reading")
    return PrivateFileIdentity(size=size, sha256=digest.hexdigest())


def _validate_pe_fd(fd: int) -> PrivateFileIdentity:
    info = os.fstat(fd)
    if info.st_size > MAX_LICENSED_SIZE:
        raise BootstrapError("licensed input exceeds size limit")
    if info.st_size < 0x40:
        raise BootstrapError("licensed input has invalid DOS header")
    dos = os.pread(fd, 0x40, 0)
    if len(dos) != 0x40 or dos[:2] != b"MZ":
        raise BootstrapError("licensed input has invalid DOS header")
    pe_offset = int.from_bytes(dos[0x3C:0x40], "little")
    if pe_offset < 0x40 or pe_offset > info.st_size - 6:
        raise BootstrapError("licensed input has invalid PE offset")
    pe = os.pread(fd, 6, pe_offset)
    if len(pe) != 6 or pe[:4] != b"PE\0\0":
        raise BootstrapError("licensed input has invalid PE signature")
    if int.from_bytes(pe[4:6], "little") != 0x8664:
        raise BootstrapError("licensed input is not an x86_64 PE")
    identity = _hash_fd(fd, capped=True)
    if _file_identity(info) != _file_identity(os.fstat(fd)):
        raise BootstrapError("licensed input changed while validating")
    return identity


def _open_validated_source(path: Path) -> tuple[int, PrivateFileIdentity]:
    parent_fd = _open_parent(path, "licensed input")
    try:
        fd = _open_regular(parent_fd, path.name, "licensed input")
    except Exception:
        os.close(parent_fd)
        raise
    os.close(parent_fd)
    try:
        return fd, _validate_pe_fd(fd)
    except Exception:
        os.close(fd)
        raise


def validate_pe64(path: str | os.PathLike[str]) -> PrivateFileIdentity:
    """Validate and privately identify one local, user-owned x86_64 PE file."""
    source = _absolute_path(path, "licensed input")
    try:
        fd, identity = _open_validated_source(source)
    except BootstrapError:
        raise
    except OSError:
        raise BootstrapError("licensed input could not be read") from None
    os.close(fd)
    return identity


def _record_value(record: FileRecord) -> dict[str, object]:
    return {
        "logical_path": record.logical_path,
        "state": record.state,
        "mode": record.mode,
        "size": record.size,
        "sha256": record.sha256,
        "private": record.private,
    }


def _record_from_value(value: object) -> FileRecord:
    keys = {"logical_path", "state", "mode", "size", "sha256", "private"}
    if not isinstance(value, dict) or set(value) != keys:
        raise BootstrapError("licensed journal is invalid")
    logical = value["logical_path"]
    state_value = value["state"]
    mode = value["mode"]
    size = value["size"]
    digest = value["sha256"]
    private = value["private"]
    if not isinstance(logical, str) or not logical or type(private) is not bool:
        raise BootstrapError("licensed journal is invalid")
    if state_value == "absent":
        if (mode, size, digest) != (None, None, None):
            raise BootstrapError("licensed journal is invalid")
    elif state_value == "regular":
        if (
            type(mode) is not int
            or not 0 <= mode <= 0o7777
            or type(size) is not int
            or size < 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise BootstrapError("licensed journal is invalid")
    else:
        raise BootstrapError("licensed journal is invalid")
    return FileRecord(logical, state_value, mode, size, digest, private)  # type: ignore[arg-type]


def _absent_record(logical: str) -> FileRecord:
    return FileRecord(logical, "absent", None, None, None, True)


def _record_fd(fd: int, logical: str) -> FileRecord:
    info = os.fstat(fd)
    identity = _hash_fd(fd)
    return FileRecord(
        logical,
        "regular",
        stat.S_IMODE(info.st_mode),
        identity.size,
        identity.sha256,
        True,
    )


def _open_record(
    parent_fd: int, name: str, logical: str
) -> tuple[FileRecord, int | None]:
    try:
        fd = _open_regular(parent_fd, name, "licensed destination")
    except FileNotFoundError:
        return _absent_record(logical), None
    record = _record_fd(fd, logical)
    return record, fd


def plan_threading_copy(
    source: str | os.PathLike[str], destination: str | os.PathLike[str]
) -> LicensedAction:
    """Plan a private local copy without persisting the licensed source path."""
    source_path = _absolute_path(source, "licensed input")
    destination_path = _absolute_path(destination, "licensed destination")
    try:
        source_fd, source_identity = _open_validated_source(source_path)
        os.close(source_fd)
        parent_fd = _open_parent(destination_path, "licensed destination")
        try:
            before, before_fd = _open_record(
                parent_fd, destination_path.name, os.fspath(destination_path)
            )
            if before_fd is not None:
                os.close(before_fd)
        finally:
            os.close(parent_fd)
    except BootstrapError:
        raise
    except OSError:
        raise BootstrapError("licensed copy could not be planned") from None
    disposition: Literal["install", "replace", "keep"]
    if before.state == "absent":
        disposition = "install"
    elif (
        before.size == source_identity.size and before.sha256 == source_identity.sha256
    ):
        disposition = "keep"
    else:
        disposition = "replace"
    return LicensedAction(
        destination_logical=os.fspath(destination_path),
        source_size=source_identity.size,
        source_sha256=source_identity.sha256,
        before=before,
        disposition=disposition,
        _source=source_path,
        _destination=destination_path,
    )


def _journal_value(journal: _Journal) -> dict[str, object]:
    return {
        "version": _JOURNAL_VERSION,
        "destination_logical": journal.destination_logical,
        "source_size": journal.source_size,
        "source_sha256": journal.source_sha256,
        "before": _record_value(journal.before),
        "disposition": journal.disposition,
        "stage_name": journal.stage_name,
        "backup_name": journal.backup_name,
        "rollback_name": journal.rollback_name,
        "recovery_name": journal.recovery_name,
        "status": journal.status,
    }


def _safe_sibling_name(value: object, suffix: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith(".")
        or not value.endswith(suffix)
        or "/" in value
        or "\x00" in value
    ):
        raise BootstrapError("licensed journal is invalid")
    return value


def _journal_from_value(value: object) -> _Journal:
    keys = {
        "version",
        "destination_logical",
        "source_size",
        "source_sha256",
        "before",
        "disposition",
        "stage_name",
        "backup_name",
        "rollback_name",
        "recovery_name",
        "status",
    }
    if (
        not isinstance(value, dict)
        or set(value) != keys
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise BootstrapError("licensed journal is invalid")
    destination = value["destination_logical"]
    source_size = value["source_size"]
    source_sha256 = value["source_sha256"]
    disposition = value["disposition"]
    status = value["status"]
    if not isinstance(destination, str):
        raise BootstrapError("licensed journal is invalid")
    _absolute_path(destination, "licensed journal destination")
    if type(source_size) is not int or not 0 <= source_size <= MAX_LICENSED_SIZE:
        raise BootstrapError("licensed journal is invalid")
    if (
        not isinstance(source_sha256, str)
        or len(source_sha256) != 64
        or any(character not in "0123456789abcdef" for character in source_sha256)
    ):
        raise BootstrapError("licensed journal is invalid")
    if not isinstance(disposition, str) or disposition not in {
        "install",
        "replace",
        "keep",
    }:
        raise BootstrapError("licensed journal is invalid")
    if not isinstance(status, str) or status not in {
        "planned",
        "installed",
        "rolled_back",
        "recovery_required",
    }:
        raise BootstrapError("licensed journal is invalid")
    before = _record_from_value(value["before"])
    if before.logical_path != destination or not before.private:
        raise BootstrapError("licensed journal is invalid")
    before_matches_source = (
        before.state == "regular"
        and before.size == source_size
        and before.sha256 == source_sha256
    )
    if not (
        (disposition == "install" and before.state == "absent")
        or (disposition == "keep" and before_matches_source)
        or (
            disposition == "replace"
            and before.state == "regular"
            and not before_matches_source
        )
    ):
        raise BootstrapError("licensed journal is invalid")
    destination_name = Path(destination).name
    stage_name = _safe_sibling_name(value["stage_name"], ".licensed-stage")
    match = re.fullmatch(
        rf"\.{re.escape(destination_name)}\.([0-9a-f]{{24}})\.licensed-stage",
        stage_name,
    )
    if match is None:
        raise BootstrapError("licensed journal is invalid")
    stem = f".{destination_name}.{match.group(1)}"
    backup_name = _safe_sibling_name(value["backup_name"], ".licensed-backup")
    rollback_name = _safe_sibling_name(value["rollback_name"], ".rollback-held")
    recovery_name = _safe_sibling_name(value["recovery_name"], ".recovery")
    if (
        backup_name != f"{stem}.licensed-backup"
        or rollback_name != f"{stem}.rollback-held"
        or recovery_name != f"{stem}.recovery"
    ):
        raise BootstrapError("licensed journal is invalid")
    return _Journal(
        destination,
        source_size,
        source_sha256,
        before,
        disposition,  # type: ignore[arg-type]
        stage_name,
        backup_name,
        rollback_name,
        recovery_name,
        status,  # type: ignore[arg-type]
    )


def _new_journal(action: LicensedAction) -> _Journal:
    token = secrets.token_hex(12)
    stem = f".{action._destination.name}.{token}"
    return _Journal(
        action.destination_logical,
        action.source_size,
        action.source_sha256,
        action.before,
        action.disposition,
        f"{stem}.licensed-stage",
        f"{stem}.licensed-backup",
        f"{stem}.rollback-held",
        f"{stem}.recovery",
        "planned",
    )


def _write_journal(journal_fd: int, journal: _Journal) -> None:
    atomic_write_private_json(journal_fd, _JOURNAL_NAME, _journal_value(journal))


def _replace_status(journal: _Journal, status: str) -> _Journal:
    return _Journal(
        journal.destination_logical,
        journal.source_size,
        journal.source_sha256,
        journal.before,
        journal.disposition,
        journal.stage_name,
        journal.backup_name,
        journal.rollback_name,
        journal.recovery_name,
        status,  # type: ignore[arg-type]
    )


def _matches_source(record: FileRecord, journal: _Journal) -> bool:
    return (
        record.state == "regular"
        and record.size == journal.source_size
        and record.sha256 == journal.source_sha256
    )


def _matches_staged_source(record: FileRecord, journal: _Journal) -> bool:
    return _matches_source(record, journal) and record.mode == _SOURCE_MODE


def _same_named_file(parent_fd: int, name: str, held_fd: int) -> bool:
    try:
        named_fd = _open_regular(parent_fd, name, "licensed transaction file")
    except (FileNotFoundError, BootstrapError):
        return False
    try:
        return _file_identity(os.fstat(named_fd)) == _file_identity(os.fstat(held_fd))
    finally:
        os.close(named_fd)


def _preserve_partial_stage(parent_fd: int, journal: _Journal) -> None:
    held_fd = _open_regular(parent_fd, journal.stage_name, "licensed partial stage")
    try:
        if not _same_named_file(parent_fd, journal.stage_name, held_fd):
            raise BootstrapError("licensed partial stage changed before recovery")
        destination_name = Path(journal.destination_logical).name
        for _ in range(100):
            recovery_name = (
                f".{destination_name}.{secrets.token_hex(12)}.partial.recovery"
            )
            try:
                rename_noreplace(
                    parent_fd,
                    journal.stage_name,
                    parent_fd,
                    recovery_name,
                )
            except FileExistsError:
                continue
            os.fsync(parent_fd)
            recovered_fd = _open_regular(
                parent_fd, recovery_name, "licensed partial recovery"
            )
            try:
                if _file_identity(os.fstat(recovered_fd)) != _file_identity(
                    os.fstat(held_fd)
                ):
                    raise BootstrapError("licensed partial recovery evidence changed")
            finally:
                os.close(recovered_fd)
            return
        raise BootstrapError("licensed partial recovery name conflicts")
    finally:
        os.close(held_fd)


def _copy_stage(source_fd: int, parent_fd: int, journal: _Journal) -> None:
    source_generation = _file_identity(os.fstat(source_fd))
    while True:
        try:
            stage_fd = os.open(
                journal.stage_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                _SOURCE_MODE,
                dir_fd=parent_fd,
            )
            break
        except FileExistsError:
            record, existing_fd = _open_record(
                parent_fd, journal.stage_name, journal.destination_logical
            )
            if existing_fd is not None:
                os.close(existing_fd)
            if _matches_staged_source(record, journal):
                return
            if _matches_source(record, journal):
                raise BootstrapError("licensed stage conflicts") from None
            _preserve_partial_stage(parent_fd, journal)
    try:
        os.fchmod(stage_fd, _SOURCE_MODE)
        digest = hashlib.sha256()
        size = 0
        os.lseek(source_fd, 0, os.SEEK_SET)
        while block := os.read(source_fd, _READ_SIZE):
            size += len(block)
            if size > MAX_LICENSED_SIZE:
                raise BootstrapError("licensed input exceeds size limit")
            digest.update(block)
            write_all(stage_fd, block)
        if (
            size != journal.source_size
            or digest.hexdigest() != journal.source_sha256
            or _file_identity(os.fstat(source_fd)) != source_generation
        ):
            raise BootstrapError("licensed input changed after planning")
        os.fsync(stage_fd)
    finally:
        os.close(stage_fd)


def _quarantine_destination(
    parent_fd: int, journal_fd: int, journal: _Journal
) -> _Journal:
    _current, current_fd = _open_record(
        parent_fd, Path(journal.destination_logical).name, journal.destination_logical
    )
    if current_fd is None:
        return journal
    try:
        if not _same_named_file(
            parent_fd, Path(journal.destination_logical).name, current_fd
        ):
            raise BootstrapError("licensed recovery could not bind destination")
        try:
            rename_noreplace(
                parent_fd,
                Path(journal.destination_logical).name,
                parent_fd,
                journal.recovery_name,
            )
        except FileExistsError:
            raise BootstrapError("licensed recovery name conflicts") from None
        os.fsync(parent_fd)
        _recovered, recovered_fd = _open_record(
            parent_fd, journal.recovery_name, journal.destination_logical
        )
        if recovered_fd is None:
            raise BootstrapError("licensed recovery evidence is missing")
        try:
            if _file_identity(os.fstat(recovered_fd)) != _file_identity(
                os.fstat(current_fd)
            ):
                raise BootstrapError("licensed recovery evidence changed")
        finally:
            os.close(recovered_fd)
    finally:
        os.close(current_fd)
    updated = _replace_status(journal, "recovery_required")
    _write_journal(journal_fd, updated)
    return updated


def _load_or_create_journal(action: LicensedAction, journal_fd: int) -> _Journal:
    try:
        value = read_private_json(journal_fd, _JOURNAL_NAME)
    except FileNotFoundError:
        journal = _new_journal(action)
        _write_journal(journal_fd, journal)
        return journal
    journal = _journal_from_value(value)
    expected = action.private_json()
    actual = {
        "destination_logical": journal.destination_logical,
        "source_size": journal.source_size,
        "source_sha256": journal.source_sha256,
        "before": _record_value(journal.before),
        "disposition": journal.disposition,
    }
    if actual != expected or action._destination != Path(journal.destination_logical):
        raise BootstrapError("licensed action does not match private journal")
    return journal


def _validate_fresh_action(action: LicensedAction) -> tuple[int, Path]:
    if not isinstance(action, LicensedAction):
        raise BootstrapError("licensed action is invalid")
    try:
        destination = _absolute_path(
            action.destination_logical, "licensed action destination"
        )
        source = _absolute_path(action._source, "licensed action source")
    except BootstrapError:
        raise BootstrapError("licensed action is invalid") from None
    if (
        not isinstance(action._destination, Path)
        or action._destination != destination
        or type(action.source_size) is not int
        or not 0 <= action.source_size <= MAX_LICENSED_SIZE
        or not isinstance(action.source_sha256, str)
        or len(action.source_sha256) != 64
        or any(
            character not in "0123456789abcdef" for character in action.source_sha256
        )
        or not isinstance(action.before, FileRecord)
        or action.before.logical_path != action.destination_logical
        or not action.before.private
        or not isinstance(action.disposition, str)
        or action.disposition not in {"install", "replace", "keep"}
    ):
        raise BootstrapError("licensed action is invalid")
    try:
        before = _record_from_value(_record_value(action.before))
    except (AttributeError, BootstrapError):
        raise BootstrapError("licensed action is invalid") from None
    before_matches_source = (
        before.state == "regular"
        and before.size == action.source_size
        and before.sha256 == action.source_sha256
    )
    if not (
        (action.disposition == "install" and before.state == "absent")
        or (action.disposition == "keep" and before_matches_source)
        or (
            action.disposition == "replace"
            and before.state == "regular"
            and not before_matches_source
        )
    ):
        raise BootstrapError("licensed action is invalid")
    try:
        source_fd, source_identity = _open_validated_source(source)
    except BootstrapError as error:
        raise BootstrapError("licensed input validation failed") from error
    except OSError:
        raise BootstrapError("licensed input validation failed") from None
    if (
        source_identity.size != action.source_size
        or source_identity.sha256 != action.source_sha256
    ):
        os.close(source_fd)
        raise BootstrapError("licensed input changed after planning")
    return source_fd, destination


def _publish_stage(parent_fd: int, journal_fd: int, journal: _Journal) -> _Journal:
    stage_record, stage_fd = _open_record(
        parent_fd, journal.stage_name, journal.destination_logical
    )
    if stage_fd is None or not _matches_staged_source(stage_record, journal):
        if stage_fd is not None:
            os.close(stage_fd)
        raise BootstrapError("licensed stage conflicts")
    destination_name = Path(journal.destination_logical).name
    try:
        if (
            not _same_named_file(parent_fd, journal.stage_name, stage_fd)
            or stat.S_IMODE(os.fstat(stage_fd).st_mode) != _SOURCE_MODE
        ):
            raise BootstrapError("licensed stage changed before publication")
        try:
            rename_noreplace(parent_fd, journal.stage_name, parent_fd, destination_name)
        except FileExistsError:
            raise BootstrapError(
                "licensed destination changed before publication"
            ) from None
        os.fsync(parent_fd)
        destination, destination_fd = _open_record(
            parent_fd, destination_name, journal.destination_logical
        )
        try:
            if (
                destination_fd is None
                or not _matches_staged_source(destination, journal)
                or _file_identity(os.fstat(destination_fd))
                != _file_identity(os.fstat(stage_fd))
            ):
                _quarantine_destination(parent_fd, journal_fd, journal)
                raise BootstrapError(
                    "licensed publication changed; recovery evidence preserved"
                )
        finally:
            if destination_fd is not None:
                os.close(destination_fd)
    finally:
        os.close(stage_fd)
    installed = _replace_status(journal, "installed")
    _write_journal(journal_fd, installed)
    return installed


def install_threading_copy(
    action: LicensedAction, journal: str | os.PathLike[str]
) -> None:
    """Install or resume one private licensed-file transaction."""
    parent_fd: int | None = None
    source_fd: int | None = None
    journal_fd: int | None = None
    try:
        source_fd, destination = _validate_fresh_action(action)
        journal_path = _absolute_path(journal, "licensed journal")
        ensure_private_directory(journal_path)
        journal_fd = open_owned_root(journal_path)
        state = _load_or_create_journal(action, journal_fd)
        if state.status == "recovery_required":
            raise BootstrapError("licensed transaction requires recovery")
        if state.status == "rolled_back":
            raise BootstrapError("licensed transaction was already rolled back")
        parent_fd = _open_parent(destination, "licensed destination")
        current, current_fd = _open_record(
            parent_fd, destination.name, state.destination_logical
        )
        if current_fd is not None:
            os.close(current_fd)
        if state.disposition == "keep":
            if not _matches_source(current, state):
                raise BootstrapError("licensed destination changed after planning")
            if state.status != "installed":
                _write_journal(journal_fd, _replace_status(state, "installed"))
            return

        if _matches_staged_source(current, state):
            if state.disposition == "replace":
                backup, backup_fd = _open_record(
                    parent_fd, state.backup_name, state.destination_logical
                )
                if backup_fd is not None:
                    os.close(backup_fd)
                if backup != state.before:
                    raise BootstrapError("licensed backup or destination conflicts")
            if state.status != "installed":
                _write_journal(journal_fd, _replace_status(state, "installed"))
            return
        if _matches_source(current, state):
            _quarantine_destination(parent_fd, journal_fd, state)
            raise BootstrapError(
                "licensed publication mode changed; recovery evidence preserved"
            )

        _copy_stage(source_fd, parent_fd, state)
        if state.disposition == "replace":
            backup, backup_fd = _open_record(
                parent_fd, state.backup_name, state.destination_logical
            )
            if backup_fd is not None:
                os.close(backup_fd)
            current, current_fd = _open_record(
                parent_fd, destination.name, state.destination_logical
            )
            if current_fd is not None:
                os.close(current_fd)
            if backup.state == "absent" and current == state.before:
                held_fd = _open_regular(
                    parent_fd, destination.name, "licensed destination"
                )
                try:
                    held_info = os.fstat(held_fd)
                    held_inode = (held_info.st_dev, held_info.st_ino)
                    if not _same_named_file(parent_fd, destination.name, held_fd):
                        raise BootstrapError(
                            "licensed destination changed before backup"
                        )
                    try:
                        rename_noreplace(
                            parent_fd,
                            destination.name,
                            parent_fd,
                            state.backup_name,
                        )
                    except FileExistsError:
                        raise BootstrapError("licensed backup conflicts") from None
                    os.fsync(parent_fd)
                    moved_backup, moved_fd = _open_record(
                        parent_fd, state.backup_name, state.destination_logical
                    )
                    if moved_fd is None:
                        raise BootstrapError(
                            "licensed backup recovery evidence is missing"
                        )
                    try:
                        moved_info = os.fstat(moved_fd)
                        if (
                            moved_backup != state.before
                            or (moved_info.st_dev, moved_info.st_ino) != held_inode
                        ):
                            _write_journal(
                                journal_fd,
                                _replace_status(state, "recovery_required"),
                            )
                            raise BootstrapError(
                                "licensed backup changed; recovery evidence preserved"
                            )
                    finally:
                        os.close(moved_fd)
                finally:
                    os.close(held_fd)
                current = _absent_record(state.destination_logical)
                backup = state.before
            elif backup != state.before or (
                current.state != "absent" and not _matches_source(current, state)
            ):
                raise BootstrapError("licensed backup or destination conflicts")

        current, current_fd = _open_record(
            parent_fd, destination.name, state.destination_logical
        )
        if current_fd is not None:
            os.close(current_fd)
        if _matches_staged_source(current, state):
            if state.status != "installed":
                _write_journal(journal_fd, _replace_status(state, "installed"))
            return
        if _matches_source(current, state):
            _quarantine_destination(parent_fd, journal_fd, state)
            raise BootstrapError(
                "licensed publication mode changed; recovery evidence preserved"
            )
        if current.state != "absent":
            raise BootstrapError("licensed destination changed before publication")
        _publish_stage(parent_fd, journal_fd, state)
    except (BootstrapError, KeyboardInterrupt):
        raise
    except OSError:
        raise BootstrapError("licensed transaction failed") from None
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        if journal_fd is not None:
            os.close(journal_fd)


def _move_bound(
    parent_fd: int,
    source_name: str,
    destination_name: str,
    expected: FileRecord,
    logical: str,
) -> None:
    held_fd = _open_regular(parent_fd, source_name, "licensed transaction file")
    try:
        held_record = _record_fd(held_fd, logical)
        if held_record != expected or not _same_named_file(
            parent_fd, source_name, held_fd
        ):
            raise BootstrapError("licensed transaction file changed before rollback")
        try:
            rename_noreplace(parent_fd, source_name, parent_fd, destination_name)
        except FileExistsError:
            raise BootstrapError("licensed rollback destination conflicts") from None
        os.fsync(parent_fd)
        moved, moved_fd = _open_record(parent_fd, destination_name, logical)
        if moved_fd is None:
            raise BootstrapError("licensed rollback evidence is missing")
        try:
            if moved != expected or _file_identity(
                os.fstat(moved_fd)
            ) != _file_identity(os.fstat(held_fd)):
                raise BootstrapError("licensed rollback evidence changed")
        finally:
            os.close(moved_fd)
    finally:
        os.close(held_fd)


def rollback_threading_copy(journal: str | os.PathLike[str]) -> None:
    """Restore/remove only proven transaction-owned state, preserving conflicts."""
    journal_path = _absolute_path(journal, "licensed journal")
    journal_fd = open_owned_root(journal_path)
    parent_fd: int | None = None
    try:
        state = _journal_from_value(read_private_json(journal_fd, _JOURNAL_NAME))
        if state.status == "rolled_back":
            return
        destination = _absolute_path(
            state.destination_logical, "licensed journal destination"
        )
        parent_fd = _open_parent(destination, "licensed destination")
        current, current_fd = _open_record(
            parent_fd, destination.name, state.destination_logical
        )
        if current_fd is not None:
            os.close(current_fd)
        source_record = FileRecord(
            state.destination_logical,
            "regular",
            _SOURCE_MODE,
            state.source_size,
            state.source_sha256,
            True,
        )

        if state.disposition == "keep":
            if not _matches_source(current, state):
                raise BootstrapError("licensed no-op destination changed")
            _write_journal(journal_fd, _replace_status(state, "rolled_back"))
            return

        if current.state == "regular" and not _matches_source(current, state):
            if state.disposition == "replace" and current == state.before:
                backup, backup_fd = _open_record(
                    parent_fd, state.backup_name, state.destination_logical
                )
                if backup_fd is not None:
                    os.close(backup_fd)
                if backup.state == "absent":
                    _write_journal(journal_fd, _replace_status(state, "rolled_back"))
                    return
            _quarantine_destination(parent_fd, journal_fd, state)
            raise BootstrapError(
                "licensed destination changed; recovery evidence preserved"
            )

        if _matches_source(current, state):
            _move_bound(
                parent_fd,
                destination.name,
                state.rollback_name,
                source_record,
                state.destination_logical,
            )
            current = _absent_record(state.destination_logical)

        if state.disposition == "replace":
            backup, backup_fd = _open_record(
                parent_fd, state.backup_name, state.destination_logical
            )
            if backup_fd is not None:
                os.close(backup_fd)
            if current.state == "absent" and backup == state.before:
                try:
                    _move_bound(
                        parent_fd,
                        state.backup_name,
                        destination.name,
                        state.before,
                        state.destination_logical,
                    )
                except BootstrapError as error:
                    restored, restored_fd = _open_record(
                        parent_fd, destination.name, state.destination_logical
                    )
                    if restored_fd is not None:
                        os.close(restored_fd)
                    if restored.state == "regular":
                        _quarantine_destination(parent_fd, journal_fd, state)
                    else:
                        _write_journal(
                            journal_fd,
                            _replace_status(state, "recovery_required"),
                        )
                    raise BootstrapError(
                        "licensed restore changed; recovery evidence preserved"
                    ) from error
            elif current != state.before:
                raise BootstrapError("licensed rollback requires recovery")
        elif current.state != "absent":
            raise BootstrapError("licensed rollback requires recovery")

        _write_journal(journal_fd, _replace_status(state, "rolled_back"))
    except (BootstrapError, KeyboardInterrupt):
        raise
    except OSError:
        raise BootstrapError("licensed rollback failed") from None
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(journal_fd)
