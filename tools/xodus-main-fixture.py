#!/usr/bin/env python3
"""Minimal local Xodus main-stream fixture for WineGDK lifecycle tests."""

from __future__ import annotations

import argparse
import os
import socket
import struct
from pathlib import Path

XML_MAGIC = 0x58445358
HEADER = struct.Struct("<IHH")


def read_exact(stream: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.recv(remaining)
        if not chunk:
            raise EOFError
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def serve_connection(stream: socket.socket) -> None:
    with stream:
        while True:
            try:
                header = read_exact(stream, HEADER.size)
            except EOFError:
                return
            magic, message_type, size = HEADER.unpack(header)
            body = read_exact(stream, size)
            if magic != XML_MAGIC or message_type != 1:
                raise RuntimeError("fixture accepts only Xodus XML Ping frames")
            stream.sendall(HEADER.pack(XML_MAGIC, 2, len(body)) + body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("socket_path", type=Path)
    parser.add_argument("--connections", type=int, default=2)
    args = parser.parse_args()

    if args.socket_path.exists():
        raise SystemExit(f"refusing to replace existing path: {args.socket_path}")
    args.socket_path.parent.mkdir(parents=True, exist_ok=True)

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(args.socket_path))
        os.chmod(args.socket_path, 0o600)
        listener.listen(1)
        print("READY", flush=True)
        for _ in range(args.connections):
            stream, _ = listener.accept()
            serve_connection(stream)
    finally:
        listener.close()
        args.socket_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
