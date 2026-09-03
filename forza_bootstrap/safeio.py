# SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Private, descriptor-relative filesystem primitives for bootstrap state."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import os
import secrets
import stat
from typing import Any

from .model import BootstrapError, canonical_json

DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_RENAME_NOREPLACE = 1
_NONREGULAR_OPEN_ERRNOS = frozenset({errno.ENXIO, errno.EISDIR, errno.ENODEV})
BOOTSTRAP_LOCK_NAME = "bootstrap.lock"
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEAT2 = getattr(_LIBC, "renameat2", None)
if _RENAMEAT2 is not None:
    _RENAMEAT2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    _RENAMEAT2.restype = ctypes.c_int


def _path_parts(path: str | os.PathLike[str]) -> tuple[str, ...]:
    value = os.fspath(path)
    if not os.path.isabs(value):
        raise BootstrapError(f"private path must be absolute: {value}")
    parts = value.split("/")[1:]
    if any(part in {"", ".", ".."} for part in parts):
        raise BootstrapError(f"unsafe private path: {value}")
    return tuple(parts)


def _leaf_name(name: str) -> None:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise BootstrapError(f"unsafe private file name: {name!r}")


def _open_absolute_directory(path: str | os.PathLike[str], *, create: bool) -> tuple[int, bool]:
    parts = _path_parts(path)
    fd = os.open("/", DIRECTORY)
    created_final = False
    try:
        for index, part in enumerate(parts):
            try:
                next_fd = os.open(part, DIRECTORY, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, _PRIVATE_DIRECTORY_MODE, dir_fd=fd)
                os.fsync(fd)
                try:
                    next_fd = os.open(part, DIRECTORY, dir_fd=fd)
                except OSError as error:
                    if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                        raise BootstrapError(f"symlink refused in private path: {part}") from error
                    raise
                if index == len(parts) - 1:
                    created_final = True
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise BootstrapError(f"symlink refused in private path: {part}") from error
                raise
            os.close(fd)
            fd = next_fd
        return fd, created_final
    except Exception:
        os.close(fd)
        raise


def _validate_private_directory(fd: int, label: str) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise BootstrapError(f"{label} is not a directory")
    if info.st_uid != os.getuid():
        raise BootstrapError(f"{label} has unsafe ownership")
    if stat.S_IMODE(info.st_mode) != _PRIVATE_DIRECTORY_MODE:
        raise BootstrapError(f"{label} has unsafe mode")


def ensure_private_directory(path: str | os.PathLike[str]) -> None:
    """Create *path* if absent, or verify it is an owner-only directory."""
    fd, created = _open_absolute_directory(path, create=True)
    try:
        if created:
            os.fchmod(fd, _PRIVATE_DIRECTORY_MODE)
            os.fsync(fd)
        _validate_private_directory(fd, "private directory")
    finally:
        os.close(fd)


def open_owned_root(path: str | os.PathLike[str]) -> int:
    """Open an existing owner-only private directory without following links."""
    fd, _ = _open_absolute_directory(path, create=False)
    try:
        _validate_private_directory(fd, "private directory")
        return fd
    except Exception:
        os.close(fd)
        raise


def _validate_private_regular(fd: int, name: str) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise BootstrapError(f"private file is not regular: {name}")
    if info.st_uid != os.getuid():
        raise BootstrapError(f"private file has unsafe ownership: {name}")
    if stat.S_IMODE(info.st_mode) != _PRIVATE_FILE_MODE:
        raise BootstrapError(f"private file has unsafe mode: {name}")


def _open_private_regular(parent_fd: int, name: str) -> int:
    _leaf_name(name)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise BootstrapError(f"symlink refused for private file: {name}") from error
        if error.errno in _NONREGULAR_OPEN_ERRNOS:
            raise BootstrapError(f"private file is not regular: {name}") from error
        raise
    try:
        _validate_private_regular(fd, name)
        return fd
    except Exception:
        os.close(fd)
        raise


def write_all(fd: int, data: bytes) -> None:
    """Write all bytes or fail rather than silently accepting a short write."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise BootstrapError("private file write made no progress")
        view = view[written:]


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def read_private_json(parent_fd: int, name: str) -> Any:
    """Read one owner-only JSON file by descriptor without link traversal."""
    _validate_private_directory(parent_fd, "private JSON parent")
    fd = _open_private_regular(parent_fd, name)
    try:
        chunks: list[bytes] = []
        while block := os.read(fd, 1024 * 1024):
            chunks.append(block)
    finally:
        os.close(fd)
    try:
        return json.loads(
            b"".join(chunks).decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise BootstrapError(f"private JSON is invalid: {name}") from error


def atomic_write_private_json(parent_fd: int, name: str, value: Any) -> None:
    """Durably replace an owner-only JSON file in an already-private directory."""
    _validate_private_directory(parent_fd, "private JSON parent")
    _leaf_name(name)
    try:
        data = canonical_json(value)
    except (TypeError, ValueError) as error:
        raise BootstrapError(f"private JSON cannot be encoded: {name}") from error

    try:
        existing_fd = _open_private_regular(parent_fd, name)
    except FileNotFoundError:
        existing_fd = None
    if existing_fd is not None:
        os.close(existing_fd)

    temporary = ""
    renamed = False
    try:
        for _ in range(100):
            temporary = f".{name}.{secrets.token_hex(12)}.tmp"
            try:
                fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    _PRIVATE_FILE_MODE,
                    dir_fd=parent_fd,
                )
                break
            except FileExistsError:
                continue
        else:
            raise BootstrapError("cannot reserve private JSON temporary file")
        try:
            os.fchmod(fd, _PRIVATE_FILE_MODE)
            _validate_private_regular(fd, temporary)
            write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        renamed = True
        os.fsync(parent_fd)
    except Exception:
        if temporary and not renamed:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        raise


def rename_noreplace(source_fd: int, source: str, destination_fd: int, destination: str) -> None:
    """Rename only when the destination is absent, or fail closed if unsupported."""
    _leaf_name(source)
    _leaf_name(destination)
    if _RENAMEAT2 is None:
        raise BootstrapError("renameat2 RENAME_NOREPLACE is unavailable")
    result = _RENAMEAT2(
        source_fd,
        os.fsencode(source),
        destination_fd,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.ENOSYS, errno.EINVAL}:
        raise BootstrapError("renameat2 RENAME_NOREPLACE is unavailable")
    raise OSError(error_number, os.strerror(error_number), destination)


def acquire_bootstrap_lock(runtime_dir: str | os.PathLike[str]) -> int:
    """Return a non-blocking, held owner-only lock descriptor."""
    ensure_private_directory(runtime_dir)
    parent_fd = open_owned_root(runtime_dir)
    fd: int | None = None
    try:
        try:
            fd = os.open(
                BOOTSTRAP_LOCK_NAME,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                _PRIVATE_FILE_MODE,
                dir_fd=parent_fd,
            )
        except FileExistsError:
            try:
                fd = os.open(
                    BOOTSTRAP_LOCK_NAME,
                    os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=parent_fd,
                )
            except OSError as error:
                if error.errno == errno.ELOOP:
                    raise BootstrapError("symlink refused for bootstrap lock") from error
                if error.errno in _NONREGULAR_OPEN_ERRNOS:
                    raise BootstrapError("bootstrap lock is not a regular file") from error
                raise
            _validate_private_regular(fd, BOOTSTRAP_LOCK_NAME)
        else:
            os.fchmod(fd, _PRIVATE_FILE_MODE)
            _validate_private_regular(fd, BOOTSTRAP_LOCK_NAME)
            os.fsync(fd)
            os.fsync(parent_fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BootstrapError("bootstrap lock is already held") from error
        result = fd
        fd = None
        return result
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


def _assert_bootstrap_lock(runtime_dir: str | os.PathLike[str], lock_fd: int) -> None:
    """Ensure *lock_fd* names and exclusively locks this runtime's lock file."""
    parent_fd = open_owned_root(runtime_dir)
    lock_path_fd: int | None = None
    try:
        lock_path_fd = _open_private_regular(parent_fd, BOOTSTRAP_LOCK_NAME)
        try:
            supplied = os.fstat(lock_fd)
        except OSError as error:
            raise BootstrapError("bootstrap lock descriptor is invalid") from error
        _validate_private_regular(lock_fd, BOOTSTRAP_LOCK_NAME)
        on_disk = os.fstat(lock_path_fd)
        if (supplied.st_dev, supplied.st_ino) != (on_disk.st_dev, on_disk.st_ino):
            raise BootstrapError("bootstrap lock descriptor does not match state root")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BootstrapError("bootstrap lock is already held") from error
        except OSError as error:
            raise BootstrapError("bootstrap lock descriptor cannot be locked") from error
    finally:
        if lock_path_fd is not None:
            os.close(lock_path_fd)
        os.close(parent_fd)
