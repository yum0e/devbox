#!/usr/bin/env python3
"""Project opaque host Span streams as private Unix sockets."""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import ssl
import stat
import threading
from pathlib import Path


NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
READY_TOKEN = re.compile(r"^[a-f0-9]{32}$")
MAX_CONNECTIONS = 16
MAX_SPANS = 16


def copy_stream(source: socket.socket, destination: socket.socket) -> None:
    try:
        while chunk := source.recv(64 * 1024):
            destination.sendall(chunk)
    except OSError:
        pass
    try:
        destination.shutdown(socket.SHUT_WR)
    except OSError:
        pass


class Bridge:
    def __init__(self, name: str, port: int, socket_dir: Path, host: str, context: ssl.SSLContext, slots=None):
        self.name, self.port, self.host, self.context = name, port, host, context
        self.path = socket_dir / f"{name}.sock"
        self.stopping = threading.Event()
        self.slots = slots or threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.connections: set[socket.socket] = set()
        self.connections_lock = threading.Lock()
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(metadata.st_mode):
                raise RuntimeError(f"refusing to replace non-socket Span path: {self.path}")
            self.path.unlink()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        # Only the Island and this bridge mount the containing volume. The
        # root-owned directory prevents Island code from replacing endpoints;
        # the socket itself remains connectable by the unprivileged Island UID.
        os.chmod(self.path, 0o666)
        self.server.listen(64)
        self.server.settimeout(0.5)
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _serve(self) -> None:
        while not self.stopping.is_set():
            try:
                client, _address = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.stopping.is_set():
                    return
                raise
            if not self.slots.acquire(blocking=False):
                client.close()
                continue
            thread = threading.Thread(target=self._connection, args=(client,), daemon=True)
            try:
                thread.start()
            except RuntimeError:
                client.close()
                self.slots.release()

    def _connection(self, client: socket.socket) -> None:
        tracked: list[socket.socket] = [client]
        with self.connections_lock:
            self.connections.add(client)
        try:
            with client:
                try:
                    with socket.create_connection((self.host, self.port), timeout=10) as raw:
                        with self.context.wrap_socket(raw, server_hostname=self.host) as remote:
                            tracked.append(remote)
                            with self.connections_lock:
                                self.connections.add(remote)
                            client.settimeout(None)
                            remote.settimeout(None)
                            reverse = threading.Thread(target=copy_stream, args=(remote, client), daemon=True)
                            reverse.start()
                            copy_stream(client, remote)
                            reverse.join()
                except (OSError, ssl.SSLError, RuntimeError):
                    return
        finally:
            with self.connections_lock:
                for connection in tracked:
                    self.connections.discard(connection)
            self.slots.release()

    def stop(self) -> None:
        self.stopping.set()
        self.server.close()
        with self.connections_lock:
            connections = list(self.connections)
        for connection in connections:
            try:
                connection.close()
            except OSError:
                pass
        self.thread.join(timeout=2)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def load_config(path: Path) -> list[tuple[str, int]]:
    if path.stat().st_size > 64 * 1024:
        raise RuntimeError("Span bridge config is too large")
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, list):
        raise RuntimeError("Span bridge config must be a list")
    if len(document)>MAX_SPANS:
        raise RuntimeError(f"Span bridge supports at most {MAX_SPANS} Spans")
    result = []
    names = set()
    for item in document:
        if not isinstance(item, dict) or set(item) != {"name", "port"}:
            raise RuntimeError("invalid Span bridge entry")
        name, port = item["name"], item["port"]
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in names:
            raise RuntimeError("invalid or duplicate Span bridge name")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise RuntimeError("invalid Span bridge port")
        names.add(name)
        result.append((name, port))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--socket-dir", required=True, type=Path)
    parser.add_argument("--host", default="host.docker.internal")
    parser.add_argument("--tls-ca", required=True)
    parser.add_argument("--tls-cert", required=True)
    parser.add_argument("--tls-key", required=True)
    parser.add_argument("--ready-token-file", required=True, type=Path)
    args = parser.parse_args(argv)
    args.socket_dir.mkdir(parents=True, exist_ok=True)
    ready_path = args.socket_dir / ".ready"
    ready_path.unlink(missing_ok=True)
    token = args.ready_token_file.read_text(encoding="ascii").strip()
    if not READY_TOKEN.fullmatch(token):
        raise RuntimeError("invalid Span readiness token")
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=args.tls_ca)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.options |= ssl.OP_NO_COMPRESSION
    context.load_cert_chain(certfile=args.tls_cert, keyfile=args.tls_key)
    slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
    bridges = [Bridge(name, port, args.socket_dir, args.host, context, slots) for name, port in load_config(args.config)]
    stopping = threading.Event()
    def stop(_signum=None, _frame=None):
        stopping.set()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, stop)
    try:
        for bridge in bridges:
            bridge.start()
        ready_path.write_text(token, encoding="ascii")
        ready_path.chmod(0o444)
        stopping.wait()
    finally:
        ready_path.unlink(missing_ok=True)
        for bridge in reversed(bridges):
            bridge.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
