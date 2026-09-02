#!/usr/bin/env python3
"""Exercise the legacy XGameRuntime Unix transport with local synthetic data."""

from __future__ import annotations

import argparse
import ctypes
import os
import socket
import struct
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

MAX_INVITE_URI = 4096
STATUS_SUCCESS = 0
STATUS_UNSUCCESSFUL = ctypes.c_int32(0xC0000001).value
STATUS_INVALID_BUFFER_SIZE = ctypes.c_int32(0xC0000206).value
STATUS_CONNECTION_REFUSED = ctypes.c_int32(0xC0000236).value

INVITE_CONNECT = 3
INVITE_POLL = 4
INVITE_CLOSE = 5
SOCIAL_UI = 6


class InvitePollArgs(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint32),
        ("uri", ctypes.c_ubyte * (MAX_INVITE_URI + 1)),
    ]


UnixCall = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p)
ConnectionHandler = Callable[[socket.socket], None]


def read_exact(stream: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    while size:
        chunk = stream.recv(size)
        if not chunk:
            raise EOFError("peer closed the synthetic transport")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


class CallTable:
    def __init__(self, library_path: Path) -> None:
        self.library = ctypes.CDLL(str(library_path))
        addresses = (ctypes.c_void_p * 7).in_dll(
            self.library, "__wine_unix_call_funcs"
        )
        self.calls = tuple(UnixCall(address) for address in addresses)

    def invoke(self, index: int, arguments: ctypes.Structure | None = None) -> int:
        pointer = None if arguments is None else ctypes.byref(arguments)
        return self.calls[index](pointer)


@contextmanager
def private_fixture(handler: ConnectionHandler) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="xgameruntime-legacy-") as directory:
        path = Path(directory) / "xodus.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        os.chmod(path, 0o600)
        if path.stat().st_mode & 0o777 != 0o600:
            raise AssertionError("fixture socket is not private")
        listener.listen(1)
        listener.settimeout(0.1)
        stopped = threading.Event()
        failures: list[BaseException] = []

        def serve() -> None:
            try:
                while not stopped.is_set():
                    try:
                        stream, _ = listener.accept()
                        break
                    except TimeoutError:
                        continue
                else:
                    return
                with stream:
                    handler(stream)
            except Exception as error:  # noqa: BLE001 - surfaced on caller thread
                failures.append(error)

        thread = threading.Thread(
            target=serve, name="legacy-xodus-fixture", daemon=True
        )
        thread.start()
        previous = os.environ.get("WINEGDK_XODUS_SOCKET")
        os.environ["WINEGDK_XODUS_SOCKET"] = str(path)
        try:
            yield path
        finally:
            if previous is None:
                os.environ.pop("WINEGDK_XODUS_SOCKET", None)
            else:
                os.environ["WINEGDK_XODUS_SOCKET"] = previous
            stopped.set()
            listener.close()
            thread.join(timeout=2)
            if thread.is_alive():
                raise AssertionError("fixture did not terminate")
            if failures:
                raise failures[0]


def require(actual: int, expected: int, operation: str) -> None:
    if actual != expected:
        raise AssertionError(
            f"{operation} returned {actual & 0xFFFFFFFF:#010x}, "
            f"expected {expected & 0xFFFFFFFF:#010x}"
        )


def send_fragmented(stream: socket.socket, payload: bytes) -> None:
    """Force the native read_all loop to assemble fragmented reads."""
    for byte in payload:
        stream.sendall(bytes([byte]))


def test_fragmented_invite(calls: CallTable) -> None:
    uri = b"ms-xbl-multiplayer://inviteAccept?fixture=synthetic-local"

    def handler(stream: socket.socket) -> None:
        if read_exact(stream, 4) != b"XDSI":
            raise AssertionError("legacy invite handshake changed")
        send_fragmented(stream, struct.pack("<I", len(uri)) + uri)

    with private_fixture(handler):
        require(calls.invoke(INVITE_CONNECT), STATUS_SUCCESS, "XDSI connect")
        arguments = InvitePollArgs()
        require(calls.invoke(INVITE_POLL, arguments), STATUS_SUCCESS, "XDSI poll")
        if arguments.length != len(uri):
            raise AssertionError("legacy invite length changed")
        if bytes(arguments.uri[: arguments.length]) != uri:
            raise AssertionError("legacy invite payload changed")
        if arguments.uri[arguments.length] != 0:
            raise AssertionError("legacy invite payload was not terminated")
        require(calls.invoke(INVITE_CLOSE), STATUS_SUCCESS, "XDSI close")


def test_oversized_invite(calls: CallTable) -> None:
    def handler(stream: socket.socket) -> None:
        if read_exact(stream, 4) != b"XDSI":
            raise AssertionError("legacy invite handshake changed")
        send_fragmented(stream, struct.pack("<I", MAX_INVITE_URI + 1))

    with private_fixture(handler):
        require(calls.invoke(INVITE_CONNECT), STATUS_SUCCESS, "large XDSI connect")
        require(
            calls.invoke(INVITE_POLL, InvitePollArgs()),
            STATUS_INVALID_BUFFER_SIZE,
            "oversized XDSI rejection",
        )
        require(calls.invoke(INVITE_CLOSE), STATUS_SUCCESS, "large XDSI close")


def test_social_success(calls: CallTable) -> None:
    def handler(stream: socket.socket) -> None:
        if read_exact(stream, 4) != b"XDUI":
            raise AssertionError("legacy social handshake changed")
        send_fragmented(stream, b"\x00")

    with private_fixture(handler):
        require(calls.invoke(SOCIAL_UI), STATUS_SUCCESS, "XDUI success")


def test_social_failure(calls: CallTable) -> None:
    def handler(stream: socket.socket) -> None:
        if read_exact(stream, 4) != b"XDUI":
            raise AssertionError("legacy social handshake changed")
        stream.sendall(b"\x01")

    with private_fixture(handler):
        require(calls.invoke(SOCIAL_UI), STATUS_UNSUCCESSFUL, "XDUI failure")


def test_close_then_connection_refusal(calls: CallTable) -> None:
    with tempfile.TemporaryDirectory(prefix="xgameruntime-refusal-") as directory:
        missing = Path(directory) / "missing.sock"
        previous = os.environ.get("WINEGDK_XODUS_SOCKET")
        os.environ["WINEGDK_XODUS_SOCKET"] = str(missing)
        try:
            require(calls.invoke(INVITE_CLOSE), STATUS_SUCCESS, "subscriber close")
            require(
                calls.invoke(INVITE_CONNECT),
                STATUS_CONNECTION_REFUSED,
                "connection refusal",
            )
        finally:
            if previous is None:
                os.environ.pop("WINEGDK_XODUS_SOCKET", None)
            else:
                os.environ["WINEGDK_XODUS_SOCKET"] = previous


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("library", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.library.is_file():
        raise SystemExit(f"Unix library does not exist: {args.library}")

    calls = CallTable(args.library)
    test_fragmented_invite(calls)
    test_oversized_invite(calls)
    test_social_success(calls)
    test_social_failure(calls)
    test_close_then_connection_refusal(calls)
    print("legacy XGameRuntime transport fixture passed")


if __name__ == "__main__":
    main()
