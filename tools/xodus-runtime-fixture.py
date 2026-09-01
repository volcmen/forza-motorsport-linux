#!/usr/bin/env python3
"""Hermetic Xodus main-stream and social-UI fixture for WineGDK tests."""

from __future__ import annotations

import argparse
import os
import socket
import struct
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

XML_MAGIC = 0x58445358
HEADER = struct.Struct("<IHH")
TOKEN_RESPONSE = (
    b"<MSATokenResponse><Token>fixture-token</Token>"
    b"<Expiry>4102444800</Expiry></MSATokenResponse>\0"
)
INVITE_URI = (
    b"ms-xbl-multiplayer://inviteAccept?"
    b"invitedUser=123456789&sender=987654321&connectionString=fixture"
)


def read_exact(stream: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    while size:
        chunk = stream.recv(size)
        if not chunk:
            raise EOFError("peer closed the transport")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def send_xml(stream: socket.socket, message_type: int, body: bytes) -> None:
    stream.sendall(HEADER.pack(XML_MAGIC, message_type, len(body)) + body)


def serve_main(
    stream: socket.socket,
    initial: bytes,
    scenario: str,
    socket_path: Path,
    invite_connected: threading.Event,
) -> None:
    prefix = initial
    with stream:
        while True:
            try:
                header = prefix + read_exact(stream, HEADER.size - len(prefix))
                prefix = b""
            except EOFError:
                return

            magic, message_type, size = HEADER.unpack(header)
            body = read_exact(stream, size)
            if magic != XML_MAGIC:
                raise RuntimeError("main stream used an unexpected frame magic")
            if message_type == 1:
                send_xml(stream, 2, body)
                if scenario == "invite-subscriber-unavailable":
                    socket_path.unlink(missing_ok=True)
                if scenario == "invite-main-failure":
                    if not invite_connected.wait(timeout=5):
                        raise RuntimeError("invite subscriber did not connect")
                    return
            elif message_type == 3:
                try:
                    request = ET.fromstring(body.rstrip(b"\0").decode("utf-8"))
                except (UnicodeDecodeError, ET.ParseError) as error:
                    raise RuntimeError("MSA request body was not valid UTF-8 XML") from error
                expected = {
                    "ClientId": "0000000040000000",
                    "AllowUi": "false",
                    "MsaFullTrust": "false",
                }
                if request.tag != "MsaTokenRequest" or {
                    child.tag: child.text for child in request
                } != expected:
                    raise RuntimeError("MSA request body had unexpected fields")
                send_xml(stream, 4, TOKEN_RESPONSE)
                if scenario == "unavailable":
                    socket_path.unlink(missing_ok=True)
            else:
                raise RuntimeError(f"unexpected main-stream message type {message_type}")


def serve_social(
    stream: socket.socket, initial: bytes, scenario: str, ready_marker: Path | None
) -> None:
    with stream:
        handshake = initial + read_exact(stream, 6 - len(initial))
        if handshake != b"XDUI\x01\x01":
            raise RuntimeError("social UI did not request protocol v1 invite-only mode")

        if scenario == "immediate-fail":
            stream.sendall(b"\x02")
        elif scenario == "preack-timeout":
            time.sleep(6)
        elif scenario == "success":
            stream.sendall(b"\xa1\x00")
        elif scenario == "user-cancel":
            stream.sendall(b"\xa1\x01")
        elif scenario == "wait-cancel":
            stream.sendall(b"\xa1")
            if ready_marker:
                ready_marker.write_text("ready\n", encoding="ascii")
            while stream.recv(1):
                pass
        else:
            raise RuntimeError(f"unexpected social scenario {scenario}")


def serve_invite(
    stream: socket.socket,
    scenario: str,
    attempt: int,
    invite_connected: threading.Event,
    ready_marker: Path | None,
) -> None:
    with stream:
        invite_connected.set()
        if scenario == "invite-reconnect" and attempt == 1:
            return
        if scenario in ("invite-unregister", "invite-main-failure"):
            while stream.recv(1):
                pass
            return
        if scenario == "invite-multiple":
            if not ready_marker:
                raise RuntimeError("multiple-registration fixture needs a barrier")
            for _ in range(500):
                if ready_marker.exists():
                    break
                time.sleep(0.01)
            else:
                raise RuntimeError("multiple-registration barrier timed out")

        frame = struct.pack("<I", len(INVITE_URI)) + INVITE_URI
        for byte in frame:
            stream.sendall(bytes((byte,)))
        while stream.recv(1):
            pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("socket_path", type=Path)
    parser.add_argument(
        "--scenario",
        required=True,
        choices=(
            "success",
            "user-cancel",
            "immediate-fail",
            "wait-cancel",
            "unavailable",
            "preack-timeout",
            "invite-delivery",
            "invite-self-unregister",
            "invite-unregister",
            "invite-reconnect",
            "invite-multiple",
            "invite-default-queue",
            "invite-wait-pending",
            "invite-concurrent-waiters",
            "invite-main-failure",
            "invite-subscriber-unavailable",
        ),
    )
    parser.add_argument("--ready-marker", type=Path)
    args = parser.parse_args()

    if args.socket_path.exists():
        raise SystemExit(f"refusing to replace existing path: {args.socket_path}")
    args.socket_path.parent.mkdir(parents=True, exist_ok=True)
    if args.ready_marker:
        args.ready_marker.unlink(missing_ok=True)

    if args.scenario in ("unavailable", "invite-subscriber-unavailable"):
        expected_connections = 1
    elif args.scenario == "invite-reconnect":
        expected_connections = 3
    else:
        expected_connections = 2
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    failures: list[BaseException] = []
    workers: list[threading.Thread] = []
    invite_attempts = 0
    invite_attempts_lock = threading.Lock()
    invite_connected = threading.Event()

    def serve(stream: socket.socket) -> None:
        nonlocal invite_attempts
        try:
            initial = read_exact(stream, 4)
            if initial == HEADER.pack(XML_MAGIC, 0, 0)[:4]:
                serve_main(
                    stream,
                    initial,
                    args.scenario,
                    args.socket_path,
                    invite_connected,
                )
            elif initial == b"XDUI":
                serve_social(stream, initial, args.scenario, args.ready_marker)
            elif initial == b"XDSI":
                with invite_attempts_lock:
                    invite_attempts += 1
                    attempt = invite_attempts
                serve_invite(
                    stream,
                    args.scenario,
                    attempt,
                    invite_connected,
                    args.ready_marker,
                )
            else:
                raise RuntimeError(f"unexpected transport preamble {initial!r}")
        except BaseException as error:  # noqa: BLE001 - surfaced on the main thread
            failures.append(error)
            stream.close()

    try:
        listener.bind(str(args.socket_path))
        os.chmod(args.socket_path, 0o600)
        listener.listen(expected_connections)
        print("READY", flush=True)

        for index in range(expected_connections):
            stream, _ = listener.accept()
            worker = threading.Thread(
                target=serve, args=(stream,), name=f"xodus-fixture-{index}"
            )
            worker.start()
            workers.append(worker)

        for worker in workers:
            worker.join(timeout=20)
            if worker.is_alive():
                raise RuntimeError(f"{worker.name} did not terminate")
        if failures:
            raise failures[0]
    finally:
        listener.close()
        args.socket_path.unlink(missing_ok=True)
        if args.ready_marker:
            args.ready_marker.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
