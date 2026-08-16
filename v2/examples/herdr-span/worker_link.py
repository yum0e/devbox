"""Terminal-side projection of one private Agent worker Link."""
from __future__ import annotations

import base64
import json
import os
import re
import signal
import socket
import stat
import struct
import sys
import termios
import threading
import tty
from pathlib import Path


HEADER = struct.Struct("!I")
FRAME = struct.Struct("!cI")
MAX_REQUEST = 64 * 1024
MAX_ARGV_BYTES = 16 * 1024
MARKER = re.compile(r"__DEVC2_HERDR_[0-9a-f]{32}_EXIT_")


class WorkerError(RuntimeError):
    pass


def decode(raw: str) -> dict:
    if len(raw) > MAX_REQUEST or not re.fullmatch(r"[A-Za-z0-9_=-]+", raw):
        raise WorkerError("invalid worker payload")
    try:
        document = json.loads(base64.urlsafe_b64decode(raw.encode()))
    except (ValueError, json.JSONDecodeError) as error:
        raise WorkerError("invalid worker payload") from error
    if not isinstance(document, dict) or set(document) != {"socket", "argv", "marker"}:
        raise WorkerError("invalid worker payload")
    path, argv, marker = document["socket"], document["argv"], document["marker"]
    if not isinstance(path, str) or not Path(path).is_absolute() or "\0" in path:
        raise WorkerError("invalid worker Link")
    if (not isinstance(argv, list) or not argv or len(argv) > 64
            or not all(isinstance(item, str) and "\0" not in item for item in argv)
            or sum(len(item.encode()) for item in argv) > MAX_ARGV_BYTES):
        raise WorkerError("invalid worker argv")
    if not isinstance(marker, str) or MARKER.fullmatch(marker) is None:
        raise WorkerError("invalid worker marker")
    return document


def checked_socket(raw: str) -> Path:
    path = Path(raw)
    metadata = path.lstat()
    if (not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077):
        raise WorkerError("unsafe worker Link")
    return path


def park() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    while True:
        threading.Event().wait(3600)


def failed(marker: str, error: Exception) -> None:
    print(f"\r\n{marker.replace('_EXIT_', '_START_', 1)}", flush=True)
    print(f"worker Link: {error}", file=sys.stderr, flush=True)
    print(f"\r\n{marker}125", flush=True)


def ready_marker(marker: str) -> bytes:
    return marker.replace("_EXIT_", "_READY_", 1).encode()


def receive_ready(marker: str) -> None:
    expected = ready_marker(marker)
    received = bytearray()
    while len(received) <= len(expected):
        byte = os.read(0, 1)
        if not byte:
            raise WorkerError("worker readiness ended early")
        if byte in (b"\r", b"\n"):
            if bytes(received) == expected:
                return
            break
        received.extend(byte)
    raise WorkerError("invalid worker readiness")


def receive(connection: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        block = connection.recv(size - len(result))
        if not block:
            raise WorkerError("Agent Link ended early")
        result.extend(block)
    return bytes(result)


def run(raw: str) -> int:
    document = decode(raw)
    connection = None
    try:
        path = checked_socket(document["socket"])
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.connect(os.fspath(path))
        request = json.dumps({"argv": document["argv"]}, separators=(",", ":")).encode()
        if len(request) > MAX_REQUEST:
            raise WorkerError("worker request exceeds 64 KiB")
        connection.sendall(HEADER.pack(len(request)) + request)
    except (OSError, WorkerError) as error:
        if connection is not None:
            connection.close()
        failed(document["marker"], error)
        park()

    saved = termios.tcgetattr(0) if os.isatty(0) else None
    if saved is not None:
        tty.setraw(0)
    print(f"\r\n{document['marker'].replace('_EXIT_', '_START_', 1)}", flush=True)
    stopping = threading.Event()

    def send_input() -> None:
        try:
            while not stopping.is_set():
                block = os.read(0, 64 * 1024)
                if not block:
                    connection.shutdown(socket.SHUT_WR)
                    return
                connection.sendall(block)
        except OSError:
            return

    try:
        try:
            receive_ready(document["marker"])
            thread = threading.Thread(target=send_input, daemon=True)
            thread.start()
            while True:
                kind, size = FRAME.unpack(receive(connection, FRAME.size))
                if kind == b"O" and size <= 64 * 1024:
                    view = memoryview(receive(connection, size))
                    while view:
                        view = view[os.write(1, view):]
                    continue
                if kind == b"E" and size == 1:
                    code = receive(connection, 1)[0]
                    print(f"\r\n{document['marker']}{code}", flush=True)
                    break
                raise WorkerError("invalid Agent frame")
        except (OSError, WorkerError) as error:
            print(f"worker Link: {error}", file=sys.stderr, flush=True)
            print(f"\r\n{document['marker']}125", flush=True)
    finally:
        stopping.set()
        connection.close()
        if saved is not None:
            termios.tcsetattr(0, termios.TCSADRAIN, saved)
    park()
