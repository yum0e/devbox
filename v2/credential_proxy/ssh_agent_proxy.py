#!/usr/bin/env python3
"""A minimal SSH-agent protocol proxy that exposes exactly one public key.

Only SSH_AGENTC_REQUEST_IDENTITIES and SSH_AGENTC_SIGN_REQUEST are accepted.
The configured public key must also be reported by the upstream agent before it
is exposed by an identities response. Sign requests for every other key are
rejected locally and never reach the host agent.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import logging
import hmac
import os
import secrets
import signal
import socket
import stat
import struct
import threading
from dataclasses import dataclass
from pathlib import Path

SSH_AGENT_FAILURE = 5
SSH2_AGENTC_REQUEST_IDENTITIES = 11
SSH2_AGENT_IDENTITIES_ANSWER = 12
SSH2_AGENTC_SIGN_REQUEST = 13
SSH2_AGENT_SIGN_RESPONSE = 14
DEFAULT_MAX_MESSAGE = 1024 * 1024
LOG = logging.getLogger("ssh-agent-filter")
RELAY_CONTEXT = b"devc2-ssh-relay-v1\x00"

class ProtocolError(Exception):
    pass

def pack_u32(value: int) -> bytes:
    return struct.pack(">I", value)

def pack_string(value: bytes) -> bytes:
    return pack_u32(len(value)) + value

def read_u32(data: bytes, offset: int) -> tuple[int, int]:
    if len(data) - offset < 4:
        raise ProtocolError("truncated uint32")
    return struct.unpack_from(">I", data, offset)[0], offset + 4

def read_string(data: bytes, offset: int) -> tuple[bytes, int]:
    size, offset = read_u32(data, offset)
    end = offset + size
    if end > len(data):
        raise ProtocolError("truncated string")
    return data[offset:end], end

def recv_exact(sock: socket.socket, size: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            if remaining == size:
                return None
            raise ProtocolError("connection closed mid-message")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)

def read_packet(sock: socket.socket, max_message: int = DEFAULT_MAX_MESSAGE) -> bytes | None:
    header = recv_exact(sock, 4)
    if header is None:
        return None
    size = struct.unpack(">I", header)[0]
    if size < 1 or size > max_message:
        raise ProtocolError(f"invalid SSH-agent message length: {size}")
    return recv_exact(sock, size)

def write_packet(sock: socket.socket, payload: bytes) -> None:
    sock.sendall(pack_u32(len(payload)) + payload)

def parse_public_key(line: str) -> tuple[bytes, str]:
    fields = line.strip().split()
    if len(fields) < 2:
        raise ValueError("public key must use OpenSSH '<type> <base64> [comment]' format")
    try:
        blob = base64.b64decode(fields[1], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid base64 in configured public key") from exc
    if not blob:
        raise ValueError("configured public key is empty")
    try:
        algorithm, end = read_string(blob, 0)
        algorithm_name = algorithm.decode("ascii", "strict")
    except (ProtocolError, UnicodeDecodeError) as exc:
        raise ValueError("invalid encoded SSH public key blob") from exc
    if end > len(blob) or algorithm_name != fields[0]:
        raise ValueError("public key type does not match encoded key blob")
    comment = " ".join(fields[2:]) if len(fields) > 2 else "devbox-selected-key"
    return blob, comment

def parse_identities(payload: bytes) -> list[tuple[bytes, bytes]]:
    if not payload or payload[0] != SSH2_AGENT_IDENTITIES_ANSWER:
        raise ProtocolError("upstream returned an invalid identities response")
    count, offset = read_u32(payload, 1)
    # Each identity consumes at least eight bytes, so this also rejects absurd counts.
    if count > len(payload) // 8:
        raise ProtocolError("invalid upstream identity count")
    result = []
    for _ in range(count):
        blob, offset = read_string(payload, offset)
        comment, offset = read_string(payload, offset)
        result.append((blob, comment))
    if offset != len(payload):
        raise ProtocolError("trailing data in upstream identities response")
    return result

def parse_sign_request(payload: bytes) -> tuple[bytes, bytes, int]:
    if not payload or payload[0] != SSH2_AGENTC_SIGN_REQUEST:
        raise ProtocolError("not a sign request")
    key, offset = read_string(payload, 1)
    data, offset = read_string(payload, offset)
    flags, offset = read_u32(payload, offset)
    if offset != len(payload):
        raise ProtocolError("trailing data in sign request")
    return key, data, flags

@dataclass(frozen=True)
class FilterConfig:
    upstream_socket: str
    allowed_key_blob: bytes
    upstream_tcp: tuple[str, int] | None = None
    upstream_secret: bytes | None = None
    allowed_comment: str = "devbox-selected-key"
    max_message: int = DEFAULT_MAX_MESSAGE
    upstream_timeout: float = 30.0

class AgentFilter:
    def __init__(self, config: FilterConfig):
        self.config = config

    def _round_trip(self, request: bytes) -> bytes:
        family = socket.AF_INET if self.config.upstream_tcp else socket.AF_UNIX
        destination = self.config.upstream_tcp or self.config.upstream_socket
        with socket.socket(family, socket.SOCK_STREAM) as upstream:
            upstream.settimeout(self.config.upstream_timeout)
            upstream.connect(destination)
            if self.config.upstream_tcp:
                if not self.config.upstream_secret:
                    raise ProtocolError("TCP relay secret is missing")
                nonce = recv_exact(upstream, 32)
                if nonce is None: raise ProtocolError("TCP relay closed during authentication")
                upstream.sendall(hmac.digest(self.config.upstream_secret, RELAY_CONTEXT + nonce, "sha256"))
            write_packet(upstream, request)
            response = read_packet(upstream, self.config.max_message)
            if response is None:
                raise ProtocolError("upstream agent closed without a response")
            return response

    def handle(self, request: bytes) -> bytes:
        if not request:
            return bytes([SSH_AGENT_FAILURE])
        if request[0] == SSH2_AGENTC_REQUEST_IDENTITIES:
            if len(request) != 1:
                return bytes([SSH_AGENT_FAILURE])
            try:
                identities = parse_identities(self._round_trip(request))
            except (OSError, ProtocolError):
                LOG.warning("upstream identities request failed", exc_info=True)
                return bytes([SSH_AGENT_FAILURE])
            match = next((item for item in identities if item[0] == self.config.allowed_key_blob), None)
            if match is None:
                return bytes([SSH2_AGENT_IDENTITIES_ANSWER]) + pack_u32(0)
            # Use the upstream comment; it contains no authority and improves ssh-add output.
            comment = match[1] or self.config.allowed_comment.encode()
            return (bytes([SSH2_AGENT_IDENTITIES_ANSWER]) + pack_u32(1)
                    + pack_string(self.config.allowed_key_blob) + pack_string(comment))
        if request[0] == SSH2_AGENTC_SIGN_REQUEST:
            try:
                key, _data, _flags = parse_sign_request(request)
            except ProtocolError:
                return bytes([SSH_AGENT_FAILURE])
            if key != self.config.allowed_key_blob:
                LOG.warning("rejected SSH sign request for a non-selected key")
                return bytes([SSH_AGENT_FAILURE])
            try:
                response = self._round_trip(request)
            except (OSError, ProtocolError):
                LOG.warning("upstream sign request failed", exc_info=True)
                return bytes([SSH_AGENT_FAILURE])
            if not response or response[0] not in (SSH2_AGENT_SIGN_RESPONSE, SSH_AGENT_FAILURE):
                LOG.warning("upstream returned an invalid SSH sign response")
                return bytes([SSH_AGENT_FAILURE])
            if response[0] == SSH_AGENT_FAILURE:
                LOG.warning("upstream SSH agent refused the selected key's sign request")
            return response
        LOG.warning("rejected unsupported SSH-agent request type %d", request[0])
        return bytes([SSH_AGENT_FAILURE])

class AgentServer:
    def __init__(self, listen_socket: str, agent_filter: AgentFilter, socket_mode: int = 0o666, drop_uid: int | None = None, drop_gid: int | None = None):
        self.listen_socket = listen_socket
        self.socket_mode = socket_mode
        self.drop_uid = drop_uid
        self.drop_gid = drop_gid
        self.agent_filter = agent_filter
        self._server: socket.socket | None = None
        self._stopping = threading.Event()
        self._threads: set[threading.Thread] = set()
        self._threads_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(64)

    def _prepare_path(self) -> None:
        path = Path(self.listen_socket)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            existing = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(existing.st_mode):
            raise RuntimeError(f"refusing to replace non-socket path: {path}")
        path.unlink()

    def serve_forever(self) -> None:
        self._prepare_path()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server = server
        server.bind(self.listen_socket)
        os.chmod(self.listen_socket, self.socket_mode)
        server.listen(64)
        if self.drop_gid is not None:
            os.setgroups([])
            os.setgid(self.drop_gid)
        if self.drop_uid is not None:
            os.setuid(self.drop_uid)
        if self.drop_uid is not None or self.drop_gid is not None:
            LOG.info("SSH agent filter dropped privileges to uid=%d gid=%d", os.getuid(), os.getgid())
        server.settimeout(0.5)
        LOG.info("SSH agent filter listening on %s", self.listen_socket)
        try:
            while not self._stopping.is_set():
                try:
                    client, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stopping.is_set():
                        break
                    raise
                if not self._slots.acquire(blocking=False):
                    client.close()
                    continue
                client.settimeout(self.agent_filter.config.upstream_timeout)
                thread = threading.Thread(target=self._client_thread, args=(client,), daemon=True)
                with self._threads_lock:
                    self._threads.add(thread)
                thread.start()
        finally:
            server.close()
            try:
                Path(self.listen_socket).unlink()
            except (FileNotFoundError, PermissionError):
                # After the deliberate UID drop the process may no longer be able
                # to unlink its root-created socket; the next root startup removes it.
                pass

    def _client_thread(self, client: socket.socket) -> None:
        current = threading.current_thread()
        try:
            with client:
                while not self._stopping.is_set():
                    try:
                        request = read_packet(client, self.agent_filter.config.max_message)
                    except (OSError, ProtocolError):
                        return
                    if request is None:
                        return
                    write_packet(client, self.agent_filter.handle(request))
        finally:
            with self._threads_lock:
                self._threads.discard(current)
            self._slots.release()

    def shutdown(self) -> None:
        self._stopping.set()
        if self._server is not None:
            self._server.close()

class AuthenticatedTCPRelay:
    """Host-only adapter from an authenticated TCP connection to a Unix agent."""
    def __init__(self, agent_filter: AgentFilter, secret: bytes, host: str = "0.0.0.0", port: int = 0):
        if len(secret) != 32: raise ValueError("relay secret must contain exactly 32 bytes")
        self.agent_filter, self.secret, self.host, self.port = agent_filter, secret, host, port
        self._server = None
        self._stopping = threading.Event()
        self._threads = set()
        self._threads_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(64)

    def serve_forever(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((self.host, self.port)); self.port = server.getsockname()[1]
            self._server = server; server.listen(64); server.settimeout(.5)
            LOG.info("authenticated SSH-agent relay listening on port %d", self.port)
            while not self._stopping.is_set():
                try: client, _ = server.accept()
                except socket.timeout: continue
                except OSError:
                    if self._stopping.is_set(): break
                    raise
                if not self._slots.acquire(blocking=False):
                    client.close(); continue
                thread=threading.Thread(target=self._client_thread,args=(client,),daemon=True)
                with self._threads_lock: self._threads.add(thread)
                thread.start()

    def _client_thread(self, client: socket.socket) -> None:
        current=threading.current_thread()
        try:
            with client:
                client.settimeout(30)
                nonce=secrets.token_bytes(32)
                client.sendall(nonce)
                presented=recv_exact(client,32)
                expected=hmac.digest(self.secret,RELAY_CONTEXT + nonce,"sha256")
                if presented is None or not hmac.compare_digest(presented,expected): return
                request=read_packet(client,self.agent_filter.config.max_message)
                if request is None: return
                write_packet(client,self.agent_filter.handle(request))
        except (OSError,ProtocolError):
            LOG.warning("authenticated host SSH-agent relay request failed",exc_info=True)
        finally:
            with self._threads_lock: self._threads.discard(current)
            self._slots.release()

    def shutdown(self) -> None:
        self._stopping.set()
        if self._server is not None: self._server.close()

def load_allowed_key(path: str) -> tuple[bytes, str]:
    return parse_public_key(Path(path).read_text(encoding="utf-8"))

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default=os.environ.get("SSH_PROXY_SOCKET", "/run/devc2-ssh/agent.sock"))
    parser.add_argument("--relay-listen", help="run a host TCP relay on host:port instead of a Unix filter")
    parser.add_argument("--relay-port-file")
    parser.add_argument("--upstream", default=os.environ.get("SSH_UPSTREAM_SOCKET", "/run/host-services/ssh-auth.sock"))
    parser.add_argument("--upstream-tcp", help="authenticated relay host:port")
    parser.add_argument("--upstream-secret-file")
    parser.add_argument("--allowed-key", default=os.environ.get("SSH_ALLOWED_PUBLIC_KEY_FILE", "/run/devc2-public/ssh-allowed.pub"))
    parser.add_argument("--drop-uid", type=int)
    parser.add_argument("--drop-gid", type=int)
    parser.add_argument("--socket-mode", default=os.environ.get("SSH_PROXY_SOCKET_MODE", "0666"), help="octal mode for the filtered socket")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.relay_listen:
        if not args.upstream_secret_file or not args.relay_port_file: parser.error("host relay requires --upstream-secret-file and --relay-port-file")
        secret = Path(args.upstream_secret_file).read_bytes()
        blob, comment = load_allowed_key(args.allowed_key)
        host, port = args.relay_listen.rsplit(":", 1)
        relay = AuthenticatedTCPRelay(AgentFilter(FilterConfig(upstream_socket=args.upstream, allowed_key_blob=blob, allowed_comment=comment)), secret, host, int(port))
        thread = threading.Thread(target=relay.serve_forever, daemon=True); thread.start()
        deadline = __import__("time").monotonic() + 5
        while relay.port == 0 and thread.is_alive() and __import__("time").monotonic() < deadline: __import__("time").sleep(.01)
        if relay.port == 0: raise RuntimeError("host relay did not start")
        Path(args.relay_port_file).write_text(str(relay.port), encoding="ascii")
        for signum in (signal.SIGINT, signal.SIGTERM): signal.signal(signum, lambda _s, _f: relay.shutdown())
        thread.join(); return 0
    blob, comment = load_allowed_key(args.allowed_key)
    upstream_tcp = None
    secret = None
    if args.upstream_tcp:
        host, port = args.upstream_tcp.rsplit(":", 1)
        upstream_tcp = (host, int(port))
        if not args.upstream_secret_file: parser.error("--upstream-secret-file is required with --upstream-tcp")
        secret = Path(args.upstream_secret_file).read_bytes()
        if len(secret) != 32: parser.error("relay secret file must contain exactly 32 bytes")
    server = AgentServer(args.listen, AgentFilter(FilterConfig(upstream_socket=args.upstream, allowed_key_blob=blob, allowed_comment=comment, upstream_tcp=upstream_tcp, upstream_secret=secret)), int(args.socket_mode, 8), args.drop_uid, args.drop_gid)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _s, _f: server.shutdown())
    server.serve_forever()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
