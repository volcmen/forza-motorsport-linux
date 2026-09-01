#!/usr/bin/env python3
"""Exercise WineGDK's Xodus Unix-call transport without a live account."""

from __future__ import annotations

import argparse
import ctypes
import os
import socket
import struct
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

XODUS_INVITE_URI_MAX = 4096
STATUS_SUCCESS = 0
STATUS_ACCESS_DENIED = ctypes.c_int32(0xC0000022).value
STATUS_CANCELLED = ctypes.c_int32(0xC0000120).value
STATUS_INVALID_NETWORK_RESPONSE = ctypes.c_int32(0xC00000C3).value

INVITE_CONNECT = 5
INVITE_POLL = 6
INVITE_CLOSE = 7
SOCIAL_UI_START = 8
SOCIAL_UI_WAIT = 9
SOCIAL_UI_CANCEL = 10


class InviteConnectArgs(ctypes.Structure):
    _fields_ = [("timeout_ms", ctypes.c_uint32)]


class InvitePollArgs(ctypes.Structure):
    _fields_ = [
        ("timeout_ms", ctypes.c_uint32),
        ("uri_length", ctypes.c_uint32),
        ("uri", ctypes.c_ubyte * (XODUS_INVITE_URI_MAX + 1)),
    ]


class SocialUiStartArgs(ctypes.Structure):
    _fields_ = [
        ("timeout_ms", ctypes.c_uint32),
        ("mode", ctypes.c_uint8),
        ("operation_id", ctypes.c_uint64),
    ]


class SocialUiWaitArgs(ctypes.Structure):
    _fields_ = [("operation_id", ctypes.c_uint64), ("status", ctypes.c_uint8)]


class SocialUiCancelArgs(ctypes.Structure):
    _fields_ = [("operation_id", ctypes.c_uint64)]


UnixCall = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p)
ConnectionHandler = Callable[[socket.socket], None]


def read_exact(stream: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    while size:
        chunk = stream.recv(size)
        if not chunk:
            raise EOFError("peer closed the transport")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


class CallTable:
    def __init__(self, library_path: Path) -> None:
        self.library = ctypes.CDLL(str(library_path))
        addresses = (ctypes.c_void_p * 11).in_dll(
            self.library, "__wine_unix_call_funcs"
        )
        self.calls = tuple(UnixCall(address) for address in addresses)

    def invoke(self, index: int, arguments: ctypes.Structure | None = None) -> int:
        pointer = None if arguments is None else ctypes.byref(arguments)
        return self.calls[index](pointer)


@contextmanager
def private_fixture(handler: ConnectionHandler) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="xgameruntime-transport-") as directory:
        path = Path(directory) / "xodus.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        os.chmod(path, 0o600)
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
            except Exception as error:  # noqa: BLE001 - surfaced on the test thread
                failures.append(error)

        thread = threading.Thread(target=serve, name="xodus-fixture", daemon=True)
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


def test_private_socket_permissions(calls: CallTable) -> None:
    with tempfile.TemporaryDirectory(prefix="xgameruntime-permissions-") as directory:
        path = Path(directory) / "xodus.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(path))
            os.chmod(path, 0o666)
            os.environ["WINEGDK_XODUS_SOCKET"] = str(path)
            result = calls.invoke(INVITE_CONNECT, InviteConnectArgs(250))
            require(result, STATUS_ACCESS_DENIED, "world-readable socket rejection")
        finally:
            os.environ.pop("WINEGDK_XODUS_SOCKET", None)
            listener.close()


def test_invite_stream(calls: CallTable) -> None:
    uri = (
        b"ms-xbl-multiplayer://inviteAccept?"
        b"invitedUser=123456789&sender=987654321&connectionString=fixture"
    )

    def handler(stream: socket.socket) -> None:
        assert read_exact(stream, 4) == b"XDSI"
        stream.sendall(struct.pack("<I", len(uri)) + uri)

    with private_fixture(handler):
        require(
            calls.invoke(INVITE_CONNECT, InviteConnectArgs(1000)),
            STATUS_SUCCESS,
            "XDSI connect",
        )
        arguments = InvitePollArgs(1000)
        require(calls.invoke(INVITE_POLL, arguments), STATUS_SUCCESS, "XDSI poll")
        received = bytes(arguments.uri[: arguments.uri_length])
        if received != uri:
            raise AssertionError("XDSI poll changed the validated invite URI")
        require(calls.invoke(INVITE_CLOSE), STATUS_SUCCESS, "XDSI close")


def test_invalid_invite_is_rejected(calls: CallTable) -> None:
    invalid_uris = (
        b"ms-xbl-multiplayer://inviteAccept?invitedUser=123&sender=456",
        (
            b"ms-xbl-multiplayer://inviteAccept?"
            b"invitedUser=18446744073709551616&sender=456&connectionString=fixture"
        ),
        (
            b"ms-xbl-multiplayer://inviteAccept?"
            b"invitedUser=123&sender=456&connectionString=fixture&unknown=value"
        ),
        (
            b"ms-xbl-multiplayer://inviteAccept?"
            b"invitedUser=123&sender=456&connectionString=fixture#fragment"
        ),
    )

    for uri in invalid_uris:

        def handler(stream: socket.socket, response: bytes = uri) -> None:
            assert read_exact(stream, 4) == b"XDSI"
            stream.sendall(struct.pack("<I", len(response)) + response)

        with private_fixture(handler):
            require(
                calls.invoke(INVITE_CONNECT, InviteConnectArgs(1000)),
                STATUS_SUCCESS,
                "invalid XDSI connect",
            )
            require(
                calls.invoke(INVITE_POLL, InvitePollArgs(1000)),
                STATUS_INVALID_NETWORK_RESPONSE,
                "invalid_network_response",
            )
            require(
                calls.invoke(INVITE_CLOSE), STATUS_SUCCESS, "invalid XDSI close"
            )


def test_invite_frame_has_one_deadline(calls: CallTable) -> None:
    def handler(stream: socket.socket) -> None:
        for byte in struct.pack("<I", 1):
            try:
                stream.sendall(bytes([byte]))
            except BrokenPipeError:
                return
            time.sleep(0.075)

    with private_fixture(handler):
        require(
            calls.invoke(INVITE_CONNECT, InviteConnectArgs(1000)),
            STATUS_SUCCESS,
            "slow XDSI connect",
        )
        started = time.monotonic()
        result = calls.invoke(INVITE_POLL, InvitePollArgs(100))
        elapsed = time.monotonic() - started
        if result == STATUS_SUCCESS:
            raise AssertionError("slow XDSI frame bypassed its deadline")
        if elapsed >= 0.25:
            raise AssertionError(f"slow XDSI frame took {elapsed:.3f}s")
        require(calls.invoke(INVITE_CLOSE), STATUS_SUCCESS, "slow XDSI close")


def test_social_terminal_states(calls: CallTable) -> None:
    for mode, terminal in ((0, 0), (1, 1)):

        def handler(
            stream: socket.socket,
            expected_mode: int = mode,
            expected_terminal: int = terminal,
        ) -> None:
            assert read_exact(stream, 6) == b"XDUI\x01" + bytes([expected_mode])
            stream.sendall(b"\xa1" + bytes([expected_terminal]))

        with private_fixture(handler):
            start = SocialUiStartArgs(1000, mode)
            require(
                calls.invoke(SOCIAL_UI_START, start),
                STATUS_SUCCESS,
                "XDUI start",
            )
            if not start.operation_id:
                raise AssertionError("XDUI start returned no operation identifier")
            wait = SocialUiWaitArgs(start.operation_id)
            require(
                calls.invoke(SOCIAL_UI_WAIT, wait),
                STATUS_SUCCESS,
                "XDUI wait",
            )
            if wait.status != terminal:
                raise AssertionError("XDUI terminal state changed in transport")


def test_social_immediate_terminal(calls: CallTable) -> None:
    def handler(stream: socket.socket) -> None:
        assert read_exact(stream, 6) == b"XDUI\x01\x01"
        stream.sendall(b"\x02")

    with private_fixture(handler):
        start = SocialUiStartArgs(1000, 1)
        require(
            calls.invoke(SOCIAL_UI_START, start),
            STATUS_SUCCESS,
            "immediate XDUI start",
        )
        wait = SocialUiWaitArgs(start.operation_id)
        require(
            calls.invoke(SOCIAL_UI_WAIT, wait),
            STATUS_SUCCESS,
            "immediate XDUI wait",
        )
        if wait.status != 2:
            raise AssertionError("immediate XDUI terminal state changed in transport")


def test_social_cancellation(calls: CallTable) -> None:
    waiting = threading.Event()

    def handler(stream: socket.socket) -> None:
        assert read_exact(stream, 6) == b"XDUI\x01\x00"
        stream.sendall(b"\xa1")
        waiting.set()
        while stream.recv(1):
            pass

    with private_fixture(handler):
        start = SocialUiStartArgs(1000, 0)
        require(
            calls.invoke(SOCIAL_UI_START, start), STATUS_SUCCESS, "cancel XDUI start"
        )
        if not waiting.wait(timeout=1):
            raise AssertionError("XDUI fixture did not enter its terminal wait")

        result: list[int] = []
        failures: list[Exception] = []

        def wait_for_terminal() -> None:
            try:
                result.append(
                    calls.invoke(SOCIAL_UI_WAIT, SocialUiWaitArgs(start.operation_id))
                )
            except Exception as error:  # noqa: BLE001 - surfaced after joining
                failures.append(error)

        thread = threading.Thread(target=wait_for_terminal, name="xdui-wait")
        thread.start()
        require(
            calls.invoke(SOCIAL_UI_CANCEL, SocialUiCancelArgs(start.operation_id)),
            STATUS_SUCCESS,
            "XDUI cancel",
        )
        thread.join(timeout=2)
        if thread.is_alive():
            raise AssertionError("cancelled XDUI wait did not terminate")
        if failures:
            raise failures[0]
        if not result:
            raise AssertionError("cancelled XDUI wait returned no status")
        require(result[0], STATUS_CANCELLED, "status_cancelled")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("unix_library", type=Path)
    args = parser.parse_args()
    library = args.unix_library.resolve(strict=True)
    calls = CallTable(library)

    tests = (
        test_private_socket_permissions,
        test_invite_stream,
        test_invalid_invite_is_rejected,
        test_invite_frame_has_one_deadline,
        test_social_terminal_states,
        test_social_immediate_terminal,
        test_social_cancellation,
    )
    for test in tests:
        test(calls)
        print(f"ok: {test.__name__}")
    print("all XGameRuntime transport tests passed")


if __name__ == "__main__":
    main()
