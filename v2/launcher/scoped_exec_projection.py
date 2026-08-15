#!/usr/bin/env python3
"""Generic manifest-driven projected command Link."""

import base64
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from typing import Set


NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
PLACEHOLDER = re.compile(r"\$\{[^}]*\}")
HEADER = struct.Struct("!I")
MAX_MANIFEST = 2 * 1024 * 1024
MAX_CA = 1024 * 1024
MAX_SYSTEM_CA = 16 * 1024 * 1024
MAX_FILES = 16
MAX_FILE_BYTES = 1024 * 1024
MAX_ENVIRONMENT = 32
MAX_ENV_NAME = 255
MAX_ENV_VALUE = 64 * 1024
MAX_ROUTES = 16
MAX_PROXY_HEADERS = 16 * 1024
MAX_HANDLERS = 16
MAX_PENDING = 1024 * 1024
CHUNK = 64 * 1024


class LinkError(RuntimeError):
    pass


def receive(connection, size):
    result = bytearray()
    while len(result) < size:
        block = connection.recv(size - len(result))
        if not block:
            raise LinkError("World response ended early")
        result.extend(block)
    return bytes(result)


def decode_b64(value, description):
    if not isinstance(value, str):
        raise LinkError("invalid %s" % description)
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, UnicodeError) as error:
        raise LinkError("invalid %s" % description) from error


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LinkError("duplicate JSON object field")
        result[key] = value
    return result


def utf8_size(value):
    try:
        return len(value.encode("utf-8"))
    except UnicodeError as error:
        raise LinkError("invalid Unicode in World manifest") from error


def valid_dns(host):
    if not isinstance(host, str) or not host or len(host) > 253:
        return False
    if host.endswith("."):
        host = host[:-1]
    return bool(host) and all(DNS_LABEL.fullmatch(label) for label in host.split("."))


def validate_file_name(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise LinkError("invalid manifest file name")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise LinkError("invalid manifest file name")
    utf8_size(value)
    return value


def validate_manifest(document):
    required = {"v", "ca_b64", "files", "environment", "routes"}
    if not isinstance(document, dict) or set(document) != required:
        raise LinkError("invalid World manifest schema")
    if type(document["v"]) is not int or document["v"] != 1:
        raise LinkError("unsupported World manifest version")

    certificate = decode_b64(document["ca_b64"], "World CA")
    if not certificate or len(certificate) > MAX_CA:
        raise LinkError("World CA exceeds its size limit")
    if b"PRIVATE KEY" in certificate.upper():
        raise LinkError("World CA contains private key material")
    try:
        pem = certificate.decode("ascii")
        if "-----BEGIN CERTIFICATE-----" not in pem or "-----END CERTIFICATE-----" not in pem:
            raise ValueError
        ssl.create_default_context(cadata=pem)
    except (UnicodeError, ValueError, ssl.SSLError) as error:
        raise LinkError("World CA is not a valid public PEM certificate") from error

    raw_files = document["files"]
    if not isinstance(raw_files, list) or len(raw_files) > MAX_FILES:
        raise LinkError("invalid manifest files")
    files = []
    names = set()
    total = 0
    for entry in raw_files:
        if not isinstance(entry, dict) or set(entry) != {"name", "contents_b64"}:
            raise LinkError("invalid manifest file")
        name = validate_file_name(entry["name"])
        if name in names or any(name.startswith(old + "/") or old.startswith(name + "/") for old in names):
            raise LinkError("duplicate or conflicting manifest file name")
        names.add(name)
        contents = decode_b64(entry["contents_b64"], "manifest file contents")
        total += len(contents)
        if total > MAX_FILE_BYTES:
            raise LinkError("manifest files exceed 1 MiB")
        files.append((name, contents))

    raw_environment = document["environment"]
    if not isinstance(raw_environment, dict) or len(raw_environment) > MAX_ENVIRONMENT:
        raise LinkError("invalid manifest environment")
    environment = {}
    for key, value in raw_environment.items():
        if (not isinstance(key, str) or len(key) > MAX_ENV_NAME or not ENV_NAME.fullmatch(key)
                or "\x00" in key or not isinstance(value, str) or "\x00" in value
                or utf8_size(value) > MAX_ENV_VALUE):
            raise LinkError("invalid manifest environment entry")
        environment[key] = value

    raw_routes = document["routes"]
    if not isinstance(raw_routes, list) or len(raw_routes) > MAX_ROUTES:
        raise LinkError("invalid manifest routes")
    routes = set()
    for route in raw_routes:
        if not isinstance(route, str) or not route.endswith(":443"):
            raise LinkError("invalid manifest route")
        host = route[:-4]
        if not valid_dns(host) or host.endswith(".") or route != route.lower():
            raise LinkError("invalid manifest route")
        if route in routes:
            raise LinkError("duplicate manifest route")
        routes.add(route)
    return certificate, files, environment, routes


def bootstrap(socket_path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(30)
        connection.connect(socket_path)
        connection.sendall(b"B")
        size = HEADER.unpack(receive(connection, HEADER.size))[0]
        if not size or size > MAX_MANIFEST:
            raise LinkError("World manifest exceeds 2 MiB")
        raw = receive(connection, size)
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise LinkError("World returned invalid manifest JSON") from error
    return validate_manifest(document)


def system_ca():
    paths = ssl.get_default_verify_paths()
    for raw_path in (paths.cafile, paths.openssl_cafile):
        if not raw_path:
            continue
        path = Path(raw_path)
        try:
            metadata = path.stat()
            if not path.is_file() or not 0 < metadata.st_size <= MAX_SYSTEM_CA:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if b"-----BEGIN CERTIFICATE-----" in data:
            return data
    raise LinkError("system CA bundle is unavailable")


def private_write(path, contents):
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(contents)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
    finally:
        os.close(descriptor)


def proxy_address(raw):
    if raw is None:
        return None
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise LinkError("inherited HTTPS proxy is invalid") from error
    if (parsed.scheme.lower() != "http" or not parsed.hostname
            or port is None
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise LinkError("inherited HTTPS proxy is invalid")
    return parsed.hostname, port


def inherited_proxy():
    raw = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    return proxy_address(raw)


def connect_target(first_line):
    fields = first_line.split(b" ")
    if len(fields) != 3 or fields[0] != b"CONNECT" or fields[2] not in (b"HTTP/1.0", b"HTTP/1.1"):
        raise LinkError("unsupported proxy request")
    try:
        authority = fields[1].decode("ascii")
    except UnicodeError as error:
        raise LinkError("proxy target is invalid") from error
    if not authority.endswith(":443"):
        raise LinkError("proxy target is invalid")
    host = authority[:-4]
    if not valid_dns(host.lower()) or host.endswith("."):
        raise LinkError("proxy target is invalid")
    return host, host.lower() + ":443"


class Adapter:
    def __init__(self, world_path, routes, fallback):
        self.world_path = world_path
        self.routes = routes
        self.fallback = fallback
        self.stopping = threading.Event()
        self.slots = threading.BoundedSemaphore(MAX_HANDLERS)
        self.lock = threading.Lock()
        self.connections = set()  # type: Set[socket.socket]
        self.threads = set()  # type: Set[threading.Thread]
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(MAX_HANDLERS)
        self.server.settimeout(0.5)
        self.thread = threading.Thread(target=self._serve, name="scoped-link-adapter", daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.server.getsockname()[1]

    def _remember(self, *connections):
        with self.lock:
            self.connections.update(connections)

    def _forget(self, *connections):
        with self.lock:
            for connection in connections:
                self.connections.discard(connection)

    def _serve(self):
        while not self.stopping.is_set():
            try:
                client, _address = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self.slots.acquire(blocking=False):
                client.close()
                continue
            thread = threading.Thread(target=self._handle, args=(client,), daemon=True)
            with self.lock:
                self.threads.add(thread)
            thread.start()

    def _handle(self, client):
        upstream = None
        self._remember(client)
        try:
            client.settimeout(10)
            initial = bytearray()
            end = -1
            while end < 0:
                if len(initial) >= MAX_PROXY_HEADERS:
                    raise LinkError("proxy request headers exceed 16 KiB")
                block = client.recv(min(4096, MAX_PROXY_HEADERS - len(initial)))
                if not block:
                    raise LinkError("proxy request ended early")
                initial.extend(block)
                end = initial.find(b"\r\n\r\n")
            header_end = end + 4
            first_line = bytes(initial[:end]).split(b"\r\n", 1)[0]
            host, authority = connect_target(first_line)
            if authority in self.routes:
                upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                upstream.settimeout(30)
                upstream.connect(self.world_path)
                upstream.sendall(b"T")
                upstream.sendall(initial)
            elif self.fallback is not None:
                upstream = socket.create_connection(self.fallback, timeout=10)
                upstream.sendall(initial)
            else:
                upstream = socket.create_connection((host, 443), timeout=10)
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if len(initial) > header_end:
                    upstream.sendall(initial[header_end:])
            self._remember(upstream)
            client.setblocking(False)
            upstream.setblocking(False)
            self._relay(client, upstream)
        except (OSError, LinkError):
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

    def _relay(self, left, right):
        selector = selectors.DefaultSelector()
        peers = {left: right, right: left}
        pending = {left: bytearray(), right: bytearray()}
        readable = {left: True, right: True}
        write_closed = {left: False, right: False}

        def refresh(connection):
            if connection.fileno() < 0:
                return
            events = 0
            if readable[connection] and len(pending[peers[connection]]) < MAX_PENDING:
                events |= selectors.EVENT_READ
            if pending[connection]:
                events |= selectors.EVENT_WRITE
            try:
                if events:
                    selector.modify(connection, events)
                else:
                    selector.unregister(connection)
            except (KeyError, ValueError):
                if events:
                    try:
                        selector.register(connection, events)
                    except (KeyError, ValueError):
                        pass

        try:
            refresh(left)
            refresh(right)
            while not self.stopping.is_set():
                if not any(readable.values()) and not any(pending.values()):
                    return
                for key, mask in selector.select(0.5):
                    connection = key.fileobj
                    peer = peers[connection]
                    if mask & selectors.EVENT_READ:
                        try:
                            data = connection.recv(min(CHUNK, MAX_PENDING - len(pending[peer])))
                        except BlockingIOError:
                            data = None
                        if data:
                            pending[peer].extend(data)
                        elif data == b"":
                            readable[connection] = False
                    if mask & selectors.EVENT_WRITE and pending[connection]:
                        try:
                            sent = connection.send(pending[connection])
                        except BlockingIOError:
                            sent = 0
                        del pending[connection][:sent]
                    if not readable[peer] and not pending[connection] and not write_closed[connection]:
                        try:
                            connection.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
                        write_closed[connection] = True
                    refresh(connection)
                    refresh(peer)
        finally:
            selector.close()

    def close(self):
        self.stopping.set()
        self.server.close()
        with self.lock:
            connections = tuple(self.connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        self.thread.join(timeout=2)
        with self.lock:
            threads = tuple(self.threads)
        for thread in threads:
            thread.join(timeout=2)


def ca_name(file_names):
    index = 0
    while True:
        candidate = ".island-ca.pem" if index == 0 else ".island-ca-%d.pem" % index
        if all(name != candidate and not name.startswith(candidate + "/")
               and not candidate.startswith(name + "/") for name in file_names):
            return candidate
        index += 1


def materialize(root, certificate, files):
    for name, contents in files:
        target = root.joinpath(*name.split("/"))
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_write(target, contents)
    name = ca_name({name for name, _contents in files})
    ca_path = root / name
    bundle = system_ca().rstrip(b"\n") + b"\n" + certificate.rstrip(b"\n") + b"\n"
    private_write(ca_path, bundle)
    return ca_path


def child_environment(manifest, root, ca_path, proxy):
    replacements = {
        "${ROOT}": str(root),
        "${CA}": str(ca_path),
        "${PROXY}": proxy,
    }
    result = {}
    for key, raw_value in manifest.items():
        value = raw_value
        for token, replacement in replacements.items():
            value = value.replace(token, replacement)
        if PLACEHOLDER.search(value):
            raise LinkError("unresolved manifest environment placeholder")
        if "\x00" in value or utf8_size(value) > MAX_ENV_VALUE:
            raise LinkError("expanded manifest environment value is invalid")
        result[key] = value
    return result


def process_group_exists(group):
    try:
        os.killpg(group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def terminate_process_group(group):
    if not process_group_exists(group):
        return
    try:
        os.killpg(group, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not process_group_exists(group):
            return
        time.sleep(0.05)
    try:
        os.killpg(group, signal.SIGKILL)
    except ProcessLookupError:
        pass


def preserve_signal_status(return_code):
    if return_code >= 0:
        return return_code
    signum = -return_code
    if signum != signal.SIGKILL:
        signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
    return 128 + signum


def run(command, socket_path):
    certificate, files, manifest_environment, routes = bootstrap(socket_path)
    fallback = inherited_proxy()
    return_code = 1
    with tempfile.TemporaryDirectory(prefix="devc2-scoped-") as raw_root:
        root = Path(raw_root)
        os.chmod(str(root), 0o700)
        ca_path = materialize(root, certificate, files)
        adapter = Adapter(socket_path, routes, fallback)
        child = None
        previous = {}
        pending_signal = [None]

        def forward(signum, _frame):
            if child is None:
                pending_signal[0] = signum
            elif child.poll() is None:
                try:
                    os.killpg(child.pid, signum)
                except ProcessLookupError:
                    pass

        try:
            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
                previous[signum] = signal.signal(signum, forward)
            environment = os.environ.copy()
            environment.update(child_environment(manifest_environment, root, ca_path, adapter.url))
            child = subprocess.Popen(command, env=environment, start_new_session=True)
            if pending_signal[0] is not None:
                try:
                    os.killpg(child.pid, pending_signal[0])
                except ProcessLookupError:
                    pass
            return_code = child.wait()
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            if child is not None and child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait()
            if child is not None:
                terminate_process_group(child.pid)
            adapter.close()
    return preserve_signal_status(return_code)


def main():
    command_name = Path(sys.argv[0]).name
    if not NAME.fullmatch(command_name):
        raise LinkError("invalid projected command name")
    usage = "usage: %s run -- COMMAND [ARG...]" % command_name
    arguments = sys.argv[1:]
    if arguments in (["--help"], ["-h"], ["help"], ["run", "--help"], ["run", "-h"]):
        print(usage)
        return 0
    if len(arguments) < 3 or arguments[:2] != ["run", "--"]:
        print(usage, file=sys.stderr)
        return 2
    socket_dir = os.environ.get("DEVC2_SCOPED_EXEC_SOCKET_DIR", "/run/devc2/spans")
    socket_path = os.path.join(socket_dir, command_name + ".sock")
    return run(arguments[2:], socket_path)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, LinkError, struct.error, subprocess.SubprocessError) as error:
        print("%s: %s" % (Path(sys.argv[0]).name, error), file=sys.stderr)
        raise SystemExit(125)
