"""Docker backend Agent exporting one interactive Island worker Link."""
from __future__ import annotations

import fcntl
import hashlib
import inspect
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
import time
from pathlib import Path

import island_supervisor


HEADER = struct.Struct("!I")
MAX_REQUEST = 64 * 1024
MAX_ARGV_BYTES = 16 * 1024
MAX_WORKERS = 16
FRAME = struct.Struct("!cI")
STATUS = struct.Struct("!c32sB")
TOKEN = re.compile(r"^[0-9a-f]{32}$")
SUPERVISOR_SOURCE = inspect.getsource(island_supervisor)


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


def receive_stream(stream, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        block = stream.read(size - len(result))
        if not block:
            raise AgentError("worker supervisor stream ended early")
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
    if not isinstance(document, dict) or set(document) != {"argv", "token", "rows", "columns"}:
        raise AgentError("invalid worker request")
    argv = document["argv"]
    if (not isinstance(argv, list) or not argv or len(argv) > 64
            or not all(isinstance(item, str) and "\0" not in item for item in argv)
            or sum(len(item.encode()) for item in argv) > MAX_ARGV_BYTES):
        raise AgentError("invalid worker argv")
    if not isinstance(document["token"], str) or TOKEN.fullmatch(document["token"]) is None:
        raise AgentError("invalid worker token")
    if (not isinstance(document["rows"], int) or isinstance(document["rows"], bool)
            or not 1 <= document["rows"] <= 1000
            or not isinstance(document["columns"], int) or isinstance(document["columns"], bool)
            or not 1 <= document["columns"] <= 1000):
        raise AgentError("invalid worker terminal size")
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
    supervised = True

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

    def command(self, argv: list[str], rows: int, columns: int) -> list[str]:
        return [
            self.docker, "exec", "--interactive", "--user", "1000:1000",
            "--workdir", "/workspace", self.container(),
            "/usr/bin/python3", "-c", SUPERVISOR_SOURCE,
            str(rows), str(columns), "--", *argv,
        ]


class Agent:
    def __init__(self, listener: socket.socket, island: DockerIsland, status: socket.socket | None = None):
        self.listener, self.island, self.status = listener, island, status
        self.stopping = threading.Event()
        self.slots = threading.BoundedSemaphore(MAX_WORKERS)
        self.connections: set[socket.socket] = set()
        self.processes: set[subprocess.Popen] = set()
        self.threads: set[threading.Thread] = set()
        self.lock = threading.Lock()

    def publish(self, kind: bytes, token: str, code: int = 0) -> None:
        if self.status is not None:
            self.status.send(STATUS.pack(kind, token.encode("ascii"), code))

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
        document = None
        try:
            with connection:
                try:
                    document = request(connection)
                    self.run_worker(connection, document)
                except (AgentError, OSError) as error:
                    if document is not None:
                        try:
                            self.publish(b"E", document["token"], 125)
                        except OSError:
                            pass
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
        if getattr(self.island, "supervised", False):
            self.run_supervised_worker(connection, document)
            return
        command = self.island.command(document["argv"], document["rows"], document["columns"])
        if self.stopping.is_set():
            raise AgentError("Agent is stopping")
        master, slave = pty.openpty()
        environment = os.environ.copy()
        environment["DOCKER_CLI_HINTS"] = "false"
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack(
                "HHHH", document["rows"], document["columns"], 0, 0,
            ))
            environment["TERM"] = "xterm-256color"
            environment["COLORTERM"] = "truecolor"
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
        try:
            self.publish(b"R", document["token"])
            send_frame(connection, b"S", b"")
            input_thread.start()
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
            self.publish(b"E", document["token"], code)
            send_frame(connection, b"E", bytes([code]))
        finally:
            self.stop_process(process)
            os.close(master)
            if input_thread.ident is not None:
                input_thread.join(timeout=1)
            with self.lock:
                self.processes.discard(process)

    def run_supervised_worker(self, connection: socket.socket, document: dict) -> None:
        command = self.island.command(document["argv"], document["rows"], document["columns"])
        if self.stopping.is_set():
            raise AgentError("Agent is stopping")
        environment = os.environ.copy()
        environment["DOCKER_CLI_HINTS"] = "false"
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=environment, start_new_session=True,
        )
        if process.stdin is None or process.stdout is None:
            self.stop_process(process)
            raise AgentError("worker supervisor streams are unavailable")
        with self.lock:
            self.processes.add(process)
        disconnected = threading.Event()
        write_lock = threading.Lock()

        def control(kind: bytes, payload: bytes = b"") -> None:
            with write_lock:
                process.stdin.write(FRAME.pack(kind, len(payload)) + payload)
                process.stdin.flush()

        def receive_frame(timeout: float) -> tuple[bytes, bytes] | None:
            readable, _writable, _errors = select.select([process.stdout], [], [], timeout)
            if not readable:
                return None
            kind, size = FRAME.unpack(receive_stream(process.stdout, FRAME.size))
            if size > 64 * 1024:
                raise AgentError("worker supervisor frame is too large")
            return kind, receive_stream(process.stdout, size)

        def input_loop() -> None:
            next_heartbeat = 0.0
            while not self.stopping.is_set() and process.poll() is None:
                try:
                    now = time.monotonic()
                    if now >= next_heartbeat:
                        control(b"H")
                        next_heartbeat = now + 0.5
                    readable, _writable, _errors = select.select([connection], [], [], 0.1)
                    if not readable:
                        continue
                    block = connection.recv(64 * 1024)
                    if not block:
                        break
                    control(b"I", block)
                except (OSError, ValueError):
                    break
            disconnected.set()
            try:
                control(b"C")
            except (OSError, ValueError):
                pass

        input_thread = threading.Thread(target=input_loop)
        try:
            ready = receive_frame(5)
            if ready != (b"R", b""):
                raise AgentError("worker supervisor did not become ready")
            self.publish(b"R", document["token"])
            send_frame(connection, b"S", b"")
            input_thread.start()
            exit_code = None
            while process.poll() is None and exit_code is None:
                if self.stopping.is_set():
                    disconnected.set()
                frame = receive_frame(0.2)
                if frame is None:
                    continue
                kind, payload = frame
                if kind == b"O":
                    try:
                        send_frame(connection, b"O", payload)
                    except OSError:
                        disconnected.set()
                elif kind == b"E" and len(payload) == 1:
                    exit_code = payload[0]
                else:
                    raise AgentError("worker supervisor returned an invalid frame")
            if exit_code is None:
                exit_code = process.returncode if process.returncode is not None else 125
            exit_code = exit_code if 0 <= exit_code <= 255 else 255
            self.publish(b"E", document["token"], exit_code)
            if not disconnected.is_set():
                send_frame(connection, b"E", bytes([exit_code]))
        finally:
            disconnected.set()
            try:
                control(b"C")
            except (OSError, ValueError):
                pass
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.stop_process(process)
            if input_thread.ident is not None:
                input_thread.join(timeout=1)
            process.stdin.close()
            process.stdout.close()
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
        status_descriptor = int(os.environ["DEVC2_HERDR_STATUS_FD"])
    except (KeyError, ValueError) as error:
        raise AgentError("missing Agent listener or status channel") from error
    agent = Agent(
        socket.socket(fileno=descriptor), DockerIsland(),
        socket.socket(fileno=status_descriptor),
    )
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, lambda _signum, _frame: agent.request_stop())
    try:
        return agent.serve()
    finally:
        agent.stop()
