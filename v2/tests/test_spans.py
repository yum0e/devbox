from __future__ import annotations

import json
import contextlib
import os
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from credential_proxy import span_bridge
from launcher import devc2
from launcher import span_runtime


class SpanDescriptionTests(unittest.TestCase):
    def provider(self, root: Path, name: str = "echo", extra: dict | None = None) -> tuple[Path, Path]:
        client = root / "client"
        client.write_text("#!/bin/sh\nexit 0\n")
        client.chmod(0o755)
        document = {"name": name, "version": "1.0.0", "client": str(client)}
        if extra:
            document.update(extra)
        provider = root / f"{name}-span"
        provider.write_text(f"#!/bin/sh\nprintf '%s' '{json.dumps(document)}'\n")
        provider.chmod(0o755)
        return provider, client

    def test_provider_is_discovered_from_host_path_and_client_is_projected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _provider, client = self.provider(root)
            description = span_runtime.describe("echo", search_path=str(root))
            self.assertEqual(description.client, client)
            destination = root / "projection"
            span_runtime.project_clients([description], destination)
            projected = destination / "echo"
            self.assertEqual(projected.read_bytes(), client.read_bytes())
            self.assertEqual(stat.S_IMODE(projected.stat().st_mode), 0o555)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o555)

    def test_installation_is_not_a_grant_and_description_is_deliberately_tiny(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.provider(root, extra={"methods": ["spawn"]})
            with self.assertRaisesRegex(RuntimeError, "only name, version, and client"):
                span_runtime.describe("echo", search_path=str(root))
            with self.assertRaisesRegex(RuntimeError, "not found"):
                span_runtime.describe("missing", search_path=str(root))

    def test_names_cannot_escape_the_projection(self):
        for name in ("../echo", "Echo", "echo/span", "-echo", "echo-"):
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "invalid Span name"):
                span_runtime.describe(name, search_path="")


class ProviderLifecycleTests(unittest.TestCase):
    def test_provider_receives_one_island_listener_and_is_stopped_with_it(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = root / "client"
            client.write_text("client")
            provider_path = root / "echo-span"
            provider_path.write_text(
                "#!/usr/bin/env python3\n"
                "import os,socket\n"
                "assert os.environ['DEVC2_ISLAND_ID']=='island-one'\n"
                f"assert os.environ['DEVC2_WORKSPACE']=={str(root)!r}\n"
                "server=socket.socket(fileno=int(os.environ['DEVC2_SPAN_SOCKET_FD']))\n"
                "connection,_=server.accept()\n"
                "with connection:\n"
                "    connection.sendall(connection.recv(4096))\n"
            )
            provider_path.chmod(0o755)
            description = span_runtime.SpanDescription("echo", "1", client, provider_path)
            provider = span_runtime.Provider(description, root, "island-one")
            try:
                with socket.create_connection(provider.address, timeout=2) as connection:
                    connection.sendall(b"opaque bytes")
                    self.assertEqual(connection.recv(4096), b"opaque bytes")
                provider.process.wait(timeout=2)
                self.assertEqual(provider.process.returncode, 0)
            finally:
                provider.stop()

    def test_provider_exit_closes_the_listener_and_fails_startup(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = root / "client"
            client.write_text("client")
            provider_path = root / "exit-span"
            provider_path.write_text("#!/bin/sh\nexit 23\n")
            provider_path.chmod(0o755)
            description = span_runtime.SpanDescription("exit", "1", client, provider_path)
            provider = span_runtime.Provider(description, root, "island-one")
            address = provider.address
            try:
                with self.assertRaisesRegex(RuntimeError, "status 23"):
                    provider.check_started()
                with self.assertRaises(OSError):
                    socket.create_connection(address, timeout=0.2)
            finally:
                provider.stop()

    def test_exited_provider_cannot_leave_background_children(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = root / "client"
            client.write_text("client")
            child_pid_file = root / "child.pid"
            provider_path = root / "daemon-span"
            provider_path.write_text(
                "#!/bin/sh\n"
                "sleep 60 &\n"
                f"printf '%s' \"$!\" >{child_pid_file}\n"
                "exit 0\n"
            )
            provider_path.chmod(0o755)
            description = span_runtime.SpanDescription("daemon", "1", client, provider_path)
            provider = span_runtime.Provider(description, root, "island-one")
            child_pid = None
            try:
                with self.assertRaisesRegex(RuntimeError, "status 0"):
                    provider.check_started()
                child_pid = int(child_pid_file.read_text())
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                provider.stop()
                if child_pid is not None:
                    try:
                        os.kill(child_pid, 9)
                    except ProcessLookupError:
                        pass

    @unittest.skipUnless(hasattr(os, "kill"), "requires Unix process signals")
    def test_launcher_death_reaps_the_provider(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = root / "client"
            client.write_text("client")
            pid_file, ready_file = root / "provider.pid", root / "ready"
            provider_path = root / "wait-span"
            provider_path.write_text(
                "#!/bin/sh\n"
                f"printf '%s' \"$$\" >{pid_file}\n"
                "while :; do sleep 1; done\n"
            )
            provider_path.chmod(0o755)
            helper = (
                "import pathlib,time\n"
                "from launcher import span_runtime\n"
                f"root=pathlib.Path({str(root)!r})\n"
                "d=span_runtime.SpanDescription('wait','1',root/'client',root/'wait-span')\n"
                "p=span_runtime.Provider(d,root,'island-crash')\n"
                "p.check_started()\n"
                f"pathlib.Path({str(ready_file)!r}).write_text('ready')\n"
                "time.sleep(60)\n"
            )
            process = subprocess.Popen([sys.executable, "-c", helper], cwd=Path(__file__).parents[2])
            provider_pid = None
            provider_gone = False
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not ready_file.exists():
                    time.sleep(0.05)
                self.assertTrue(ready_file.exists(), "provider helper did not become ready")
                provider_pid = int(pid_file.read_text())
                process.kill()
                process.wait(timeout=2)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        os.kill(provider_pid, 0)
                    except ProcessLookupError:
                        provider_gone = True
                        break
                    time.sleep(0.05)
                else:
                    self.fail("provider survived launcher death")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
                if provider_pid is not None and not provider_gone:
                    try:
                        os.killpg(provider_pid, 9)
                    except ProcessLookupError:
                        pass

    def test_relay_preserves_an_opaque_stream(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = root / "client"
            client.write_text("client")
            provider_path = root / "echo-span"
            provider_path.write_text(
                "#!/usr/bin/env python3\n"
                "import os,socket\n"
                "server=socket.socket(fileno=int(os.environ['DEVC2_SPAN_SOCKET_FD']))\n"
                "connection,_=server.accept()\n"
                "with connection:\n"
                "    while data:=connection.recv(4096): connection.sendall(data)\n"
            )
            provider_path.chmod(0o755)
            server_tls, client_tls = devc2.generate_relay_pki(root / "pki")
            description = span_runtime.SpanDescription("echo", "1", client, provider_path)
            provider = span_runtime.Provider(description, root, "island-one")
            relay = span_runtime.OpaqueRelay(provider.address, server_tls)
            try:
                context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(client_tls / "ca.crt"))
                context.load_cert_chain(client_tls / "client.crt", client_tls / "client.key")
                with socket.create_connection(("127.0.0.1", relay.port), timeout=2) as raw_socket:
                    with context.wrap_socket(raw_socket, server_hostname="host.docker.internal") as connection:
                        payload = bytes(range(256)) * 32
                        connection.sendall(payload)
                        received = bytearray()
                        while len(received) < len(payload):
                            received.extend(connection.recv(4096))
                        self.assertEqual(bytes(received), payload)
            finally:
                relay.stop()
                provider.stop()


class BridgeConfigTests(unittest.TestCase):
    def test_bridge_config_knows_only_names_and_ports(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "spans.json"
            path.write_text('[{"name":"herdr","port":49152}]')
            self.assertEqual(span_bridge.load_config(path), [("herdr", 49152)])
            path.write_text('[{"name":"herdr","port":49152,"methods":["spawn"]}]')
            with self.assertRaisesRegex(RuntimeError, "invalid Span bridge entry"):
                span_bridge.load_config(path)

    def test_readiness_uses_a_per_launch_token(self):
        self.assertTrue(span_bridge.READY_TOKEN.fullmatch("a" * 32))
        self.assertFalse(span_bridge.READY_TOKEN.fullmatch("stale"))
        entrypoint = (Path(__file__).parents[1] / "devbox" / "entrypoint.sh").read_text()
        self.assertIn("cmp -s /run/devc2-public/span-ready /run/devc2/spans/.ready", entrypoint)

    def test_saturated_bridge_rejects_without_killing_accept_loop(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(span_bridge, "MAX_CONNECTIONS", 1):
            root = Path(raw)
            bridge = span_bridge.Bridge("echo", 1, root, "127.0.0.1", mock.Mock())
            self.assertTrue(bridge.slots.acquire(blocking=False))
            bridge.start()
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(1)
                    client.connect(str(root / "echo.sock"))
                    self.assertEqual(client.recv(1), b"")
                self.assertTrue(bridge.thread.is_alive())
            finally:
                bridge.slots.release()
                bridge.stop()

    def test_compose_keeps_transport_credentials_outside_the_island(self):
        compose = (Path(__file__).parents[1] / "compose.yaml").read_text()
        island = compose.split("  devbox:\n", 1)[1].split("\n  credential-proxy:\n", 1)[0]
        bridge = compose.split("  span-proxy:\n", 1)[1].split("\nvolumes:\n", 1)[0]
        self.assertIn("target: /run/devc2/bin", island)
        self.assertIn("target: /run/devc2/spans", island)
        self.assertNotIn("devc2-span-tls", island)
        self.assertIn("devc2-span-tls", bridge)
        self.assertIn("profiles: [spans]", bridge)
        self.assertIn('user: "0:0"', bridge)
        self.assertIn("credential_proxy.span_bridge", bridge)


class LauncherGrantTests(unittest.TestCase):
    def run_launcher(self, root: Path, names: tuple[str, ...]):
        private, public = root / "private", root / "public"
        private.mkdir()
        public.mkdir()
        relay_server, relay_client = root / "relay-server", root / "relay-client"
        relay_server.mkdir()
        relay_client.mkdir()
        process = mock.Mock()
        process.poll.return_value = 0
        calls = []
        runtime = mock.Mock(ports={"herdr": 49153})
        client = root / "herdr-client"
        client.write_text("#!/bin/sh\nexit 0\n")
        client.chmod(0o755)
        description = span_runtime.SpanDescription("herdr", "1", client, root / "herdr-span")
        completed = mock.Mock(returncode=0)
        with mock.patch.object(devc2, "require_docker"), \
             mock.patch.object(devc2, "ensure_v1_not_running"), \
             mock.patch.object(devc2, "ensure_tact_config"), \
             mock.patch.object(devc2, "repository_lock", return_value=contextlib.nullcontext()), \
             mock.patch.object(devc2, "prepare", return_value=(private, public)), \
             mock.patch.object(devc2, "generate_relay_pki", return_value=(relay_server, relay_client)), \
             mock.patch.object(devc2, "start_host_agent_relay", return_value=(process, 49152)), \
             mock.patch.object(devc2, "stop_process"), \
             mock.patch.object(devc2, "compose", side_effect=lambda *args, **kwargs: calls.append(args)), \
             mock.patch.object(devc2.subprocess, "run", return_value=completed), \
             mock.patch.object(devc2.span_runtime, "describe", return_value=description) as describe, \
             mock.patch.object(devc2.span_runtime, "SpanRuntime", return_value=runtime) as start_runtime:
            self.assertEqual(devc2._start_unlocked(root, span_names=names), 0)
        return calls, describe, start_runtime, runtime, public

    def test_no_grant_starts_no_provider_or_span_sidecar(self):
        with tempfile.TemporaryDirectory() as raw:
            calls, describe, start_runtime, runtime, public = self.run_launcher(Path(raw), ())
            describe.assert_not_called()
            start_runtime.assert_not_called()
            runtime.stop.assert_not_called()
            self.assertFalse((public / "spans.json").exists())
            self.assertEqual(calls[0][1].get("COMPOSE_PROFILES"),"spans")
            self.assertNotIn("span-proxy", " ".join(str(part) for call in calls for part in call))

    def test_explicit_grant_starts_and_stops_only_that_span(self):
        with tempfile.TemporaryDirectory() as raw:
            calls, describe, start_runtime, runtime, public = self.run_launcher(Path(raw), ("herdr",))
            describe.assert_called_once_with("herdr")
            start_runtime.assert_called_once()
            runtime.stop.assert_called_once()
            self.assertEqual(json.loads((public / "spans.json").read_text()), [{"name": "herdr", "port": 49153}])
            self.assertIn("span-proxy", " ".join(str(part) for call in calls for part in call))


if __name__ == "__main__":
    unittest.main()
