#!/usr/bin/env python3
"""The small host half of the experimental devc2 Span contract."""
from __future__ import annotations

import json
import os
import re
import shutil
import select
import socket
import ssl
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path


NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
MAX_DESCRIPTION_BYTES = 64 * 1024
MAX_CLIENT_BYTES = 64 * 1024 * 1024
MAX_CONNECTIONS = 64
MAX_SPANS = 16
SUPERVISOR = Path(__file__).with_name("span_supervisor.py")


@dataclass(frozen=True)
class SpanDescription:
    name: str
    version: str
    client: Path
    provider: Path


def valid_name(name: str) -> bool:
    return bool(NAME.fullmatch(name))


def describe(name: str, *, search_path: str | None = None) -> SpanDescription:
    if not valid_name(name):
        raise RuntimeError(f"invalid Span name: {name!r}")
    command = shutil.which(f"{name}-span", path=search_path)
    if not command:
        raise RuntimeError(f"Span provider not found on host PATH: {name}-span")
    provider = Path(command).resolve(strict=True)
    try:
        completed = subprocess.run(
            [str(provider), "describe"], text=False, capture_output=True,
            check=False, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"could not describe Span {name!r}") from error
    if completed.returncode:
        detail = completed.stderr[:4096].decode("utf-8", "replace").strip()
        raise RuntimeError(f"{name}-span describe failed{': ' + detail if detail else ''}")
    if len(completed.stdout) > MAX_DESCRIPTION_BYTES:
        raise RuntimeError(f"{name}-span describe returned too much data")
    try:
        document = json.loads(completed.stdout)
    except (UnicodeDecodeError, ValueError) as error:
        raise RuntimeError(f"{name}-span describe did not return JSON") from error
    if not isinstance(document, dict) or set(document) != {"name", "version", "client"}:
        raise RuntimeError(f"{name}-span describe must return only name, version, and client")
    if document["name"] != name:
        raise RuntimeError(f"{name}-span described a different Span name")
    if not isinstance(document["version"], str) or not document["version"]:
        raise RuntimeError(f"{name}-span described an invalid version")
    if not isinstance(document["client"], str) or not Path(document["client"]).is_absolute():
        raise RuntimeError(f"{name}-span client must be an absolute path")
    client = Path(document["client"]).resolve(strict=True)
    metadata = client.stat()
    if not client.is_file() or metadata.st_size > MAX_CLIENT_BYTES:
        raise RuntimeError(f"{name}-span client must be a regular file no larger than 64 MiB")
    if not metadata.st_mode & 0o111:
        raise RuntimeError(f"{name}-span client is not executable")
    return SpanDescription(name, document["version"], client, provider)


def project_clients(descriptions: list[SpanDescription], destination: Path) -> None:
    if len(descriptions) > MAX_SPANS:
        raise RuntimeError(f"an Island may grant at most {MAX_SPANS} Spans")
    destination.mkdir(mode=0o700)
    for item in descriptions:
        target = destination / item.name
        shutil.copyfile(item.client, target)
        target.chmod(0o555)
    destination.chmod(0o555)


class Provider:
    def __init__(self, description: SpanDescription, workspace: Path, instance_id: str):
        self.description = description
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(64)
        self._address = self.listener.getsockname()
        lifetime_read, self.lifetime_write = os.pipe()
        self.ready_read, ready_write = os.pipe()
        environment = dict(os.environ)
        environment.update({
            "DEVC2_SPAN_SOCKET_FD": str(self.listener.fileno()),
            "DEVC2_ISLAND_ID": instance_id,
            "DEVC2_WORKSPACE": str(workspace),
        })
        try:
            self.process = subprocess.Popen(
                [
                    sys.executable, os.fspath(SUPERVISOR),
                    "--provider", os.fspath(description.provider),
                    "--listener-fd", str(self.listener.fileno()),
                    "--lifetime-fd", str(lifetime_read),
                    "--ready-fd", str(ready_write),
                ],
                env=environment,
                stdin=subprocess.DEVNULL,
                pass_fds=(self.listener.fileno(), lifetime_read, ready_write),
                start_new_session=True,
            )
        except Exception:
            os.close(lifetime_read)
            os.close(ready_write)
            os.close(self.ready_read)
            os.close(self.lifetime_write)
            self.lifetime_write = None
            self.listener.close()
            raise
        os.close(lifetime_read)
        os.close(ready_write)
        self.listener.close()

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._address
        return host, port

    def check_started(self) -> None:
        readable, _writable, _errors = select.select([self.ready_read], [], [], 2)
        if readable and os.read(self.ready_read,1)==b"1":
            os.close(self.ready_read)
            self.ready_read = None
            return
        if self.ready_read is not None:
            os.close(self.ready_read)
            self.ready_read = None
        try:
            status = self.process.wait(timeout=2)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"{self.description.name}-span serve did not become ready") from error
        raise RuntimeError(f"{self.description.name}-span serve exited with status {status}")

    def stop(self) -> None:
        if self.ready_read is not None:
            os.close(self.ready_read)
            self.ready_read = None
        try:
            if self.lifetime_write is not None:
                os.close(self.lifetime_write)
                self.lifetime_write = None
        except OSError:
            pass
        if self.process.poll() is not None:
            return
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


class OpaqueRelay:
    """Mutually authenticated TCP ingress; bytes are otherwise untouched."""

    def __init__(self, upstream: tuple[str, int], tls_directory: Path):
        self.upstream = upstream
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.options |= ssl.OP_NO_COMPRESSION
        self.context.verify_mode = ssl.CERT_REQUIRED
        self.context.load_verify_locations(cafile=str(tls_directory / "ca.crt"))
        self.context.load_cert_chain(
            certfile=str(tls_directory / "server.crt"),
            keyfile=str(tls_directory / "server.key"),
        )
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("0.0.0.0", 0))
        self.server.listen(64)
        self.server.settimeout(0.5)
        self.stopping = threading.Event()
        self.connections: set[socket.socket] = set()
        self.connections_lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return self.server.getsockname()[1]

    def _serve(self) -> None:
        while not self.stopping.is_set():
            try:
                connection, _address = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.stopping.is_set():
                    return
                raise
            if not self.slots.acquire(blocking=False):
                connection.close()
                continue
            thread = threading.Thread(target=self._connection, args=(connection,), daemon=True)
            try:
                thread.start()
            except RuntimeError:
                connection.close()
                self.slots.release()

    @staticmethod
    def _copy(source: socket.socket, destination: socket.socket) -> None:
        try:
            while chunk := source.recv(64 * 1024):
                destination.sendall(chunk)
        except OSError:
            pass
        try:
            destination.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def _connection(self, raw: socket.socket) -> None:
        tracked: socket.socket = raw
        with self.connections_lock:
            self.connections.add(raw)
        try:
            raw.settimeout(5)
            with self.context.wrap_socket(raw, server_side=True) as incoming:
                tracked = incoming
                with self.connections_lock:
                    self.connections.discard(raw)
                    self.connections.add(incoming)
                incoming.settimeout(None)
                with socket.create_connection(self.upstream, timeout=10) as upstream:
                    upstream.settimeout(None)
                    reverse = threading.Thread(target=self._copy, args=(upstream, incoming), daemon=True)
                    reverse.start()
                    self._copy(incoming, upstream)
                    reverse.join()
        except (OSError, ssl.SSLError, RuntimeError):
            pass
        finally:
            with self.connections_lock:
                self.connections.discard(raw)
                self.connections.discard(tracked)
            try:
                tracked.close()
            except OSError:
                pass
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


class SpanRuntime:
    def __init__(self, descriptions: list[SpanDescription], workspace: Path, instance_id: str, tls_directory: Path):
        self.providers: list[Provider] = []
        self.relays: list[OpaqueRelay] = []
        self.ports: dict[str, int] = {}
        try:
            for description in descriptions:
                provider = Provider(description, workspace, instance_id)
                self.providers.append(provider)
                provider.check_started()
                relay = OpaqueRelay(provider.address, tls_directory)
                self.relays.append(relay)
                self.ports[description.name] = relay.port
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        for relay in reversed(self.relays):
            relay.stop()
        self.relays.clear()
        for provider in reversed(self.providers):
            provider.stop()
        self.providers.clear()
