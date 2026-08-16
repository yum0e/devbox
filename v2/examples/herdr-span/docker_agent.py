"""Docker backend Agent exporting one interactive Island worker Link."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pty
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
from pathlib import Path


HEADER = struct.Struct("!I")
MAX_REQUEST = 64 * 1024
MAX_ARGV_BYTES = 16 * 1024
MAX_WORKERS = 16
FRAME = struct.Struct("!cI")


class AgentError(RuntimeError):
    pass


def receive(connection: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        block = connection.recv(size - len(result))
        if not block:
            raise AgentError("worker request ended early")
        result.extend(block)
    return bytes(result)


def request(connection: socket.socket) -> dict:
    size = HEADER.unpack(receive(connection, HEADER.size))[0]
    if not size or size > MAX_REQUEST:
        raise AgentError("worker request exceeds 64 KiB")
    try:
        document = json.loads(receive(connection, size))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AgentError("invalid worker request") from error
    if not isinstance(document, dict) or set(document) != {"argv"}:
        raise AgentError("invalid worker request")
    argv = document["argv"]
    if (not isinstance(argv, list) or not argv or len(argv) > 64
            or not all(isinstance(item, str) and "\0" not in item for item in argv)
            or sum(len(item.encode()) for item in argv) > MAX_ARGV_BYTES):
        raise AgentError("invalid worker argv")
    return document


def send_frame(connection: socket.socket, kind: bytes, payload: bytes) -> None:
    connection.sendall(FRAME.pack(kind, len(payload)) + payload)


def write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        view = view[os.write(descriptor, view):]


def safe_executable(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise AgentError(f"required host executable is unavailable: {name}")
    path = Path(found).resolve(strict=True)
    metadata = path.stat()
    if (not path.is_file() or metadata.st_uid not in (0, os.getuid())
            or metadata.st_mode & 0o022 or not os.access(path, os.X_OK)):
        raise AgentError(f"unsafe host executable: {name}")
    return os.fspath(path)


class DockerIsland:
    def __init__(self):
        try:
            self.workspace = Path(os.environ["DEVC2_WORKSPACE"]).resolve(strict=True)
        except (KeyError, OSError) as error:
            raise AgentError("missing Island backend identity") from error
        self.docker = safe_executable("docker")

    @property
    def workspace_hash(self) -> str:
        return hashlib.sha256(os.fsencode(self.workspace)).hexdigest()[:16]

    def container(self) -> str:
        filters = (
            "label=dev.devbox.generation=2",
            f"label=dev.devbox.workspace-hash={self.workspace_hash}",
            f"label=com.docker.compose.project=devc2-{self.workspace_hash}",
            "label=com.docker.compose.service=devbox",
            "label=com.docker.compose.oneoff=True",
        )
        command = [self.docker, "ps"]
        for item in filters:
            command.extend(("--filter", item))
        command.extend(("--format", "{{.ID}}"))
        try:
            listed = subprocess.run(command, text=True, capture_output=True, timeout=10, check=False)
        except subprocess.SubprocessError as error:
            raise AgentError("Docker did not identify the Island") from error
        identifiers = listed.stdout.split() if listed.returncode == 0 else []
        if len(identifiers) != 1:
            raise AgentError("could not identify exactly one running Island container")
        try:
            inspected = subprocess.run(
                [self.docker, "inspect", identifiers[0]],
                text=True, capture_output=True, timeout=10, check=False,
            )
            document = json.loads(inspected.stdout)[0]
            labels, mounts = document["Config"]["Labels"], document["Mounts"]
            identifier, running = document["Id"], document["State"]["Running"]
        except (IndexError, KeyError, TypeError, json.JSONDecodeError, subprocess.SubprocessError) as error:
            raise AgentError("Docker returned invalid Island metadata") from error
        expected = {
            "dev.devbox.generation": "2",
            "dev.devbox.workspace-hash": self.workspace_hash,
            "com.docker.compose.project": f"devc2-{self.workspace_hash}",
            "com.docker.compose.service": "devbox",
            "com.docker.compose.oneoff": "True",
        }
        sources = {os.fspath(self.workspace)}
        if sys.platform == "darwin":
            sources.add("/host_mnt" + os.fspath(self.workspace))
        workspace_mounts = [
            item for item in mounts
            if item.get("Destination") == "/workspace"
            and item.get("Type") == "bind"
            and item.get("RW") is True
            and item.get("Source") in sources
        ]
        if (inspected.returncode or not running or not isinstance(labels, dict)
                or any(labels.get(key) != value for key, value in expected.items())
                or len(workspace_mounts) != 1
                or not isinstance(identifier, str)
                or re.fullmatch(r"[0-9a-f]{64}", identifier) is None):
            raise AgentError("Docker container is not the granted Island")
        return identifier

    def command(self, argv: list[str]) -> list[str]:
        return [
            self.docker, "exec", "--interactive", "--tty", "--user", "1000:1000",
            "--workdir", "/workspace", self.container(), *argv,
        ]


class Agent:
    def __init__(self, listener: socket.socket, island: DockerIsland):
        self.listener, self.island = listener, island
        self.stopping = threading.Event()
        self.slots = threading.BoundedSemaphore(MAX_WORKERS)
        self.connections: set[socket.socket] = set()
        self.processes: set[subprocess.Popen] = set()
        self.threads: set[threading.Thread] = set()
        self.lock = threading.Lock()

    def serve(self) -> int:
        self.listener.settimeout(0.2)
        while not self.stopping.is_set():
            try:
                connection, _address = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.stopping.is_set():
                    break
                raise
            if not self.slots.acquire(blocking=False):
                connection.close()
                continue
            thread = threading.Thread(target=self.handle, args=(connection,))
            with self.lock:
                self.connections.add(connection)
                self.threads.add(thread)
            thread.start()
        return 0

    def handle(self, connection: socket.socket) -> None:
        current = threading.current_thread()
        try:
            with connection:
                try:
                    self.run_worker(connection, request(connection))
                except (AgentError, OSError) as error:
                    try:
                        send_frame(connection, b"O", f"\r\nAgent worker failed: {error}\r\n".encode())
                        send_frame(connection, b"E", bytes([125]))
                    except OSError:
                        pass
        finally:
            with self.lock:
                self.connections.discard(connection)
                self.threads.discard(current)
            self.slots.release()

    def run_worker(self, connection: socket.socket, document: dict) -> None:
        command = self.island.command(document["argv"])
        if self.stopping.is_set():
            raise AgentError("Agent is stopping")
        master, slave = pty.openpty()
        environment = os.environ.copy()
        environment["DOCKER_CLI_HINTS"] = "false"
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 120, 0, 0))
            process = subprocess.Popen(
                command, stdin=slave, stdout=slave, stderr=slave,
                env=environment, start_new_session=True,
            )
        except Exception:
            os.close(master)
            os.close(slave)
            raise
        os.close(slave)
        with self.lock:
            self.processes.add(process)
        disconnected = threading.Event()

        def input_loop() -> None:
            while not self.stopping.is_set() and process.poll() is None:
                try:
                    readable, _writable, _errors = select.select([connection], [], [], 0.2)
                    if not readable:
                        continue
                    block = connection.recv(64 * 1024)
                    if not block:
                        disconnected.set()
                        return
                    write_all(master, block)
                except OSError:
                    disconnected.set()
                    return

        input_thread = threading.Thread(target=input_loop)
        input_thread.start()
        try:
            while process.poll() is None and not disconnected.is_set() and not self.stopping.is_set():
                readable, _writable, _errors = select.select([master], [], [], 0.1)
                if not readable:
                    continue
                try:
                    block = os.read(master, 64 * 1024)
                except OSError:
                    break
                if block:
                    send_frame(connection, b"O", block)
            self.stop_process(process)
            code = process.wait(timeout=2)
            # Drain output already buffered in the PTY before publishing exit.
            while select.select([master], [], [], 0)[0]:
                try:
                    block = os.read(master, 64 * 1024)
                except OSError:
                    break
                if not block:
                    break
                send_frame(connection, b"O", block)
            code = code if 0 <= code <= 255 else 255
            send_frame(connection, b"E", bytes([code]))
        finally:
            self.stop_process(process)
            os.close(master)
            input_thread.join(timeout=1)
            with self.lock:
                self.processes.discard(process)

    @staticmethod
    def stop_process(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    def stop(self) -> None:
        self.stopping.set()
        self.listener.close()
        with self.lock:
            connections = tuple(self.connections)
            processes = tuple(self.processes)
            threads = tuple(self.threads)
        for connection in connections:
            connection.close()
        for process in processes:
            self.stop_process(process)
        for thread in threads:
            thread.join(timeout=3)

    def request_stop(self) -> None:
        self.stopping.set()


def main() -> int:
    try:
        descriptor = int(os.environ["DEVC2_HERDR_AGENT_FD"])
    except (KeyError, ValueError) as error:
        raise AgentError("missing Agent listener") from error
    agent = Agent(socket.socket(fileno=descriptor), DockerIsland())
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, lambda _signum, _frame: agent.request_stop())
    try:
        return agent.serve()
    finally:
        agent.stop()
