#!/usr/bin/env python3
"""The small host half of the experimental devc2 Span contract."""
from __future__ import annotations

import errno
import json
import os
import re
import resource
import shutil
import select
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path


NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
MAX_CATALOG_BYTES = 64 * 1024
MAX_CLIENT_BYTES = 64 * 1024 * 1024
MAX_PROVIDER_BYTES = 64 * 1024 * 1024
MAX_CONNECTIONS = 256
LISTEN_BACKLOG = 1024
MIN_OPEN_FILES = 4096
MAX_SPANS = 16
WORLD_CONNECT_BACKOFF = (0.1, 0.5)
HTTP_ATTACHMENT_BUILTINS = {"github", "openai"}
STREAM_BUILTINS = {"ssh-agent"}
COMMAND_BUILTINS = {"diagnostics"}
SUPERVISOR = Path(__file__).with_name("span_supervisor.py")

from credential_proxy.stream_relay import duplex_stream, enable_tcp_keepalive
from launcher import world_attachment


@dataclass(frozen=True)
class Span:
    name: str
    client: Path | None
    provider: Path
    http_attachment: bool = False


def valid_name(name: str) -> bool:
    return bool(NAME.fullmatch(name))


def ensure_open_file_capacity() -> int:
    """Raise the inherited soft descriptor limit for all Span processes."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = MIN_OPEN_FILES if hard == resource.RLIM_INFINITY else min(MIN_OPEN_FILES, hard)
    if soft < target:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    return max(soft, target)


def _inside(path: Path, root: Path | None) -> bool:
    return root is not None and (path==root or root in path.parents)


def _snapshot_executable(raw: object, label: str, target: Path, maximum: int, forbidden_root: Path | None) -> Path:
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
        if not metadata.st_mode & 0o111:
            raise RuntimeError(f"Span {label} is not executable")
        output=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o500)
        with os.fdopen(descriptor,"rb") as source,os.fdopen(output,"wb") as destination:
            descriptor=None
            shutil.copyfileobj(source,destination,1024*1024)
        target.chmod(0o555)
        return target
    finally:
        if descriptor is not None: os.close(descriptor)


def load_catalog(path: Path, names: tuple[str, ...], forbidden_root: Path, client_destination: Path, provider_destination: Path, builtin_root: Path | None = None, command_projection: Path | None = None) -> list[Span]:
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
            if name in HTTP_ATTACHMENT_BUILTINS and (builtin_root/"http-credential-world").is_file():
                builtin_entries[name]=(builtin_root/"http-credential-world",None,True)
            elif world.is_file() and name in STREAM_BUILTINS:
                builtin_entries[name]=(world,None,False)
            elif world.is_file() and name in COMMAND_BUILTINS:
                if command_projection is None:
                    raise RuntimeError(f"Span {name!r} cannot be projected")
                builtin_entries[name]=(world,command_projection,False)
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
            world,link,http_attachment=entry
            provider=_snapshot_executable(str(world),f"{name!r} World",provider_destination/name,MAX_PROVIDER_BYTES,forbidden_root)
            client=None if link is None else _snapshot_executable(str(link),f"{name!r} Link",client_destination/name,MAX_CLIENT_BYTES,None)
        elif isinstance(entry,str):
            if command_projection is None:
                raise RuntimeError(f"Span {name!r} cannot be projected")
            provider=_snapshot_executable(entry,f"{name!r} World",provider_destination/name,MAX_PROVIDER_BYTES,forbidden_root)
            client=_snapshot_executable(str(command_projection),f"{name!r} command projection",client_destination/name,MAX_CLIENT_BYTES,None)
            http_attachment=False
        else:
            raise RuntimeError(f"Span {name!r} has an invalid World path")
        result.append(Span(name,client,provider,http_attachment))
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
    for catalog_name in entries:
        if not isinstance(catalog_name,str) or not valid_name(catalog_name):
            raise RuntimeError("Span catalog contains an invalid name")
    return entries


class Provider:
    def __init__(self, span: Span, workspace: Path, instance_id: str, diagnostics_path: Path | None = None):
        self.span = span
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(LISTEN_BACKLOG)
        self._address = self.listener.getsockname()
        lifetime_read, self.lifetime_write = os.pipe()
        self.started_read, started_write = os.pipe()
        self.recovery_read = None
        self.recovery_monitored = False
        recovery_write = None
        environment = dict(os.environ)
        environment.update({
            "DEVC2_SPAN_SOCKET_FD": str(self.listener.fileno()),
            "DEVC2_ISLAND_ID": instance_id,
            "DEVC2_WORKSPACE": str(workspace),
        })
        if diagnostics_path is not None and span.name == "diagnostics":
            environment["DEVC2_DIAGNOSTICS_FILE"] = str(diagnostics_path)
        if diagnostics_path is not None:
            self.recovery_read, recovery_write = os.pipe()
            environment["DEVC2_RECOVERY_FD"] = str(recovery_write)
        arguments = [
            sys.executable, os.fspath(SUPERVISOR),
            "--provider", os.fspath(span.provider),
            "--listener-fd", str(self.listener.fileno()),
            "--lifetime-fd", str(lifetime_read),
            "--started-fd", str(started_write),
        ]
        pass_fds = [self.listener.fileno(), lifetime_read, started_write]
        if recovery_write is not None:
            arguments.extend(("--recovery-fd", str(recovery_write)))
            pass_fds.append(recovery_write)
        try:
            self.process = subprocess.Popen(
                arguments,
                env=environment,
                stdin=subprocess.DEVNULL,
                pass_fds=tuple(pass_fds),
                start_new_session=True,
            )
        except Exception:
            os.close(lifetime_read)
            os.close(started_write)
            os.close(self.started_read)
            os.close(self.lifetime_write)
            self.lifetime_write = None
            if self.recovery_read is not None:
                os.close(self.recovery_read)
            if recovery_write is not None:
                os.close(recovery_write)
            self.listener.close()
            raise
        os.close(lifetime_read)
        os.close(started_write)
        if recovery_write is not None:
            os.close(recovery_write)
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
            if self.recovery_read is not None and not self.recovery_monitored:
                os.close(self.recovery_read)
                self.recovery_read = None
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
        if self.recovery_read is not None and not self.recovery_monitored:
            os.close(self.recovery_read)
            self.recovery_read = None


class DiagnosticsState:
    """Bounded, launch-local transport state readable only by its World."""

    def __init__(self, path: Path, names: list[str]):
        self.path = path
        self.lock = threading.Lock()
        self.sequence = 0
        self.links = {
            name: {
                "startup": "pending",
                "world_status": "starting",
                "world_exit": None,
                "accepted": 0,
                "active": 0,
                "completed": 0,
                "failed": 0,
                "rejected": 0,
                "last_stage": "startup",
                "last_result": "ready",
                "last_error": None,
                "recovery_state": "healthy",
                "recoveries": 0,
                "recovery_failures": 0,
            }
            for name in names
        }
        self._write()

    def _persist(self) -> None:
        try:
            self._write()
        except OSError:
            # Observation must never become a dependency of the observed Link.
            pass

    def _write(self) -> None:
        document = {
            "v": 1,
            "sequence": self.sequence,
            "links": [dict(name=name, **state) for name, state in self.links.items()],
        }
        payload = json.dumps(document, separators=(",", ":"), sort_keys=True).encode()
        descriptor, raw = tempfile.mkstemp(prefix=".diagnostics.", dir=self.path.parent)
        temporary = Path(raw)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def update(self, name: str, **changes: object) -> None:
        with self.lock:
            self.links[name].update(changes)
            self.sequence += 1
            self._persist()

    def accepted(self, name: str) -> None:
        with self.lock:
            state = self.links[name]
            state.update(
                accepted=state["accepted"] + 1,
                active=state["active"] + 1,
                last_stage="TLS handshake",
                last_result="active",
                last_error=None,
            )
            self.sequence += 1
            self._persist()

    def rejected(self, name: str) -> None:
        with self.lock:
            state = self.links[name]
            state.update(
                rejected=state["rejected"] + 1,
                last_stage="accept",
                last_result="rejected",
                last_error="connection limit",
            )
            self.sequence += 1
            self._persist()

    def recovery_started(self, name: str) -> None:
        self.update(name, recovery_state="recovering")

    def recovery_succeeded(self, name: str) -> None:
        with self.lock:
            state = self.links[name]
            state.update(
                recovery_state="healthy",
                recoveries=state["recoveries"] + 1,
            )
            self.sequence += 1
            self._persist()

    def recovery_failed(self, name: str) -> None:
        with self.lock:
            state = self.links[name]
            state.update(
                recovery_state="degraded",
                recovery_failures=state["recovery_failures"] + 1,
            )
            self.sequence += 1
            self._persist()

    def finished(self, name: str, stage: str, error: Exception | None) -> None:
        with self.lock:
            state = self.links[name]
            changes = {
                "active": max(0, state["active"] - 1),
                "last_stage": stage,
                "last_result": "failed" if error is not None else "completed",
                "last_error": type(error).__name__ if error is not None else None,
            }
            if error is None:
                changes["completed"] = state["completed"] + 1
            else:
                changes["failed"] = state["failed"] + 1
            state.update(changes)
            self.sequence += 1
            self._persist()


class OpaqueRelay:
    """Mutually authenticated TCP ingress; bytes are otherwise untouched."""

    def __init__(self, upstream: tuple[str, int], tls_directory: Path, name: str = "unknown", diagnostics: DiagnosticsState | None = None):
        self.upstream = upstream
        self.name = name
        self.diagnostics = diagnostics
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
        self.server.listen(LISTEN_BACKLOG)
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
            except OSError as error:
                if self.stopping.is_set():
                    return
                if error.errno in {errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM}:
                    self.stopping.wait(0.05)
                    continue
                raise
            while not self.stopping.is_set():
                if self.slots.acquire(timeout=0.5):
                    break
            else:
                connection.close()
                continue
            if self.diagnostics is not None:
                self.diagnostics.accepted(self.name)
            thread = threading.Thread(target=self._connection, args=(connection,), daemon=True)
            try:
                thread.start()
            except RuntimeError:
                if self.diagnostics is not None:
                    self.diagnostics.finished(self.name, "worker start", RuntimeError())
                connection.close()
                self.slots.release()


    def _connection(self, raw: socket.socket) -> None:
        tracked: socket.socket = raw
        stage = "TLS handshake"
        failure = None
        with self.connections_lock:
            self.connections.add(raw)
        try:
            enable_tcp_keepalive(raw)
            raw.settimeout(5)
            with self.context.wrap_socket(
                raw,server_side=True,do_handshake_on_connect=False,
            ) as incoming:
                tracked = incoming
                with self.connections_lock:
                    self.connections.discard(raw)
                    self.connections.add(incoming)
                incoming.do_handshake()
                incoming.settimeout(None)
                stage = "World connection"
                if self.diagnostics is not None:
                    self.diagnostics.update(self.name, last_stage=stage)
                with self._connect_world() as upstream:
                    stage = "stream relay"
                    if self.diagnostics is not None:
                        self.diagnostics.update(self.name, last_stage=stage)
                    duplex_stream(incoming, upstream, self.stopping)
        except (OSError, ssl.SSLError, RuntimeError) as error:
            failure = error
            if not self.stopping.is_set():
                print(
                    f"devc2: Span {self.name!r} host relay failed during {stage} "
                    f"({type(error).__name__})",
                    file=sys.stderr,
                    flush=True,
                )
        finally:
            if self.diagnostics is not None:
                self.diagnostics.finished(self.name, stage, failure)
            with self.connections_lock:
                self.connections.discard(raw)
                self.connections.discard(tracked)
            try:
                tracked.close()
            except OSError:
                pass
            self.slots.release()

    def _connect_world(self) -> socket.socket:
        failure = None
        for attempt in range(len(WORLD_CONNECT_BACKOFF) + 1):
            try:
                connection = socket.create_connection(self.upstream, timeout=10)
                if attempt and self.diagnostics is not None:
                    self.diagnostics.recovery_succeeded(self.name)
                return connection
            except OSError as error:
                failure = error
                if attempt == 0 and self.diagnostics is not None:
                    self.diagnostics.recovery_started(self.name)
                if attempt == len(WORLD_CONNECT_BACKOFF):
                    break
                if self.stopping.wait(WORLD_CONNECT_BACKOFF[attempt]):
                    break
        if self.diagnostics is not None:
            self.diagnostics.recovery_failed(self.name)
        raise failure or ConnectionError("World connection recovery stopped")

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
        ensure_open_file_capacity()
        self.providers: list[Provider] = []
        self.relays: list[OpaqueRelay] = []
        self.ports: dict[str, int] = {}
        self.diagnostics = None
        self.stopping = threading.Event()
        self.monitors: list[threading.Thread] = []
        try:
            if any(span.name == "diagnostics" for span in spans):
                self.diagnostics = DiagnosticsState(tls_directory.parent / "diagnostics.json", [span.name for span in spans])
            for span in spans:
                provider = Provider(
                    span, workspace, instance_id,
                    self.diagnostics.path if self.diagnostics is not None else None,
                )
                self.providers.append(provider)
                provider.check_started()
                if self.diagnostics is not None:
                    self.diagnostics.update(
                        span.name,
                        startup="passed",
                        world_status="running",
                    )
                    monitor = threading.Thread(
                        target=self._monitor_provider,
                        args=(provider,),
                        daemon=True,
                    )
                    provider.recovery_monitored = True
                    try:
                        monitor.start()
                    except RuntimeError:
                        provider.recovery_monitored = False
                        raise
                    self.monitors.append(monitor)
                relay = OpaqueRelay(provider.address, tls_directory, span.name, self.diagnostics)
                self.relays.append(relay)
                self.ports[span.name] = relay.port
        except Exception:
            self.stop()
            raise

    def materialize_http_attachments(self, public: Path) -> dict[str, str]:
        worlds = [
            (provider.span.name, provider.address)
            for provider in self.providers
            if provider.span.http_attachment
        ]
        return world_attachment.materialize(public, worlds)

    def _monitor_provider(self, provider: Provider) -> None:
        recovery = provider.recovery_read
        try:
            while not self.stopping.is_set():
                if recovery is not None:
                    readable, _writable, _errors = select.select(
                        [recovery], [], [], 0.1,
                    )
                    if readable:
                        events = os.read(recovery, 64)
                        if not events:
                            os.close(recovery)
                            recovery = None
                        for event in events:
                            if event == ord("S"):
                                self.diagnostics.recovery_started(provider.span.name)
                            elif event == ord("R"):
                                self.diagnostics.recovery_succeeded(provider.span.name)
                            elif event == ord("F"):
                                self.diagnostics.recovery_failed(provider.span.name)
                else:
                    self.stopping.wait(0.1)
                status = provider.process.poll()
                if status is not None:
                    if self.diagnostics is not None:
                        self.diagnostics.update(
                            provider.span.name,
                            world_status="exited",
                            world_exit=status,
                        )
                    return
        finally:
            if recovery is not None:
                os.close(recovery)
            provider.recovery_read = None
            provider.recovery_monitored = False

    def stop(self) -> None:
        self.stopping.set()
        for relay in reversed(self.relays):
            relay.stop()
        self.relays.clear()
        for provider in reversed(self.providers):
            provider.stop()
        self.providers.clear()
        for monitor in self.monitors:
            monitor.join(timeout=1)
        self.monitors.clear()
