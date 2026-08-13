import base64
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path

from credential_proxy.ssh_agent_proxy import (
    AgentFilter, AgentServer, AuthenticatedTCPRelay, FilterConfig, SSH_AGENT_FAILURE,
    SSH2_AGENTC_REQUEST_IDENTITIES, SSH2_AGENT_IDENTITIES_ANSWER,
    SSH2_AGENTC_SIGN_REQUEST, SSH2_AGENT_SIGN_RESPONSE,
    pack_string, pack_u32, parse_identities, parse_public_key,
    main, read_packet, write_packet,
)

def key_blob(label: bytes) -> bytes:
    return pack_string(b"ssh-ed25519") + label.ljust(32, b"x")[:32]

def public_line(blob: bytes, comment="test") -> str:
    return "ssh-ed25519 " + base64.b64encode(blob).decode() + " " + comment

class FakeAgent:
    def __init__(self, path: str, identities):
        self.path = path
        self.identities = identities
        self.requests = []
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start(); self.ready.wait(2)
        return self

    def run(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.bind(self.path); s.listen(); s.settimeout(.1); self.ready.set()
            while not self.stop.is_set():
                try: conn, _ = s.accept()
                except socket.timeout: continue
                with conn:
                    request = read_packet(conn)
                    if request is None: continue
                    self.requests.append(request)
                    if request[0] == SSH2_AGENTC_REQUEST_IDENTITIES:
                        body = bytes([SSH2_AGENT_IDENTITIES_ANSWER]) + pack_u32(len(self.identities))
                        for blob, comment in self.identities:
                            body += pack_string(blob) + pack_string(comment)
                    elif request[0] == SSH2_AGENTC_SIGN_REQUEST:
                        body = bytes([SSH2_AGENT_SIGN_RESPONSE]) + pack_string(b"fake-signature")
                    else: body = bytes([SSH_AGENT_FAILURE])
                    write_packet(conn, body)

    def close(self):
        self.stop.set(); self.thread.join(2)

class SSHAgentFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); root = Path(self.temp.name)
        self.upstream_path = str(root / "upstream.sock")
        self.allowed = key_blob(b"allowed")
        self.other = key_blob(b"other")
        self.upstream = FakeAgent(self.upstream_path, [(self.other,b"other"),(self.allowed,b"selected")]).start()
        self.filter = AgentFilter(FilterConfig(self.upstream_path, self.allowed))

    def tearDown(self):
        self.upstream.close(); self.temp.cleanup()

    def test_public_key_parser_validates_type(self):
        blob, comment = parse_public_key(public_line(self.allowed, "hello world"))
        self.assertEqual(blob, self.allowed); self.assertEqual(comment, "hello world")
        with self.assertRaises(ValueError):
            parse_public_key(public_line(self.allowed).replace("ssh-ed25519", "ssh-rsa", 1))

    def test_identities_exposes_only_selected_key(self):
        result = self.filter.handle(bytes([SSH2_AGENTC_REQUEST_IDENTITIES]))
        identities = parse_identities(result)
        self.assertEqual(identities, [(self.allowed, b"selected")])

    def test_selected_key_absent_returns_empty(self):
        self.upstream.identities = [(self.other, b"other")]
        result = self.filter.handle(bytes([SSH2_AGENTC_REQUEST_IDENTITIES]))
        self.assertEqual(parse_identities(result), [])

    def test_allowed_sign_request_is_forwarded_unchanged(self):
        request = bytes([SSH2_AGENTC_SIGN_REQUEST]) + pack_string(self.allowed) + pack_string(b"payload") + pack_u32(2)
        result = self.filter.handle(request)
        self.assertEqual(result[0], SSH2_AGENT_SIGN_RESPONSE)
        self.assertEqual(self.upstream.requests[-1], request)

    def test_other_sign_request_is_rejected_without_upstream_access(self):
        before = len(self.upstream.requests)
        request = bytes([SSH2_AGENTC_SIGN_REQUEST]) + pack_string(self.other) + pack_string(b"payload") + pack_u32(0)
        self.assertEqual(self.filter.handle(request), bytes([SSH_AGENT_FAILURE]))
        self.assertEqual(len(self.upstream.requests), before)

    def test_malformed_and_extension_requests_are_rejected(self):
        self.assertEqual(self.filter.handle(bytes([SSH2_AGENTC_SIGN_REQUEST])), bytes([SSH_AGENT_FAILURE]))
        self.assertEqual(self.filter.handle(bytes([27]) + pack_string(b"extension")), bytes([SSH_AGENT_FAILURE]))

    def test_live_server_permissions_and_round_trip(self):
        listen = str(Path(self.temp.name) / "proxy" / "agent.sock")
        server = AgentServer(listen, self.filter)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        deadline = time.time() + 2
        while not Path(listen).exists() and time.time() < deadline: time.sleep(.01)
        self.assertTrue(stat.S_ISSOCK(os.stat(listen).st_mode))
        self.assertEqual(stat.S_IMODE(os.stat(listen).st_mode), 0o666)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(listen); write_packet(client, bytes([SSH2_AGENTC_REQUEST_IDENTITIES]))
            self.assertEqual(parse_identities(read_packet(client)), [(self.allowed,b"selected")])
        server.shutdown(); thread.join(2)

class ProductionConfigTests(unittest.TestCase):
    def test_host_relay_cli_maps_unix_upstream_by_name(self):
        from unittest import mock
        allowed=key_blob(b"allowed")
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); key=root/"key.pub"; key.write_text(public_line(allowed)); secret=root/"secret"; secret.write_bytes(b"s"*32)
            with mock.patch("credential_proxy.ssh_agent_proxy.AuthenticatedTCPRelay") as relay, mock.patch("credential_proxy.ssh_agent_proxy.threading.Thread") as thread:
                relay.return_value.port=49152; thread.return_value.is_alive.return_value=True
                self.assertEqual(main(["--relay-listen","127.0.0.1:0","--relay-port-file",str(root/"port"),"--upstream","/tmp/onepassword.sock","--upstream-secret-file",str(secret),"--allowed-key",str(key)]),0)
            config=relay.call_args.args[0].config
            self.assertEqual(config.upstream_socket,"/tmp/onepassword.sock")
            self.assertIsNone(config.upstream_tcp)
            self.assertEqual(config.allowed_comment,"test")

    def test_tcp_cli_maps_arguments_by_name(self):
        from unittest import mock
        allowed=key_blob(b"allowed")
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); key=root/"key.pub"; key.write_text(public_line(allowed)); secret=root/"secret"; secret.write_bytes(b"s"*32)
            with mock.patch("credential_proxy.ssh_agent_proxy.AgentServer") as server:
                server.return_value.serve_forever.return_value=None
                self.assertEqual(main(["--listen",str(root/"agent.sock"),"--upstream-tcp","host.docker.internal:49152","--upstream-secret-file",str(secret),"--allowed-key",str(key)]),0)
            config=server.call_args.args[1].config
            self.assertEqual(config.upstream_tcp,("host.docker.internal",49152))
            self.assertEqual(config.upstream_secret,b"s"*32)
            self.assertEqual(config.allowed_comment,"test")

    def test_cli_configures_post_bind_privilege_drop(self):
        from unittest import mock
        allowed=key_blob(b"allowed")
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); key=root/"key.pub"; key.write_text(public_line(allowed))
            with mock.patch("credential_proxy.ssh_agent_proxy.AgentServer") as server:
                server.return_value.serve_forever.return_value=None
                self.assertEqual(main(["--listen",str(root/"agent.sock"),"--allowed-key",str(key),"--drop-uid","1000","--drop-gid","1000"]),0)
            self.assertEqual(server.call_args.args[-2:],(1000,1000))

class AuthenticatedRelayTests(unittest.TestCase):
    def test_secret_required_and_request_round_trips(self):
        with tempfile.TemporaryDirectory() as td:
            upstream_path=str(Path(td)/"upstream.sock")
            allowed=key_blob(b"allowed")
            upstream=FakeAgent(upstream_path,[(allowed,b"selected")]).start()
            relay=AuthenticatedTCPRelay(AgentFilter(FilterConfig(upstream_path,allowed)),b"s"*32,"127.0.0.1",0)
            thread=threading.Thread(target=relay.serve_forever,daemon=True); thread.start()
            deadline=time.time()+2
            while relay.port==0 and time.time()<deadline: time.sleep(.01)
            request=bytes([SSH2_AGENTC_REQUEST_IDENTITIES])
            filtered=AgentFilter(FilterConfig("",allowed,upstream_tcp=("127.0.0.1",relay.port),upstream_secret=b"s"*32))
            self.assertEqual(parse_identities(filtered.handle(request)),[(allowed,b"selected")])
            with socket.create_connection(("127.0.0.1",relay.port),timeout=2) as client:
                self.assertEqual(len(client.recv(32)),32)
                client.sendall(b"x"*32); write_packet(client,request)
                with self.assertRaises((ConnectionResetError, BrokenPipeError, socket.timeout)):
                    read_packet(client)
            relay.shutdown(); thread.join(2); upstream.close()

if __name__ == "__main__": unittest.main()
