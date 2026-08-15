#!/usr/bin/env python3
"""Project one registered command affordance through its private Link."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import socket
import struct
import sys


NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
HEADER = struct.Struct("!I")
MAX_ARGV = 64
MAX_ARGV_BYTES = 16 * 1024
MAX_STDIN = 64 * 1024
MAX_REQUEST = 128 * 1024
MAX_RESPONSE = 2 * 1024 * 1024


def receive(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        block = connection.recv(size - len(chunks))
        if not block:
            raise RuntimeError("World response ended early")
        chunks.extend(block)
    return bytes(chunks)


def main() -> int:
    command = Path(sys.argv[0]).name
    if not NAME.fullmatch(command):
        raise RuntimeError("invalid projected command name")
    argv = sys.argv[1:]
    if len(argv) > MAX_ARGV or sum(len(item.encode()) for item in argv) > MAX_ARGV_BYTES:
        raise RuntimeError("command arguments exceed projection limits")
    stdin = b"" if sys.stdin.isatty() else sys.stdin.buffer.read(MAX_STDIN + 1)
    if len(stdin) > MAX_STDIN:
        raise RuntimeError("command input exceeds 64 KiB")
    request = json.dumps({
        "v": 1,
        "command": command,
        "argv": argv,
        "stdin_b64": base64.b64encode(stdin).decode("ascii"),
    }, separators=(",", ":")).encode()
    if len(request) > MAX_REQUEST:
        raise RuntimeError("command request exceeds 128 KiB")
    socket_dir = Path(os.environ.get("DEVC2_COMMAND_SOCKET_DIR", "/run/devc2/spans"))
    target = socket_dir / f"{command}.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10)
        connection.connect(os.fspath(target))
        connection.sendall(HEADER.pack(len(request)) + request)
        connection.settimeout(None)
        size = HEADER.unpack(receive(connection, HEADER.size))[0]
        if size > MAX_RESPONSE:
            raise RuntimeError("World response exceeds 2 MiB")
        response = json.loads(receive(connection, size))
    if not isinstance(response, dict) or response.get("ok") is not True:
        error = response.get("error") if isinstance(response, dict) else None
        raise RuntimeError(error if isinstance(error, str) else "invalid World response")
    if set(response) != {"ok", "stdout_b64", "stderr_b64", "exit_code"}:
        raise RuntimeError("invalid World response")
    try:
        stdout = base64.b64decode(response["stdout_b64"], validate=True)
        stderr = base64.b64decode(response["stderr_b64"], validate=True)
    except (TypeError, ValueError) as error:
        raise RuntimeError("invalid World output") from error
    exit_code = response["exit_code"]
    if not isinstance(exit_code, int) or isinstance(exit_code, bool) or not 0 <= exit_code <= 255:
        raise RuntimeError("invalid World exit status")
    sys.stdout.buffer.write(stdout)
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(stderr)
    sys.stderr.buffer.flush()
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, UnicodeError, json.JSONDecodeError, struct.error) as error:
        print(f"{Path(sys.argv[0]).name}: {error}", file=sys.stderr)
        raise SystemExit(125)
