#!/usr/bin/env python3
"""Two backend-blind Worlds for the backend substitution proof."""
from __future__ import annotations

import base64
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path


HEADER = struct.Struct("!I")
MAX_MESSAGE = 2 * 1024 * 1024
MAX_ARGV = 64
MAX_ARGV_BYTES = 16 * 1024
MAX_STDIN = 64 * 1024
WORLD_NAME = re.compile(r"[a-z][a-z0-9-]{0,63}")


class WorldError(RuntimeError):
    pass


def receive(connection: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        block = connection.recv(size - len(result))
        if not block:
            raise WorldError("message ended early")
        result.extend(block)
    return bytes(result)


def output(stdout: bytes = b"", stderr: bytes = b"", exit_code: int = 0) -> dict:
    if len(stdout) + len(stderr) > MAX_MESSAGE:
        raise WorldError("output exceeds 2 MiB")
    return {
        "ok": True,
        "stdout_b64": base64.b64encode(stdout).decode("ascii"),
        "stderr_b64": base64.b64encode(stderr).decode("ascii"),
        "exit_code": exit_code if 0 <= exit_code <= 255 else 255,
    }


def invocation(request: object, command: str) -> tuple[list[str], bytes]:
    if not isinstance(request, dict) or set(request) != {"v", "command", "argv", "stdin_b64"}:
        raise WorldError("invalid invocation")
    if request["v"] != 1 or request["command"] != command:
        raise WorldError(f"command is not exported by this World: {command}")
    argv = request["argv"]
    if not isinstance(argv, list) or len(argv) > MAX_ARGV:
        raise WorldError("invalid argv")
    if not all(isinstance(item, str) and "\0" not in item for item in argv):
        raise WorldError("invalid argv")
    if sum(len(item.encode()) for item in argv) > MAX_ARGV_BYTES:
        raise WorldError("argv exceeds 16 KiB")
    encoded = request["stdin_b64"]
    if not isinstance(encoded, str):
        raise WorldError("invalid stdin")
    try:
        stdin = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as error:
        raise WorldError("invalid stdin") from error
    if len(stdin) > MAX_STDIN:
        raise WorldError("stdin exceeds 64 KiB")
    return argv, stdin


def agent_handler(workspace: Path):
    environment = {
        "HOME": "/tmp/agent-home",
        "LANG": "C.UTF-8",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }

    def handle(request: object) -> dict:
        argv, stdin = invocation(request, "agent-worker")
        if not argv:
            raise WorldError("agent-worker requires argv")
        try:
            completed = subprocess.run(
                argv,
                cwd=workspace,
                env=environment,
                input=stdin,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return output(stderr=f"agent-worker: {error}\n".encode(), exit_code=125)
        return output(completed.stdout, completed.stderr, completed.returncode)

    return handle


def herdr_handler(worker: Path, worker_sockets: Path):
    def handle(request: object) -> dict:
        argv, stdin = invocation(request, "herdr")
        if len(argv) < 3 or argv[0] != "spawn" or argv[2] != "--":
            raise WorldError("usage: herdr spawn NAME -- COMMAND [ARG...]")
        name, command = argv[1], argv[3:]
        if WORLD_NAME.fullmatch(name) is None or not command:
            raise WorldError("spawn requires a name and command")
        environment = {
            "DEVC2_COMMAND_SOCKET_DIR": os.fspath(worker_sockets),
            "HOME": "/tmp/herdr-home",
            "LANG": "C.UTF-8",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
        completed = subprocess.run(
            [os.fspath(worker), *command],
            env=environment,
            input=stdin,
            capture_output=True,
            timeout=15,
            check=False,
        )
        return output(
            f"{name}\t".encode() + completed.stdout,
            completed.stderr,
            completed.returncode,
        )

    return handle


def serve(path: Path, handle) -> int:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(os.fspath(path))
    path.chmod(0o600)
    server.listen(16)
    stopping = threading.Event()
    connections: set[socket.socket] = set()

    def stop(_signum, _frame):
        stopping.set()
        server.close()
        for connection in tuple(connections):
            connection.close()

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, stop)
    try:
        while not stopping.is_set():
            try:
                connection, _address = server.accept()
            except OSError:
                if stopping.is_set():
                    break
                raise
            connection.settimeout(0.2)
            connections.add(connection)
            try:
                with connection:
                    try:
                        size = HEADER.unpack(receive(connection, HEADER.size))[0]
                        if size > MAX_MESSAGE:
                            raise WorldError("request exceeds 2 MiB")
                        response = handle(json.loads(receive(connection, size)))
                    except (OSError, ValueError, WorldError, json.JSONDecodeError, struct.error) as error:
                        if stopping.is_set():
                            break
                        response = {"ok": False, "error": str(error)[:4096]}
                    payload = json.dumps(response, separators=(",", ":")).encode()
                    connection.sendall(HEADER.pack(len(payload)) + payload)
            finally:
                connections.discard(connection)
    finally:
        server.close()
        path.unlink(missing_ok=True)
    return 0


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "agent":
        return serve(Path(sys.argv[2]), agent_handler(Path(sys.argv[3]).resolve(strict=True)))
    if len(sys.argv) == 5 and sys.argv[1] == "herdr":
        return serve(Path(sys.argv[2]), herdr_handler(Path(sys.argv[3]), Path(sys.argv[4])))
    print("usage: world.py agent SOCKET WORKSPACE | world.py herdr SOCKET WORKER WORKER_SOCKETS", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
