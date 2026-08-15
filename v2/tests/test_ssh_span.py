import base64
import importlib.machinery
import importlib.util
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parents[1]


def load_script(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


W = load_script(ROOT / "spans" / "ssh-agent" / "world", "ssh_agent_world")
P = W


def key_blob(label: bytes) -> bytes:
    return P.pack_string(b"ssh-ed25519") + label.ljust(32, b"x")[:32]


def public_line(blob: bytes, comment: str = "test") -> str:
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}"


class FakeAgent:
    def __init__(self, path: Path, identities):
        self.path = path
        self.identities = identities
        self.requests = []
        self.stopping = threading.Event()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self.thread.start()
        if not self.ready.wait(2):
            raise RuntimeError("fake agent did not start")
        return self

    def _serve(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(self.path))
            listener.listen()
            listener.settimeout(0.1)
            self.ready.set()
            while not self.stopping.is_set():
                try:
                    connection, _address = listener.accept()
                except socket.timeout:
                    continue
                with connection:
                    request = P.read_packet(connection)
                    if request is None:
                        continue
                    self.requests.append(request)
                    if request == bytes([P.SSH2_AGENTC_REQUEST_IDENTITIES]):
                        response = bytes([P.SSH2_AGENT_IDENTITIES_ANSWER]) + P.pack_u32(len(self.identities))
                        for blob, comment in self.identities:
                            response += P.pack_string(blob) + P.pack_string(comment)
                    elif request[0] == P.SSH2_AGENTC_SIGN_REQUEST:
                        response = bytes([P.SSH2_AGENT_SIGN_RESPONSE]) + P.pack_string(b"signature")
                    else:
                        response = bytes([P.SSH_AGENT_FAILURE])
                    P.write_packet(connection, response)

    def close(self):
        self.stopping.set()
        self.thread.join(2)


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.allowed = key_blob(b"allowed")
        self.other = key_blob(b"other")
        self.upstream = FakeAgent(
            root / "agent.sock",
            [(self.other, b"other"), (self.allowed, b"selected")],
        ).start()
        config = P.FilterConfig(str(self.upstream.path), self.allowed, b"configured")
        self.agent_filter = P.AgentFilter(config)

    def tearDown(self):
        self.upstream.close()
        self.temp.cleanup()

    def test_identities_exposes_only_selected_key(self):
        response = self.agent_filter.handle(bytes([P.SSH2_AGENTC_REQUEST_IDENTITIES]))
        self.assertEqual(P.parse_identities(response), [(self.allowed, b"selected")])

    def test_selected_key_absent_returns_an_empty_list(self):
        self.upstream.identities = [(self.other, b"other")]
        response = self.agent_filter.handle(bytes([P.SSH2_AGENTC_REQUEST_IDENTITIES]))
        self.assertEqual(P.parse_identities(response), [])

    def test_allowed_sign_request_is_forwarded_unchanged(self):
        request = (bytes([P.SSH2_AGENTC_SIGN_REQUEST]) + P.pack_string(self.allowed)
                   + P.pack_string(b"payload") + P.pack_u32(2))
        self.assertEqual(self.agent_filter.handle(request)[0], P.SSH2_AGENT_SIGN_RESPONSE)
        self.assertEqual(self.upstream.requests[-1], request)

    def test_other_key_and_all_other_operations_never_reach_upstream(self):
        before = len(self.upstream.requests)
        request = (bytes([P.SSH2_AGENTC_SIGN_REQUEST]) + P.pack_string(self.other)
                   + P.pack_string(b"payload") + P.pack_u32(0))
        for rejected in (request, bytes([17]), bytes([27]) + P.pack_string(b"session-bind"), b""):
            self.assertEqual(self.agent_filter.handle(rejected), bytes([P.SSH_AGENT_FAILURE]))
        self.assertEqual(len(self.upstream.requests), before)

    def test_malformed_sign_request_is_rejected_locally(self):
        before = len(self.upstream.requests)
        self.assertEqual(
            self.agent_filter.handle(bytes([P.SSH2_AGENTC_SIGN_REQUEST])),
            bytes([P.SSH_AGENT_FAILURE]),
        )
        self.assertEqual(len(self.upstream.requests), before)


class WorldTests(unittest.TestCase):
    def test_world_supports_multiple_packets_on_one_connection_and_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            allowed = key_blob(b"allowed")
            upstream = FakeAgent(root / "agent.sock", [(allowed, b"selected")]).start()
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            provider = P.World(listener, P.AgentFilter(P.FilterConfig(str(upstream.path), allowed, b"selected")))
            thread = threading.Thread(target=provider.serve_forever, daemon=True)
            thread.start()
            try:
                with socket.create_connection(listener.getsockname(), timeout=2) as connection:
                    P.write_packet(connection, bytes([P.SSH2_AGENTC_REQUEST_IDENTITIES]))
                    self.assertEqual(P.parse_identities(P.read_packet(connection)), [(allowed, b"selected")])
                    sign = (bytes([P.SSH2_AGENTC_SIGN_REQUEST]) + P.pack_string(allowed)
                            + P.pack_string(b"data") + P.pack_u32(0))
                    P.write_packet(connection, sign)
                    self.assertEqual(P.read_packet(connection)[0], P.SSH2_AGENT_SIGN_RESPONSE)
                self.assertEqual(upstream.requests, [bytes([P.SSH2_AGENTC_REQUEST_IDENTITIES]), sign])
            finally:
                provider.shutdown()
                thread.join(2)
                upstream.close()
            self.assertFalse(thread.is_alive())
            self.assertFalse(provider.threads)

    def test_message_size_is_bounded(self):
        left, right = socket.socketpair()
        try:
            left.sendall(struct.pack(">I", P.DEFAULT_MAX_MESSAGE + 1))
            with self.assertRaisesRegex(P.ProtocolError, "invalid SSH-agent message length"):
                P.read_packet(right)
        finally:
            left.close()
            right.close()

    def test_configured_world_requires_runtime_socket_agent_and_saved_key(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = root / ".config" / "devc2"
            config.mkdir(parents=True)
            allowed = key_blob(b"allowed")
            (config / "ssh-key.pub").write_text(public_line(allowed))
            agent = FakeAgent(root / "agent.sock", [(allowed, b"selected")]).start()
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            descriptor = os.dup(listener.fileno())
            with mock.patch.dict(os.environ, {
                "HOME": str(root),
                "SSH_AUTH_SOCK": str(agent.path),
                "DEVC2_SPAN_SOCKET_FD": str(descriptor),
            }, clear=True):
                provider = P.configured_world()
            provider.shutdown()
            listener.close()
            agent.close()
            self.assertEqual(provider.agent_filter.config.allowed_key_blob, allowed)

    def test_configured_world_rejects_missing_agent(self):
        with mock.patch.dict(os.environ, {"DEVC2_SPAN_SOCKET_FD": "3"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "missing SSH Span runtime environment"):
                P.configured_world()


class FileContractTests(unittest.TestCase):
    def test_world_is_executable_and_tombstones_are_inert(self):
        world = ROOT / "spans" / "ssh-agent" / "world"
        self.assertTrue(world.stat().st_mode & 0o100)
        self.assertLess(world.stat().st_size, 64 * 1024 * 1024)
        for name in ("provider", "client"):
            path = ROOT / "spans" / "ssh-agent" / name
            self.assertTrue(path.is_file())
            self.assertFalse(path.stat().st_mode & 0o111)


if __name__ == "__main__":
    unittest.main()
