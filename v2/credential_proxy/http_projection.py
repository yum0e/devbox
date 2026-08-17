"""One bounded HTTP CONNECT projection for launch-lifetime World attachments."""
from __future__ import annotations

import json
from pathlib import Path
import re
import socket
import threading

from .stream_relay import duplex_stream, enable_tcp_keepalive, suspend_clock


MAX_CONFIG = 64 * 1024
MAX_HEADERS = 16 * 1024
MAX_CONNECTIONS = 16
RESUME_GAP_SECONDS = 15
NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class ProjectionError(RuntimeError):
    pass


def load_routes(path: Path, allowed_names: set[str]) -> dict[str, str]:
    if path.stat().st_size > MAX_CONFIG:
        raise ProjectionError("HTTP attachment config is too large")
    document = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(document, dict) or set(document) != {"v", "routes"}
            or type(document["v"]) is not int or document["v"] != 1):
        raise ProjectionError("invalid HTTP attachment config")
    routes = document["routes"]
    if not isinstance(routes, dict) or len(routes) > 256:
        raise ProjectionError("invalid HTTP attachment routes")
    result = {}
    for authority, name in routes.items():
        if (not isinstance(authority, str) or not authority.endswith(":443")
                or not valid_dns(authority[:-4]) or authority != authority.lower()
                or not isinstance(name, str) or not NAME.fullmatch(name)
                or name not in allowed_names):
            raise ProjectionError("invalid HTTP attachment route")
        result[authority] = name
    return result


def valid_dns(host: str) -> bool:
    return bool(host) and len(host) <= 253 and all(
        DNS_LABEL.fullmatch(label) for label in host.split(".")
    )


def connect_target(first_line: bytes) -> tuple[str, str]:
    fields = first_line.split(b" ")
    if len(fields) != 3 or fields[0] != b"CONNECT" or fields[2] not in (b"HTTP/1.0", b"HTTP/1.1"):
        raise ProjectionError("unsupported proxy request")
    try:
        authority = fields[1].decode("ascii")
    except UnicodeError as error:
        raise ProjectionError("invalid proxy target") from error
    if (authority != authority.lower() or not authority.endswith(":443")
            or not valid_dns(authority[:-4])):
        raise ProjectionError("invalid proxy target")
    return authority[:-4], authority


class HttpProjection:
    def __init__(self, routes: dict[str, str], socket_dir: Path, port: int = 3128, slots=None, stopping=None, failed=None):
        self.routes = routes
        self.socket_dir = socket_dir
        self.stopping = stopping or threading.Event()
        self.failed = failed or threading.Event()
        self.slots = slots or threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.lock = threading.Lock()
        self.connections: set[socket.socket] = set()
        self.threads: set[threading.Thread] = set()
        self.last_observed_time = suspend_clock()
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("0.0.0.0", port))
        self.server.listen(MAX_CONNECTIONS)
        self.server.settimeout(0.5)
        self.thread = threading.Thread(target=self._serve, name="http-world-projection", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _remember(self, *connections: socket.socket) -> None:
        with self.lock:
            self.connections.update(connections)

    def _forget(self, *connections: socket.socket) -> None:
        with self.lock:
            for connection in connections:
                self.connections.discard(connection)

    def _retire_connections(self) -> None:
        with self.lock:
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

    def _retire_after_resume(self, observed_time: float) -> bool:
        gap = observed_time - self.last_observed_time
        self.last_observed_time = observed_time
        if gap <= RESUME_GAP_SECONDS:
            return False
        self._retire_connections()
        return True

    def _serve(self) -> None:
        try:
            while not self.stopping.is_set():
                client = None
                try:
                    client, _address = self.server.accept()
                except socket.timeout:
                    pass
                self._retire_after_resume(suspend_clock())
                if client is None:
                    continue
                if not self.slots.acquire(blocking=False):
                    client.close()
                    continue
                thread = threading.Thread(target=self._handle, args=(client,), daemon=True)
                with self.lock:
                    self.threads.add(thread)
                try:
                    thread.start()
                except RuntimeError:
                    with self.lock:
                        self.threads.discard(thread)
                    client.close()
                    self.slots.release()
        except Exception as error:
            if not self.stopping.is_set():
                print(
                    f"span-bridge: HTTP listener failed ({type(error).__name__})",
                    flush=True,
                )
                self.failed.set()
                self.stopping.set()

    def _handle(self, client: socket.socket) -> None:
        upstream = None
        self._remember(client)
        try:
            client.settimeout(10)
            initial = bytearray()
            marker = -1
            while marker < 0:
                if len(initial) >= MAX_HEADERS:
                    raise ProjectionError("proxy headers exceed 16 KiB")
                block = client.recv(min(4096, MAX_HEADERS - len(initial)))
                if not block:
                    raise ProjectionError("proxy request ended early")
                initial.extend(block)
                marker = initial.find(b"\r\n\r\n")
            header_end = marker + 4
            first_line = bytes(initial[:marker]).split(b"\r\n", 1)[0]
            host, authority = connect_target(first_line)
            route = self.routes.get(authority)
            if route is not None:
                upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                upstream.settimeout(30)
                upstream.connect(str(self.socket_dir / f"{route}.sock"))
                upstream.sendall(b"T" + initial)
            else:
                upstream = socket.create_connection((host, 443), timeout=10)
                enable_tcp_keepalive(upstream)
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if len(initial) > header_end:
                    upstream.sendall(initial[header_end:])
            self._remember(upstream)
            client.settimeout(None)
            upstream.settimeout(None)
            duplex_stream(client, upstream, self.stopping)
        except (OSError, ProjectionError):
            pass
        finally:
            if upstream is not None:
                self._forget(upstream)
                upstream.close()
            self._forget(client)
            client.close()
            with self.lock:
                self.threads.discard(threading.current_thread())
            self.slots.release()

    def stop(self) -> None:
        self.stopping.set()
        self.server.close()
        self._retire_connections()
        self.thread.join(timeout=2)
        with self.lock:
            threads = tuple(self.threads)
        for thread in threads:
            thread.join(timeout=2)
