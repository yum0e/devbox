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
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path


NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
MAX_CATALOG_BYTES = 64 * 1024
MAX_CLIENT_BYTES = 64 * 1024 * 1024
MAX_PROVIDER_BYTES = 64 * 1024 * 1024
MAX_CONNECTIONS = 64
MAX_SPANS = 16
SCOPED_EXEC_BUILTINS = {"github", "openai"}
STREAM_BUILTINS = {"ssh-agent"}
SUPERVISOR = Path(__file__).with_name("span_supervisor.py")


@dataclass(frozen=True)
class Span:
    name: str
    client: Path | None
    provider: Path


def valid_name(name: str) -> bool:
    return bool(NAME.fullmatch(name))


def _inside(path: Path, root: Path | None) -> bool:
    return root is not None and (path==root or root in path.parents)


def _snapshot_executable(raw: object, label: str, target: Path, maximum: int, forbidden_root: Path | None, require_executable: bool = True) -> Path:
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        raise RuntimeError(f"Span {label} must be an absolute path")
    descriptor=None
    try:
        path = Path(raw).resolve(strict=True)
        if _inside(path,forbidden_root):
            raise RuntimeError(f"Span {label} must live outside the mounted workspace")
        descriptor=os.open(path,os.O_RDONLY|getattr(os,"O_CLOEXEC",0)|getattr(os,"O_NOFOLLOW",0))
        metadata=os.fstat(descriptor)
    except OSError as error:
        raise RuntimeError(f"Span {label} is unavailable: {raw}") from error
    try:
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size>maximum:
            raise RuntimeError(f"Span {label} must be a regular file no larger than 64 MiB")
        if metadata.st_nlink!=1:
            raise RuntimeError(f"Span {label} must not have hard links")
        if metadata.st_uid not in {0,os.getuid()} or metadata.st_mode & 0o022:
            raise RuntimeError(f"Span {label} must be owned by root/current user and not group/world-writable")
        if require_executable and not metadata.st_mode & 0o111:
            raise RuntimeError(f"Span {label} is not executable")
        output=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o500)
        with os.fdopen(descriptor,"rb") as source,os.fdopen(output,"wb") as destination:
            descriptor=None
            shutil.copyfileobj(source,destination,1024*1024)
        target.chmod(0o555)
        return target
    finally:
        if descriptor is not None: os.close(descriptor)


def load_catalog(path: Path, names: tuple[str, ...], forbidden_root: Path, client_destination: Path, provider_destination: Path, builtin_root: Path | None = None, command_projection: Path | None = None, scoped_exec_projection: Path | None = None) -> list[Span]:
    if len(names)>MAX_SPANS:
        raise RuntimeError(f"an Island may grant at most {MAX_SPANS} Spans")
    client_destination.mkdir(mode=0o700)
    provider_destination.mkdir(mode=0o700)
    if not names:
        client_destination.chmod(0o555)
        return []
    for name in names:
        if not valid_name(name):
            raise RuntimeError(f"invalid Span name: {name!r}")
    builtin_entries={}
    if builtin_root is not None:
        for name in names:
            world=builtin_root/name/"world"
            provider=builtin_root/name/"provider"
            client=builtin_root/name/"client"
            if world.is_file() and name in SCOPED_EXEC_BUILTINS:
                if scoped_exec_projection is None:
                    raise RuntimeError(f"Span {name!r} cannot be projected")
                builtin_entries[name]=(world,scoped_exec_projection)
            elif world.is_file() and name in STREAM_BUILTINS:
                builtin_entries[name]=(world,None)
            elif provider.is_file() and client.is_file():
                builtin_entries[name]={"provider":str(provider),"client":str(client)}
    external_names=[name for name in names if name not in builtin_entries]
    forbidden_root=forbidden_root.resolve()
    if not external_names:
        entries={}
    else:
        entries=_read_catalog(path,forbidden_root)
    entries={**entries,**builtin_entries}
    result=[]
    for name in names:
        entry=entries.get(name)
        if entry is None:
            raise RuntimeError(f"Span {name!r} is unavailable")
        if isinstance(entry,tuple):
            world,link=entry
            # The immediately previous updater normalizes unknown new assets to
            # 0444. Built-ins are part of devc2's immutable validated tree, so
            # snapshot them read-only and restore execute permission only on the
            # per-launch copies. Catalog-supplied executables remain strict.
            provider=_snapshot_executable(str(world),f"{name!r} World",provider_destination/name,MAX_PROVIDER_BYTES,forbidden_root,False)
            client=None if link is None else _snapshot_executable(str(link),f"{name!r} scoped-exec Link",client_destination/name,MAX_CLIENT_BYTES,None,False)
        elif isinstance(entry,str):
            if command_projection is None:
                raise RuntimeError(f"Span {name!r} cannot be projected")
            provider=_snapshot_executable(entry,f"{name!r} World",provider_destination/name,MAX_PROVIDER_BYTES,forbidden_root)
            client=_snapshot_executable(str(command_projection),f"{name!r} command projection",client_destination/name,MAX_CLIENT_BYTES,None)
        else:
            provider=_snapshot_executable(entry["provider"],f"{name!r} provider",provider_destination/name,MAX_PROVIDER_BYTES,forbidden_root)
            client=_snapshot_executable(entry["client"],f"{name!r} client",client_destination/name,MAX_CLIENT_BYTES,forbidden_root)
        result.append(Span(name,client,provider))
    client_destination.chmod(0o555)
    return result


def _read_catalog(path: Path, forbidden_root: Path) -> dict:
    if _inside(path.resolve(strict=False),forbidden_root):
        raise RuntimeError("Span catalog must live outside the mounted workspace")
    descriptor=None
    try:
        descriptor=os.open(path,os.O_RDONLY|getattr(os,"O_CLOEXEC",0)|getattr(os,"O_NOFOLLOW",0))
        metadata=os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("Span catalog must be a regular file, not a symlink")
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
            raise RuntimeError("Span catalog must be owned by the current user and not group/world-writable")
        if metadata.st_size > MAX_CATALOG_BYTES:
            raise RuntimeError("Span catalog exceeds 64 KiB")
        with os.fdopen(descriptor,"rb") as source:
            descriptor=None
            payload=source.read(MAX_CATALOG_BYTES+1)
            if len(payload)>MAX_CATALOG_BYTES:
                raise RuntimeError("Span catalog exceeds 64 KiB")
            document=json.loads(payload)
    except FileNotFoundError as error:
        raise RuntimeError(f"Span catalog not found: {path}") from error
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise RuntimeError(f"Span catalog is unreadable or invalid JSON: {path}") from error
    finally:
        if descriptor is not None: os.close(descriptor)
    if not isinstance(document, dict) or set(document) != {"spans"} or not isinstance(document["spans"], dict):
        raise RuntimeError("Span catalog must contain only a spans object")
    entries = document["spans"]
    if len(entries) > 64:
        raise RuntimeError("Span catalog contains too many entries")
    for catalog_name,entry in entries.items():
        if not isinstance(catalog_name,str) or not valid_name(catalog_name):
            raise RuntimeError("Span catalog contains an invalid name")
        if isinstance(entry,str):
            if not Path(entry).is_absolute():
                raise RuntimeError(f"Span {catalog_name!r} has an invalid World path")
            continue
        if not isinstance(entry,dict) or set(entry)!={"provider","client"}:
            raise RuntimeError(f"Span {catalog_name!r} has an invalid catalog entry")
        if not all(isinstance(entry[field],str) for field in ("provider","client")):
            raise RuntimeError(f"Span {catalog_name!r} has an invalid catalog path")
    return entries


class Provider:
    def __init__(self, span: Span, workspace: Path, instance_id: str):
        self.span = span
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(64)
        self._address = self.listener.getsockname()
        lifetime_read, self.lifetime_write = os.pipe()
        self.started_read, started_write = os.pipe()
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
                    "--provider", os.fspath(span.provider),
                    "--listener-fd", str(self.listener.fileno()),
                    "--lifetime-fd", str(lifetime_read),
                    "--started-fd", str(started_write),
                ],
                env=environment,
                stdin=subprocess.DEVNULL,
                pass_fds=(self.listener.fileno(), lifetime_read, started_write),
                start_new_session=True,
            )
        except Exception:
            os.close(lifetime_read)
            os.close(started_write)
            os.close(self.started_read)
            os.close(self.lifetime_write)
            self.lifetime_write = None
            self.listener.close()
            raise
        os.close(lifetime_read)
        os.close(started_write)
        self.listener.close()

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._address
        return host, port

    def check_started(self) -> None:
        readable, _writable, _errors = select.select([self.started_read], [], [], 2)
        if readable and os.read(self.started_read,1)==b"1":
            os.close(self.started_read)
            self.started_read = None
            return
        if self.started_read is not None:
            os.close(self.started_read)
            self.started_read = None
        try:
            status = self.process.wait(timeout=2)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"Span {self.span.name!r} provider did not stay running through startup") from error
        raise RuntimeError(f"Span {self.span.name!r} provider exited with status {status}")

    def stop(self) -> None:
        if self.started_read is not None:
            os.close(self.started_read)
            self.started_read = None
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
    def __init__(self, spans: list[Span], workspace: Path, instance_id: str, tls_directory: Path):
        self.providers: list[Provider] = []
        self.relays: list[OpaqueRelay] = []
        self.ports: dict[str, int] = {}
        try:
            for span in spans:
                provider = Provider(span, workspace, instance_id)
                self.providers.append(provider)
                provider.check_started()
                relay = OpaqueRelay(provider.address, tls_directory)
                self.relays.append(relay)
                self.ports[span.name] = relay.port
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
