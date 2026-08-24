"""Bounded HTTP CONNECT proxy for launch-scoped Span routes."""
from __future__ import annotations

from pathlib import Path
import re
import socket
import threading

from .stream_relay import RESUME_GAP_SECONDS, duplex_stream, enable_tcp_keepalive, serve_connections, transport_clocks, transport_gap
from .config import MAX_CONFIG, NAME, read_json, valid_span_name


MAX_HEADERS = 16 * 1024
MAX_CONNECTIONS = 256
LISTEN_BACKLOG = 1024
DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class ConnectProxyError(RuntimeError):
    pass


def load_routes(path: Path, allowed_names: set[str]) -> dict[str, str]:
    document = read_json(path, ConnectProxyError("HTTP attachment config is too large"))
    if (not isinstance(document, dict) or set(document) != {"v", "routes"}
            or type(document["v"]) is not int or document["v"] != 1):
        raise ConnectProxyError("invalid HTTP attachment config")
    routes = document["routes"]
    if not isinstance(routes, dict) or len(routes) > 256:
        raise ConnectProxyError("invalid HTTP attachment routes")
    result = {}
    for authority, name in routes.items():
        if (not isinstance(authority, str) or not authority.endswith(":443")
                or not valid_dns(authority[:-4]) or authority != authority.lower()
                or not valid_span_name(name)
                or name not in allowed_names):
            raise ConnectProxyError("invalid HTTP attachment route")
        result[authority] = name
    return result


def valid_dns(host: str) -> bool:
    return bool(host) and len(host) <= 253 and all(
        DNS_LABEL.fullmatch(label) for label in host.split(".")
    )


def connect_target(first_line: bytes) -> tuple[str, str]:
    fields = first_line.split(b" ")
    if len(fields) != 3 or fields[0] != b"CONNECT" or fields[2] not in (b"HTTP/1.0", b"HTTP/1.1"):
        raise ConnectProxyError("unsupported proxy request")
    try:
        authority = fields[1].decode("ascii")
    except UnicodeError as error:
        raise ConnectProxyError("invalid proxy target") from error
    if (authority != authority.lower() or not authority.endswith(":443")
            or not valid_dns(authority[:-4])):
        raise ConnectProxyError("invalid proxy target")
    return authority[:-4], authority


class HttpConnectProxy:
    def __init__(self, routes: dict[str, str], socket_dir: Path, port: int = 3128, slots=None, stopping=None, failed=None):
        self.routes = routes
        self.socket_dir = socket_dir
        self.stopping = stopping or threading.Event()
        self.failed = failed or threading.Event()
        self.slots = slots or threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.lock = threading.Lock()
        self.connections: set[socket.socket] = set()
        self.threads: set[threading.Thread] = set()
        self.resume_epoch = 0
        self.last_observed_time = transport_clocks()
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("0.0.0.0", port))
        self.server.listen(LISTEN_BACKLOG)
        self.server.settimeout(0.5)
        self.thread = threading.Thread(target=self._serve, name="http-span-proxy", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _retire_connections(self) -> int:
        with self.lock:
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

    def _snapshot_epoch(self) -> int:
        with self.lock:
            return self.resume_epoch

    def _remember(self, epoch: int, *connections: socket.socket) -> bool:
        with self.lock:
            current=epoch==self.resume_epoch
            if current: self.connections.update(connections)
        if not current:
            for connection in connections: connection.close()
        return current

    def _forget(self, *connections: socket.socket) -> None:
        with self.lock:
            for connection in connections:
                self.connections.discard(connection)

    def _retire_after_resume(self, observed_time: tuple[float, float]) -> bool:
        gap = transport_gap(self.last_observed_time, observed_time)
        self.last_observed_time = observed_time
        if gap <= RESUME_GAP_SECONDS:
            return False
        retired=self._retire_connections()
        if retired:
            print("span-gateway: HTTP proxy retired pre-resume transport",flush=True)
        return True

    def _serve(self) -> None:
        try:
            serve_connections(
                self.server, self.stopping, self.slots,
                self._snapshot_epoch, self._retire_after_resume, self._start_worker,
            )
        except Exception as error:
            if not self.stopping.is_set():
                print(
                    f"span-gateway: HTTP listener failed ({type(error).__name__})",
                    flush=True,
                )
                self.failed.set()
                self.stopping.set()

    def _start_worker(self, client: socket.socket, epoch: int) -> None:
        thread = threading.Thread(target=self._handle, args=(client,epoch), daemon=True)
        with self.lock:
            self.threads.add(thread)
        try:
            thread.start()
        except RuntimeError:
            with self.lock:
                self.threads.discard(thread)
            client.close()
            self.slots.release()

    def _handle(self, client: socket.socket, epoch: int) -> None:
        upstream = None
        if not self._remember(epoch,client):
            self.slots.release()
            return
        try:
            client.settimeout(10)
            initial = bytearray()
            marker = -1
            while marker < 0:
                if len(initial) >= MAX_HEADERS:
                    raise ConnectProxyError("proxy headers exceed 16 KiB")
                block = client.recv(min(4096, MAX_HEADERS - len(initial)))
                if not block:
                    raise ConnectProxyError("proxy request ended early")
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
                if not self._remember(epoch,upstream): return
                upstream.sendall(b"T" + initial)
            else:
                upstream = socket.create_connection((host, 443), timeout=10)
                if not self._remember(epoch,upstream): return
                enable_tcp_keepalive(upstream)
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if len(initial) > header_end:
                    upstream.sendall(initial[header_end:])
            client.settimeout(None)
            upstream.settimeout(None)
            duplex_stream(client, upstream, self.stopping)
        except (OSError, ConnectProxyError):
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
