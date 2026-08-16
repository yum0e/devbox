#!/usr/bin/env python3
"""Run one unchanged World graph through two one-method backends."""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
WORLD = ROOT / "world.py"
PROJECTION = ROOT.parents[1] / "launcher" / "command_projection.py"
HEADER = struct.Struct("!I")
MAX_RESPONSE = 2 * 1024 * 1024
sys.path.insert(0, os.fspath(ROOT.parents[1]))
from credential_proxy.stream_relay import duplex_stream


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
        process.wait(timeout=2)


def direct(argv: tuple[str, ...], environment: dict[str, str]):
    endpoint = Path(argv[3])

    def launch():
        process = subprocess.Popen(
            argv, env=environment, stdin=subprocess.DEVNULL, start_new_session=True,
        )

        def stop():
            stop_process(process)
            endpoint.unlink(missing_ok=True)
        return stop
    return launch


class Relay:
    def __init__(self, public: Path, private: Path):
        self.public, self.private = public, private
        self.stopping = threading.Event()
        self.connections: set[socket.socket] = set()
        self.handlers: set[threading.Thread] = set()
        self.lock = threading.Lock()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.public.unlink(missing_ok=True)
        self.server.bind(os.fspath(self.public))
        self.public.chmod(0o600)
        self.server.listen(16)
        self.server.settimeout(0.1)
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def serve(self) -> None:
        while not self.stopping.is_set():
            try:
                client, _address = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            thread = threading.Thread(target=self.relay, args=(client,))
            with self.lock:
                if self.stopping.is_set():
                    client.close()
                    continue
                self.connections.add(client)
                self.handlers.add(thread)
            try:
                thread.start()
            except RuntimeError:
                with self.lock:
                    self.connections.discard(client)
                    self.handlers.discard(thread)
                client.close()

    def relay(self, client: socket.socket) -> None:
        current = threading.current_thread()
        remote = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with client, remote:
            try:
                try:
                    remote.connect(os.fspath(self.private))
                except OSError:
                    return
                with self.lock:
                    self.connections.add(remote)
                try:
                    duplex_stream(client, remote, self.stopping)
                except OSError:
                    if not self.stopping.is_set():
                        raise
            finally:
                with self.lock:
                    self.connections.discard(client)
                    self.connections.discard(remote)
                    self.handlers.discard(current)

    def stop(self) -> None:
        self.stopping.set()
        self.server.close()
        self.public.unlink(missing_ok=True)
        self.thread.join(timeout=2)
        with self.lock:
            connections = tuple(self.connections)
        for connection in connections:
            try:
                connection.close()
            except OSError:
                pass
        with self.lock:
            handlers = tuple(self.handlers)
        for handler in handlers:
            handler.join(timeout=2)
        if self.thread.is_alive() or any(handler.is_alive() for handler in handlers):
            raise RuntimeError("Link relay did not stop")


def packaged_relayed(argv: tuple[str, ...], environment: dict[str, str], runtime: Path):
    source = Path(argv[1])
    public = Path(argv[3])

    def launch():
        runtime.mkdir(0o700)
        staged_world, private = runtime / "world.py", runtime / "world.sock"
        process = None
        try:
            shutil.copyfile(source, staged_world)
            if staged_world.read_bytes() != source.read_bytes():
                raise RuntimeError("staged World differs from its source artifact")
            command = (*argv[:1], os.fspath(staged_world), *argv[2:3], os.fspath(private), *argv[4:])
            process = subprocess.Popen(
                command, env=environment, stdin=subprocess.DEVNULL, start_new_session=True,
            )
            relay = Relay(public, private)
            relay.start()
        except Exception:
            if process is not None:
                stop_process(process)
            private.unlink(missing_ok=True)
            shutil.rmtree(runtime)
            raise

        def stop():
            relay.stop()
            stop_process(process)
            private.unlink(missing_ok=True)
            shutil.rmtree(runtime, ignore_errors=True)
        return stop
    return launch


def wait_link(path: Path) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            response = request(path, {}, timeout=max(0.01, deadline - time.monotonic()))
            if response.get("ok") is False:
                return
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
            pass
        time.sleep(0.01)
    raise RuntimeError(f"World Link did not become responsive: {path}")


def request(path: Path, document: dict, timeout: float = 3) -> dict:
    payload = json.dumps(document, separators=(",", ":")).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(os.fspath(path))
        connection.sendall(HEADER.pack(len(payload)) + payload)
        size = HEADER.unpack(receive(connection, HEADER.size))[0]
        if size > MAX_RESPONSE:
            raise RuntimeError("response exceeds 2 MiB")
        response = receive(connection, size)
    return json.loads(response)


def receive(connection: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        block = connection.recv(size - len(result))
        if not block:
            raise RuntimeError("response ended early")
        result.extend(block)
    return bytes(result)


def invoke(link: Path, sockets: Path, *argv: str) -> subprocess.CompletedProcess:
    environment = {
        "DEVC2_COMMAND_SOCKET_DIR": os.fspath(sockets),
        "HOME": "/tmp/caller-home",
        "LANG": "C.UTF-8",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }
    return subprocess.run([os.fspath(link), *argv], env=environment, text=True, capture_output=True, timeout=10)


def world_environment() -> dict[str, str]:
    return {
        "HOME": "/tmp/world-home",
        "LANG": "C.UTF-8",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }


def exercise(name: str, root: Path, launch_agent, launch_herdr) -> None:
    worker_sockets, herdr_sockets = root / "worker-sockets", root / "herdr-sockets"
    private_bin, caller_bin = root / "herdr-bin", root / "caller-bin"
    worker_link, herdr_link = private_bin / "agent-worker", caller_bin / "herdr"
    worker_socket, herdr_socket = worker_sockets / "agent-worker.sock", herdr_sockets / "herdr.sock"
    stop_agent = launch_agent()
    stop_herdr = None
    try:
        wait_link(worker_socket)
        stop_herdr = launch_herdr()
        wait_link(herdr_socket)
        completed = invoke(herdr_link, herdr_sockets, "spawn", "proof", "--", "/bin/sh", "-c", "printf backend-neutral")
        if completed.returncode or completed.stdout != "proof\tbackend-neutral" or completed.stderr:
            raise RuntimeError(f"{name} graph failed: {completed!r}")
        stalled = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stalled.connect(os.fspath(worker_socket))
        stalled.sendall(b"\0\0")
        stop_agent()
        stalled.close()
        failed = invoke(herdr_link, herdr_sockets, "spawn", "dead", "--", "/bin/true")
        if failed.returncode == 0:
            raise RuntimeError(f"{name} retained a usable worker route after Agent stopped")
    finally:
        if stop_herdr is not None:
            stop_herdr()
        stop_agent()
    if worker_socket.exists() or herdr_socket.exists():
        raise RuntimeError(f"{name} Worlds left a Link endpoint behind")
    print(f"{name}: same logical graph + same result + clean lifecycle")


def immediate_stop(launch, *endpoints: Path) -> None:
    stop = launch()
    stop()
    stop()
    if any(endpoint.exists() for endpoint in endpoints):
        raise RuntimeError("stop before readiness left a Link endpoint behind")


def layout(root: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    worker_sockets, herdr_sockets = root / "worker-sockets", root / "herdr-sockets"
    worker_sockets.mkdir(0o700); herdr_sockets.mkdir(0o700)
    private_bin, caller_bin = root / "herdr-bin", root / "caller-bin"
    private_bin.mkdir(0o700); caller_bin.mkdir(0o700)
    worker_link, herdr_link = private_bin / "agent-worker", caller_bin / "herdr"
    worker_link.symlink_to(PROJECTION); herdr_link.symlink_to(PROJECTION)
    worker_socket, herdr_socket = worker_sockets / "agent-worker.sock", herdr_sockets / "herdr.sock"
    workspace = root / "workspace"; workspace.mkdir()
    agent = (sys.executable, os.fspath(WORLD), "agent", os.fspath(worker_socket), os.fspath(workspace))
    herdr = (
        sys.executable, os.fspath(WORLD), "herdr", os.fspath(herdr_socket),
        os.fspath(worker_link), os.fspath(worker_sockets),
    )
    return agent, herdr


def prove_direct() -> None:
    with tempfile.TemporaryDirectory(prefix="world-backend-direct-") as raw:
        root = Path(raw)
        agent, herdr = layout(root)
        launch_agent = direct(agent, world_environment())
        launch_herdr = direct(herdr, world_environment())
        immediate_stop(launch_agent, root / "worker-sockets" / "agent-worker.sock")
        exercise("direct", root, launch_agent, launch_herdr)


def prove_packaged_relayed() -> None:
    with tempfile.TemporaryDirectory(prefix="world-backend-packaged-relayed-") as raw:
        root = Path(raw)
        agent, herdr = layout(root)
        missing = (*agent[:1], os.fspath(root / "missing-world"), *agent[2:])
        failed_runtime = root / "failed-runtime"
        try:
            packaged_relayed(missing, world_environment(), failed_runtime)()
        except FileNotFoundError:
            pass
        else:
            raise RuntimeError("packaged backend accepted a missing World artifact")
        if failed_runtime.exists():
            raise RuntimeError("failed launch retained its backend runtime")
        launch_agent = packaged_relayed(agent, world_environment(), root / "agent-runtime")
        launch_herdr = packaged_relayed(herdr, world_environment(), root / "herdr-runtime")
        immediate_stop(
            launch_agent,
            root / "worker-sockets" / "agent-worker.sock",
            root / "agent-runtime" / "world.sock",
            root / "agent-runtime",
        )
        exercise(
            "packaged-relayed", root,
            launch_agent, launch_herdr,
        )


def main() -> int:
    if len(sys.argv) != 1:
        return 2
    prove_direct()
    prove_packaged_relayed()
    print("proof: backend interface = launch() -> stop(); interaction = Links")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
