#!/usr/bin/env python3
"""Project opaque host Span streams as private Unix sockets."""
from __future__ import annotations

import argparse
import os
import re
import signal
import socket
import ssl
import stat
import threading
from pathlib import Path


READY_TOKEN = re.compile(r"^[a-f0-9]{32}$")
# HTTP tunnels pass through both the HTTP projection and a named Span bridge,
# so the two layers need independent worker budgets. Sharing one budget makes
# every routed OpenAI/GitHub tunnel count twice and can deadlock admission.
MAX_CONNECTIONS = 256
LISTEN_BACKLOG = 1024
MAX_SPANS = 16
from .http_projection import MAX_CONNECTIONS as MAX_HTTP_CONNECTIONS, HttpProjection, load_routes
from .config import MAX_CONFIG, NAME, read_json, valid_span_name
from .stream_relay import RESUME_GAP_SECONDS, duplex_stream, enable_tcp_keepalive, serve_connections, transport_clocks, transport_gap


class Bridge:
    def __init__(self, name: str, port: int, socket_dir: Path, host: str, context: ssl.SSLContext, slots=None, stopping=None, failed=None):
        self.name, self.port, self.host, self.context = name, port, host, context
        self.path = socket_dir / f"{name}.sock"
        self.stopping = stopping or threading.Event()
        self.failed = failed or threading.Event()
        self.slots = slots or threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.connections: set[socket.socket] = set()
        self.connections_lock = threading.Lock()
        self.resume_epoch = 0
        self.reported_failures: set[tuple[str, str]] = set()
        self.failure_lock = threading.Lock()
        self.last_observed_time = transport_clocks()
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
        self.server.listen(LISTEN_BACKLOG)
        self.server.settimeout(0.5)
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _report_failure(self, stage: str, error: Exception) -> None:
        key = stage, type(error).__name__
        with self.failure_lock:
            if key in self.reported_failures:
                return
            self.reported_failures.add(key)
        print(
            f"span-bridge: Span {self.name!r} failed during {stage} ({key[1]})",
            flush=True,
        )

    def _serve(self) -> None:
        try:
            serve_connections(
                self.server, self.stopping, self.slots,
                self._snapshot_epoch, self._retire_after_resume, self._start_worker,
            )
        except Exception as error:
            if not self.stopping.is_set():
                print(
                    f"span-bridge: Span {self.name!r} listener failed ({type(error).__name__})",
                    flush=True,
                )
                self.failed.set()
                self.stopping.set()

    def _snapshot_epoch(self) -> int:
        with self.connections_lock:
            return self.resume_epoch

    def _start_worker(self, client: socket.socket, epoch: int) -> None:
        thread = threading.Thread(
            target=self._connection, args=(client, epoch), daemon=True,
        )
        try:
            thread.start()
        except RuntimeError:
            client.close()
            self.slots.release()

    def _retire_after_resume(self, observed_time: tuple[float, float]) -> bool:
        gap = transport_gap(self.last_observed_time, observed_time)
        self.last_observed_time = observed_time
        if gap <= RESUME_GAP_SECONDS:
            return False
        retired=self._retire_connections()
        if retired:
            print(f"span-bridge: Span {self.name!r} retired pre-resume transport",flush=True)
        return True

    def _retire_connections(self) -> int:
        with self.connections_lock:
            self.resume_epoch += 1
            connections = tuple(self.connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        return len(connections)

    def _remember(self, connection: socket.socket, epoch: int, previous: socket.socket | None = None) -> bool:
        with self.connections_lock:
            if previous is not None:
                self.connections.discard(previous)
            current=epoch==self.resume_epoch
            if current:
                self.connections.add(connection)
        if not current:
            connection.close()
        return current

    def _connection(self, client: socket.socket, epoch: int) -> None:
        tracked: list[socket.socket] = [client]
        stage = "host connection"
        if not self._remember(client,epoch):
            self.slots.release()
            return
        try:
            with client:
                try:
                    with socket.create_connection((self.host, self.port), timeout=10) as raw:
                        tracked.append(raw)
                        if not self._remember(raw,epoch): return
                        enable_tcp_keepalive(raw)
                        stage = "TLS handshake"
                        with self.context.wrap_socket(
                            raw,server_hostname=self.host,do_handshake_on_connect=False,
                        ) as remote:
                            tracked.append(remote)
                            if not self._remember(remote,epoch,raw): return
                            remote.do_handshake()
                            client.settimeout(None)
                            remote.settimeout(None)
                            stage = "stream relay"
                            duplex_stream(client, remote, self.stopping)
                except (OSError, ssl.SSLError, RuntimeError) as error:
                    with self.connections_lock:
                        retired=epoch!=self.resume_epoch
                    if not self.stopping.is_set() and not retired:
                        self._report_failure(stage, error)
                    return
        finally:
            with self.connections_lock:
                for connection in tracked:
                    self.connections.discard(connection)
            self.slots.release()

    def stop(self) -> None:
        self.stopping.set()
        self.server.close()
        self._retire_connections()
        self.thread.join(timeout=2)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def load_config(path: Path) -> list[tuple[str, int]]:
    document = read_json(path, RuntimeError("Span bridge config is too large"))
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
        if not valid_span_name(name) or name in names:
            raise RuntimeError("invalid or duplicate Span bridge name")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise RuntimeError("invalid Span bridge port")
        names.add(name)
        result.append((name, port))
    return result


def worker_budgets() -> tuple[threading.BoundedSemaphore, threading.BoundedSemaphore]:
    """Return independent budgets for Span and HTTP relay worker layers."""
    return (
        threading.BoundedSemaphore(MAX_CONNECTIONS),
        threading.BoundedSemaphore(MAX_HTTP_CONNECTIONS),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--socket-dir", required=True, type=Path)
    parser.add_argument("--host", default="host.docker.internal")
    parser.add_argument("--tls-ca", required=True)
    parser.add_argument("--tls-cert", required=True)
    parser.add_argument("--tls-key", required=True)
    parser.add_argument("--ready-token-file", required=True, type=Path)
    parser.add_argument("--http-config", required=True, type=Path)
    parser.add_argument("--http-port", type=int, default=3128)
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
    stopping = threading.Event()
    failed = threading.Event()
    span_slots, http_slots = worker_budgets()
    entries = load_config(args.config)
    bridges = [Bridge(name, port, args.socket_dir, args.host, context, span_slots, stopping, failed) for name, port in entries]
    http = HttpProjection(
        load_routes(args.http_config, {name for name, _port in entries}),
        args.socket_dir,
        args.http_port,
        http_slots,
        stopping,
        failed,
    )
    def stop(_signum=None, _frame=None):
        stopping.set()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, stop)
    try:
        for bridge in bridges:
            bridge.start()
        http.start()
        if not failed.is_set():
            ready_path.write_text(token, encoding="ascii")
            ready_path.chmod(0o444)
            stopping.wait()
    finally:
        ready_path.unlink(missing_ok=True)
        http.stop()
        for bridge in reversed(bridges):
            bridge.stop()
    return 1 if failed.is_set() else 0


if __name__ == "__main__":
    raise SystemExit(main())
