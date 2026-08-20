from __future__ import annotations

import base64
import json
import contextlib
import os
import socket
import ssl
import stat
import struct
import subprocess
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from credential_proxy import http_projection, span_bridge, stream_relay
from launcher import devc2
from launcher import span_runtime


V2_ROOT = Path(__file__).parents[1]
COMMAND_PROJECTION = V2_ROOT / "launcher" / "command_projection.py"


class BrokenSendSocket:
    def __init__(self, connection, failed):
        self.connection, self.failed = connection, failed

    def fileno(self):
        return self.connection.fileno()

    def setblocking(self, value):
        self.connection.setblocking(value)

    def recv(self, size):
        return self.connection.recv(size)

    def send(self, _payload):
        self.failed.set()
        raise BrokenPipeError()

    def shutdown(self, how):
        self.connection.shutdown(how)

    def pending(self):
        return 0

    def close(self):
        self.connection.close()


class SpanCatalogTests(unittest.TestCase):
    def load(self,catalog: Path,names: tuple[str,...],workspace: Path | None = None):
        snapshot=Path(tempfile.mkdtemp(prefix="span-snapshot-"))
        self.addCleanup(shutil.rmtree,snapshot,True)
        forbidden=workspace or catalog.parent/"unrelated-workspace"
        return span_runtime.load_catalog(
            catalog,names,forbidden,snapshot/"clients",snapshot/"providers",
            command_projection=COMMAND_PROJECTION,
        )

    def world(self, root: Path) -> Path:
        world = root / "world"
        world.write_text("#!/bin/sh\nexit 0\n")
        world.chmod(0o755)
        return world

    def catalog(self, root: Path, entries: dict, mode: int = 0o600) -> Path:
        path=root/"spans.json"
        path.write_text(json.dumps({"spans":entries}))
        path.chmod(mode)
        return path

    def test_catalog_resolves_exact_world_without_executing_it(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            world = self.world(root)
            sentinel=root/"world-ran"
            world.write_text(f"#!/bin/sh\nprintf ran >{sentinel}\n")
            catalog=self.catalog(root,{"echo":str(world)})
            span = self.load(catalog,("echo",))[0]
            self.assertEqual(span.name,"echo")
            self.assertEqual(span.client.read_bytes(),COMMAND_PROJECTION.read_bytes())
            self.assertEqual(span.provider.read_bytes(),world.read_bytes())
            self.assertFalse(sentinel.exists())
            snapshotted_world=span.provider.read_bytes()
            world.write_text("#!/bin/sh\nexit 99\n")
            self.assertEqual(span.provider.read_bytes(),snapshotted_world)
            self.assertEqual(stat.S_IMODE(span.client.stat().st_mode),0o555)
            self.assertEqual(stat.S_IMODE(span.client.parent.stat().st_mode),0o555)

    def test_no_grant_does_not_require_or_read_a_catalog(self):
        with tempfile.TemporaryDirectory() as raw:
            self.assertEqual(self.load(Path(raw)/"missing",()),[])

    def test_catalog_is_exact_and_host_owned(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); world=self.world(root)
            catalog=root/"spans.json"
            catalog.write_text(json.dumps({"spans":{"echo":str(world)},"version":1}))
            catalog.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError,"only a spans object"):
                self.load(catalog,("echo",))
            catalog=self.catalog(root,{"echo":{"world":str(world)}})
            with self.assertRaisesRegex(RuntimeError,"invalid World path"):
                self.load(catalog,("echo",))
            catalog.chmod(0o622)
            with self.assertRaisesRegex(RuntimeError,"not group/world-writable"):
                self.load(catalog,("echo",))
            catalog.unlink(); os.symlink(world,catalog)
            with self.assertRaisesRegex(RuntimeError,"unreadable"):
                self.load(catalog,("echo",))

    def test_missing_catalog_entry_or_relative_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); world=self.world(root)
            catalog=self.catalog(root,{"echo":"relative"})
            with self.assertRaisesRegex(RuntimeError,"World must be an absolute path"):
                self.load(catalog,("echo",))
            catalog=self.catalog(root,{"echo":str(world)})
            with self.assertRaisesRegex(RuntimeError,"unavailable"):
                self.load(catalog,("missing",))
            world.chmod(0o777)
            with self.assertRaisesRegex(RuntimeError,"not group/world-writable"):
                self.load(catalog,("echo",))

    def test_catalog_and_executables_must_live_outside_the_workspace(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); workspace=root/"workspace"; outside=root/"outside"
            workspace.mkdir(); outside.mkdir()
            world=self.world(outside)
            catalog=self.catalog(workspace,{"echo":str(world)})
            with self.assertRaisesRegex(RuntimeError,"catalog must live outside"):
                self.load(catalog,("echo",),workspace)
            workspace_world=workspace/"world"
            workspace_world.write_text("#!/bin/sh\n"); workspace_world.chmod(0o755)
            catalog=self.catalog(outside,{"echo":str(workspace_world)})
            with self.assertRaisesRegex(RuntimeError,"World must live outside"):
                self.load(catalog,("echo",),workspace)
            catalog=self.catalog(outside,{"echo":str(world)})
            os.link(world,workspace/"world-alias")
            with self.assertRaisesRegex(RuntimeError,"must not have hard links"):
                self.load(catalog,("echo",),workspace)

    def test_names_cannot_escape_the_projection(self):
        with tempfile.TemporaryDirectory() as raw:
            catalog=self.catalog(Path(raw),{})
            for name in ("../echo", "Echo", "echo/span", "-echo", "echo-"):
                with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "invalid Span name"):
                    self.load(catalog,(name,))

    def test_builtin_span_needs_no_external_catalog_and_cannot_be_shadowed(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); builtin=root/"builtins"; builtin.mkdir(parents=True)
            world=builtin/"http-credential-world"; world.write_text("#!/bin/sh\nexit 0\n"); world.chmod(0o555)
            destination=root/"snapshot"; destination.mkdir()
            spans=span_runtime.load_catalog(
                root/"missing.json",("openai",),root/"workspace",destination/"clients",destination/"worlds",
                root/"builtins",
            )
            self.assertEqual([(item.name,item.provider.read_bytes()) for item in spans],[('openai',world.read_bytes())])
            self.assertIsNone(spans[0].client)
            self.assertTrue(spans[0].http_attachment)
            self.assertEqual(stat.S_IMODE(spans[0].provider.stat().st_mode),0o555)
            self.assertEqual(list((destination/"clients").iterdir()),[])

    def test_http_worlds_are_lifetime_attachments_without_commands(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); destination=root/"snapshot"; destination.mkdir()
            items=span_runtime.load_catalog(
                root/"missing.json",("github","openai"),root/"workspace",destination/"links",destination/"worlds",
                V2_ROOT/"spans",
            )
            for item in items:
                self.assertEqual(item.provider.name, item.name)
                self.assertEqual(
                    item.provider.read_bytes(),
                    (V2_ROOT/"spans"/"http-credential-world").read_bytes(),
                )
                self.assertIsNone(item.client)
                self.assertTrue(item.http_attachment)
            self.assertEqual(list((destination/"links").iterdir()),[])

    def test_ssh_world_uses_only_the_generic_stream_link(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); destination=root/"snapshot"; destination.mkdir()
            item=span_runtime.load_catalog(
                root/"missing.json",("ssh-agent",),root/"workspace",
                destination/"links",destination/"worlds",V2_ROOT/"spans",
            )[0]
            self.assertEqual(
                item.provider.read_bytes(),
                (V2_ROOT/"spans"/"ssh-agent"/"world").read_bytes(),
            )
            self.assertIsNone(item.client)
            self.assertEqual(list((destination/"links").iterdir()), [])

    def test_command_link_snapshots_one_world_and_the_generic_projection(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); world=root/"tool-world"
            world.write_text("#!/bin/sh\nexit 0\n"); world.chmod(0o755)
            catalog=self.catalog(root,{"tool":str(world)})
            destination=root/"snapshot"; destination.mkdir()
            item=span_runtime.load_catalog(
                catalog,("tool",),root/"workspace",destination/"clients",destination/"worlds",
                command_projection=COMMAND_PROJECTION,
            )[0]
            self.assertEqual(item.provider.read_bytes(),world.read_bytes())
            self.assertEqual(item.client.read_bytes(),COMMAND_PROJECTION.read_bytes())
            self.assertEqual(item.client.name,"tool")
            self.assertNotIn(b"tool",COMMAND_PROJECTION.read_bytes().lower())

    def test_structured_world_entry_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); world=root/"world"
            world.write_text("#!/bin/sh\n"); world.chmod(0o755)
            catalog=self.catalog(root,{"x":{"world":str(world)}})
            with self.assertRaisesRegex(RuntimeError,"invalid World path"):
                self.load(catalog,("x",))

    def test_ungranted_stale_entry_has_no_authority(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); world=self.world(root)
            catalog=self.catalog(root,{
                "echo":str(world),
                "stale":{"provider":"/old/provider","client":"/old/client"},
            })
            self.assertEqual(self.load(catalog,("echo",))[0].name,"echo")
            with self.assertRaisesRegex(RuntimeError,"invalid World path"):
                self.load(catalog,("stale",))


class CommandProjectionTests(unittest.TestCase):
    def test_generic_projection_preserves_argv_stdin_outputs_and_status(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); socket_dir=root/"sockets"; socket_dir.mkdir()
            projected=root/"tool"; projected.symlink_to(COMMAND_PROJECTION)
            server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            server.bind(str(socket_dir/"tool.sock")); server.listen()
            observed={}

            def serve():
                connection,_=server.accept()
                with connection:
                    size=struct.unpack("!I",connection.recv(4))[0]
                    payload=bytearray()
                    while len(payload)<size: payload.extend(connection.recv(size-len(payload)))
                    observed.update(json.loads(payload))
                    response=json.dumps({
                        "ok":True,
                        "stdout_b64":base64.b64encode(b"output\n").decode(),
                        "stderr_b64":base64.b64encode(b"warning\n").decode(),
                        "exit_code":23,
                    },separators=(",",":")).encode()
                    connection.sendall(struct.pack("!I",len(response))+response)

            thread=threading.Thread(target=serve,daemon=True); thread.start()
            try:
                completed=subprocess.run(
                    [str(projected),"wait","worker"],input=b"input",capture_output=True,
                    env={**os.environ,"DEVC2_COMMAND_SOCKET_DIR":str(socket_dir)},check=False,
                )
            finally:
                thread.join(timeout=2); server.close()
            self.assertEqual(completed.returncode,23,completed.stderr)
            self.assertEqual(completed.stdout,b"output\n")
            self.assertEqual(completed.stderr,b"warning\n")
            self.assertEqual(observed,{"v":1,"command":"tool","argv":["wait","worker"],"stdin_b64":"aW5wdXQ="})


class ProviderLifecycleTests(unittest.TestCase):
    def test_provider_receives_one_island_listener_and_is_stopped_with_it(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = root / "client"
            client.write_text("client")
            provider_path = root / "echo-span"
            provider_path.write_text(
                "#!/usr/bin/env python3\n"
                "import os,socket,sys\n"
                "assert sys.argv==[sys.argv[0]]\n"
                "assert os.environ['DEVC2_ISLAND_ID']=='island-one'\n"
                f"assert os.environ['DEVC2_WORKSPACE']=={str(root)!r}\n"
                "server=socket.socket(fileno=int(os.environ['DEVC2_SPAN_SOCKET_FD']))\n"
                "connection,_=server.accept()\n"
                "with connection:\n"
                "    connection.sendall(connection.recv(4096))\n"
            )
            provider_path.chmod(0o755)
            span = span_runtime.Span("echo", client, provider_path)
            provider = span_runtime.Provider(span, root, "island-one")
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
            span = span_runtime.Span("exit", client, provider_path)
            provider = span_runtime.Provider(span, root, "island-one")
            address = provider.address
            try:
                with self.assertRaisesRegex(RuntimeError, "status 23"):
                    provider.check_started()
                with self.assertRaises(OSError):
                    socket.create_connection(address, timeout=0.2)
            finally:
                provider.stop()

    def test_start_check_is_structural_not_semantic_readiness(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = root / "client"
            client.write_text("client")
            provider_path = root / "delayed-exit-span"
            provider_path.write_text("#!/bin/sh\nsleep 0.25\nexit 23\n")
            provider_path.chmod(0o755)
            span = span_runtime.Span("delayed", client, provider_path)
            provider = span_runtime.Provider(span, root, "island-one")
            try:
                provider.check_started()
                self.assertEqual(provider.process.wait(timeout=2), 23)
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
            span = span_runtime.Span("daemon", client, provider_path)
            provider = span_runtime.Provider(span, root, "island-one")
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
                "d=span_runtime.Span('wait',root/'client',root/'wait-span')\n"
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
            span = span_runtime.Span("echo", client, provider_path)
            provider = span_runtime.Provider(span, root, "island-one")
            relay = span_runtime.OpaqueRelay(provider.address, server_tls)
            try:
                context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(client_tls / "ca.crt"))
                context.load_cert_chain(client_tls / "client.crt", client_tls / "client.key")
                with mock.patch.object(
                    span_runtime,
                    "enable_tcp_keepalive",
                    wraps=span_runtime.enable_tcp_keepalive,
                ) as keepalive:
                    with socket.create_connection(("127.0.0.1", relay.port), timeout=2) as raw_socket:
                        with context.wrap_socket(raw_socket, server_hostname="host.docker.internal") as connection:
                            payload = bytes(range(256)) * 32
                            connection.sendall(payload)
                            received = bytearray()
                            while len(received) < len(payload):
                                received.extend(connection.recv(4096))
                            self.assertEqual(bytes(received), payload)
                    self.assertTrue(keepalive.called)
            finally:
                relay.stop()
                provider.stop()

    def test_duplex_relay_propagates_delayed_half_closes(self):
        island, relay_left = socket.socketpair()
        relay_right, world = socket.socketpair()
        stopping = threading.Event()
        thread = threading.Thread(
            target=span_bridge.duplex_stream,
            args=(relay_left, relay_right, stopping),
            daemon=True,
        )
        thread.start()
        try:
            island.sendall(b"request")
            self.assertEqual(world.recv(7), b"request")
            island.shutdown(socket.SHUT_WR)
            world.settimeout(2)
            self.assertEqual(world.recv(1), b"")
            world.sendall(b"response")
            world.shutdown(socket.SHUT_WR)
            island.settimeout(2)
            self.assertEqual(island.recv(8), b"response")
            self.assertEqual(island.recv(1), b"")
            thread.join(2)
            self.assertFalse(thread.is_alive())
        finally:
            stopping.set()
            for connection in (island, relay_left, relay_right, world):
                connection.close()
            thread.join(2)

    def test_duplex_relay_treats_peer_cancellation_as_stream_completion(self):
        island, relay_left = socket.socketpair()
        relay_right, world = socket.socketpair()
        stopping = threading.Event()
        errors = []

        def relay():
            try:
                span_bridge.duplex_stream(relay_left, relay_right, stopping)
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=relay, daemon=True)
        thread.start()
        try:
            island.close()
            world.sendall(b"response after client cancellation")
            world.shutdown(socket.SHUT_WR)
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
        finally:
            stopping.set()
            for connection in (relay_left, relay_right, world):
                connection.close()
            thread.join(2)

    def test_duplex_relay_preserves_reverse_stream_after_broken_write(self):
        island, raw_relay_left = socket.socketpair()
        relay_right, world = socket.socketpair()
        stopping = threading.Event()
        write_failed = threading.Event()
        errors = []

        relay_left = BrokenSendSocket(raw_relay_left, write_failed)

        def relay():
            try:
                span_bridge.duplex_stream(relay_left, relay_right, stopping)
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=relay, daemon=True)
        thread.start()
        try:
            world.sendall(b"undeliverable")
            self.assertTrue(write_failed.wait(2))
            island.sendall(b"reverse survives")
            world.settimeout(2)
            self.assertEqual(world.recv(16), b"reverse survives")
            island.shutdown(socket.SHUT_WR)
            world.shutdown(socket.SHUT_WR)
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
        finally:
            stopping.set()
            for connection in (island, relay_left, relay_right, world):
                connection.close()
            thread.join(2)

    def test_duplex_relay_fully_retires_a_failed_tls_stream(self):
        island, raw_relay_left = socket.socketpair()
        relay_right, world = socket.socketpair()
        stopping = threading.Event()
        write_failed = threading.Event()
        errors = []
        relay_left = BrokenSendSocket(raw_relay_left, write_failed)

        def relay():
            try:
                with mock.patch.object(span_bridge.ssl, "SSLSocket", BrokenSendSocket):
                    span_bridge.duplex_stream(relay_left, relay_right, stopping)
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=relay, daemon=True)
        thread.start()
        try:
            world.sendall(b"undeliverable TLS bytes")
            self.assertTrue(write_failed.wait(2))
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
        finally:
            stopping.set()
            for connection in (island, relay_left, relay_right, world):
                connection.close()
            thread.join(2)


class BridgeConfigTests(unittest.TestCase):
    def test_transport_gap_detects_host_sleep_even_when_vm_boot_clock_pauses(self):
        self.assertEqual(stream_relay.transport_gap((100,1000),(101,1020)),20)
        self.assertEqual(stream_relay.transport_gap((100,1000),(120,1001)),20)
        self.assertEqual(stream_relay.transport_gap((100,1000),(101,900)),float("inf"))
        self.assertEqual(stream_relay.transport_gap((100,1000),(101,float("nan"))),float("inf"))

    def test_duplex_relay_drops_first_post_resume_bytes_before_forwarding(self):
        island,relay_left=socket.socketpair(); relay_right,world=socket.socketpair()
        try:
            island.sendall(b"must not be replayed")
            with mock.patch.object(
                stream_relay,"transport_clocks",side_effect=[(100,1000),(101,1020)],
            ):
                stream_relay.duplex_stream(relay_left,relay_right,threading.Event())
            world.setblocking(False)
            with self.assertRaises(BlockingIOError): world.recv(1)
        finally:
            for connection in (island,relay_left,relay_right,world): connection.close()

    def test_bridge_retires_existing_streams_after_a_suspend_gap(self):
        with tempfile.TemporaryDirectory() as raw:
            bridge = span_bridge.Bridge("openai", 1, Path(raw), "127.0.0.1", mock.Mock())
            bridge.last_observed_time = (100,1000)
            bridge_side, island_side = socket.socketpair()
            with bridge.connections_lock:
                bridge.connections.add(bridge_side)
            try:
                self.assertFalse(
                    bridge._retire_after_resume((100 + span_bridge.RESUME_GAP_SECONDS,1001))
                )
                island_side.sendall(b"still connected")
                self.assertEqual(bridge_side.recv(15), b"still connected")

                with mock.patch("builtins.print") as output:
                    self.assertTrue(
                        bridge._retire_after_resume((
                            100 + span_bridge.RESUME_GAP_SECONDS,
                            1000 + 2 * span_bridge.RESUME_GAP_SECONDS + 1,
                        ))
                    )
                output.assert_called_once_with(
                    "span-bridge: Span 'openai' retired pre-resume transport",flush=True,
                )
                island_side.settimeout(1)
                self.assertEqual(island_side.recv(1), b"")
            finally:
                bridge_side.close()
                island_side.close()
                bridge.server.close()
                bridge.path.unlink(missing_ok=True)

    def test_bridge_poll_retires_stale_streams_after_resume(self):
        with tempfile.TemporaryDirectory() as raw:
            bridge = span_bridge.Bridge("openai", 1, Path(raw), "127.0.0.1", mock.Mock())
            bridge_side, island_side = socket.socketpair()
            with bridge.connections_lock:
                bridge.connections.add(bridge_side)
            observed=span_bridge.transport_clocks()
            bridge.last_observed_time=(observed[0],observed[1]-span_bridge.RESUME_GAP_SECONDS-1)
            bridge.start()
            try:
                island_side.settimeout(2)
                self.assertEqual(island_side.recv(1), b"")
            finally:
                bridge.stop()
                bridge_side.close()
                island_side.close()

    def test_bridge_tracks_tcp_during_tls_handshake_and_enables_keepalive(self):
        with tempfile.TemporaryDirectory() as raw:
            bridge = span_bridge.Bridge("openai", 1, Path(raw), "127.0.0.1", mock.Mock())
            client = mock.MagicMock(spec=socket.socket)
            raw_connection = mock.MagicMock(spec=socket.socket)
            raw_context = mock.MagicMock()
            raw_context.__enter__.return_value = raw_connection
            remote = mock.MagicMock(spec=ssl.SSLSocket)
            remote_context = mock.MagicMock()
            remote_context.__enter__.return_value = remote

            def wrap_socket(connection, server_hostname, do_handshake_on_connect):
                self.assertIs(connection, raw_connection)
                self.assertEqual(server_hostname, "127.0.0.1")
                self.assertFalse(do_handshake_on_connect)
                with bridge.connections_lock:
                    self.assertIn(raw_connection, bridge.connections)
                return remote_context

            def handshake():
                with bridge.connections_lock:
                    self.assertIn(remote,bridge.connections)

            bridge.context.wrap_socket.side_effect = wrap_socket
            remote.do_handshake.side_effect=handshake
            self.assertTrue(bridge.slots.acquire(blocking=False))
            try:
                with mock.patch.object(
                    span_bridge.socket,
                    "create_connection",
                    return_value=raw_context,
                ), mock.patch.object(
                    span_bridge, "enable_tcp_keepalive"
                ) as keepalive, mock.patch.object(span_bridge, "duplex_stream"):
                    bridge._connection(client)
                keepalive.assert_called_once_with(raw_connection)
                remote.do_handshake.assert_called_once_with()
            finally:
                bridge.server.close()
                bridge.path.unlink(missing_ok=True)

    def test_resume_retirement_can_interrupt_an_in_progress_tls_handshake(self):
        with tempfile.TemporaryDirectory() as raw:
            bridge=span_bridge.Bridge("openai",1,Path(raw),"127.0.0.1",mock.Mock())
            client=mock.MagicMock(spec=socket.socket); raw_connection=mock.MagicMock(spec=socket.socket)
            raw_context=mock.MagicMock(); raw_context.__enter__.return_value=raw_connection
            remote=mock.MagicMock(spec=ssl.SSLSocket); remote_context=mock.MagicMock(); remote_context.__enter__.return_value=remote
            bridge.context.wrap_socket.return_value=remote_context
            def interrupted():
                self.assertGreaterEqual(bridge._retire_connections(),1)
                raise ssl.SSLError("retired")
            remote.do_handshake.side_effect=interrupted
            self.assertTrue(bridge.slots.acquire(blocking=False))
            try:
                with mock.patch.object(span_bridge.socket,"create_connection",return_value=raw_context), \
                     mock.patch.object(span_bridge,"duplex_stream") as relay, \
                     mock.patch.object(bridge,"_report_failure") as failure:
                    bridge._connection(client)
                remote.close.assert_called()
                relay.assert_not_called()
                failure.assert_not_called()
                self.assertTrue(bridge.slots.acquire(blocking=False))
            finally:
                bridge.slots.release()
                bridge.server.close(); bridge.path.unlink(missing_ok=True)

    def test_tcp_keepalive_is_enabled_for_relay_connections(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client = socket.create_connection(listener.getsockname(), timeout=1)
        server, _address = listener.accept()
        try:
            span_bridge.enable_tcp_keepalive(client)
            self.assertEqual(
                client.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE), 1
            )
        finally:
            client.close()
            server.close()
            listener.close()

    def test_repeated_bridge_failure_emits_one_bounded_diagnostic(self):
        with tempfile.TemporaryDirectory() as raw:
            bridge = span_bridge.Bridge("ssh-agent", 1, Path(raw), "127.0.0.1", mock.Mock())
            try:
                with mock.patch("builtins.print") as output:
                    for _attempt in range(10):
                        bridge._report_failure("host connection", ConnectionRefusedError())
                output.assert_called_once_with(
                    "span-bridge: Span 'ssh-agent' failed during host connection (ConnectionRefusedError)",
                    flush=True,
                )
            finally:
                bridge.server.close()
                bridge.path.unlink(missing_ok=True)

    def test_host_relay_retries_world_connection_before_forwarding(self):
        relay = object.__new__(span_runtime.OpaqueRelay)
        relay.upstream = ("127.0.0.1", 1234)
        relay.name = "openai"
        relay.stopping = threading.Event()
        relay.diagnostics = mock.Mock()
        connected = mock.MagicMock(spec=socket.socket)
        with mock.patch.object(
            span_runtime.socket, "create_connection",
            side_effect=[ConnectionRefusedError(), connected],
        ) as connect, mock.patch.object(relay.stopping, "wait", return_value=False) as wait:
            self.assertIs(relay._connect_world(), connected)
        self.assertEqual(connect.call_count, 2)
        wait.assert_called_once_with(span_runtime.WORLD_CONNECT_BACKOFF[0])
        relay.diagnostics.recovery_started.assert_called_once_with("openai")
        relay.diagnostics.recovery_succeeded.assert_called_once_with("openai")
        relay.diagnostics.recovery_failed.assert_not_called()

    def test_host_relay_stops_after_bounded_world_connection_retries(self):
        relay = object.__new__(span_runtime.OpaqueRelay)
        relay.upstream = ("127.0.0.1", 1234)
        relay.name = "github"
        relay.stopping = threading.Event()
        relay.diagnostics = mock.Mock()
        failure = ConnectionRefusedError()
        with mock.patch.object(
            span_runtime.socket, "create_connection", side_effect=failure,
        ) as connect, mock.patch.object(relay.stopping, "wait", return_value=False) as wait, \
             self.assertRaises(ConnectionRefusedError):
            relay._connect_world()
        self.assertEqual(connect.call_count, len(span_runtime.WORLD_CONNECT_BACKOFF) + 1)
        self.assertEqual(
            [call.args[0] for call in wait.call_args_list],
            list(span_runtime.WORLD_CONNECT_BACKOFF),
        )
        relay.diagnostics.recovery_started.assert_called_once_with("github")
        relay.diagnostics.recovery_succeeded.assert_not_called()
        relay.diagnostics.recovery_failed.assert_called_once_with("github")

    def test_bridge_config_knows_only_names_and_ports(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "spans.json"
            path.write_text('[{"name":"tool","port":49152}]')
            self.assertEqual(span_bridge.load_config(path), [("tool", 49152)])
            path.write_text('[{"name":"tool","port":49152,"methods":["spawn"]}]')
            with self.assertRaisesRegex(RuntimeError, "invalid Span bridge entry"):
                span_bridge.load_config(path)

    def test_readiness_uses_a_per_launch_token(self):
        self.assertTrue(span_bridge.READY_TOKEN.fullmatch("a" * 32))
        self.assertFalse(span_bridge.READY_TOKEN.fullmatch("stale"))
        entrypoint = (Path(__file__).parents[1] / "devbox" / "entrypoint.sh").read_text()
        self.assertIn("cmp -s /run/devc2-public/span-ready /run/devc2/spans/.ready", entrypoint)

    def test_saturated_bridge_backpressures_until_capacity_is_available(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(span_bridge, "MAX_CONNECTIONS", 1):
            root = Path(raw)
            bridge = span_bridge.Bridge("echo", 1, root, "127.0.0.1", mock.Mock())
            self.assertTrue(bridge.slots.acquire(blocking=False))
            bridge.start()
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(0.1)
                    client.connect(str(root / "echo.sock"))
                    with self.assertRaises(TimeoutError):
                        client.recv(1)
                    bridge.slots.release()
                    client.settimeout(2)
                    self.assertEqual(client.recv(1), b"")
                self.assertTrue(bridge.thread.is_alive())
            finally:
                bridge.stop()

    def test_fatal_bridge_listener_failure_stops_the_sidecar(self):
        with tempfile.TemporaryDirectory() as raw:
            stopping = threading.Event()
            failed = threading.Event()
            bridge = span_bridge.Bridge(
                "echo", 1, Path(raw), "127.0.0.1", mock.Mock(),
                stopping=stopping, failed=failed,
            )
            bridge.start()
            bridge.server.close()
            try:
                self.assertTrue(stopping.wait(2))
                self.assertTrue(failed.is_set())
                bridge.thread.join(2)
                self.assertFalse(bridge.thread.is_alive())
            finally:
                bridge.stop()

    def test_compose_keeps_transport_credentials_outside_the_island(self):
        compose = (Path(__file__).parents[1] / "compose.yaml").read_text()
        island = compose.split("  devbox:\n", 1)[1].split("\n  span-proxy:\n", 1)[0]
        bridge = compose.split("  span-proxy:\n", 1)[1].split("\nvolumes:\n", 1)[0]
        self.assertIn("target: /run/devc2/bin", island)
        self.assertIn("target: /run/devc2/spans", island)
        self.assertNotIn("devc2-span-tls", island)
        self.assertIn("devc2-span-tls", bridge)
        self.assertIn("profiles: [spans]", bridge)
        self.assertIn('user: "0:0"', bridge)
        self.assertIn("credential_proxy.span_bridge", bridge)
        self.assertIn('restart: "on-failure:5"',bridge)
        self.assertIn("mem_limit: 1g", bridge)
        self.assertIn("cpus: 2", bridge)
        self.assertIn("pids_limit: 1024", bridge)
        self.assertEqual(span_bridge.MAX_CONNECTIONS, 256)
        self.assertEqual(span_bridge.LISTEN_BACKLOG, 1024)
        self.assertEqual(http_projection.MAX_CONNECTIONS, 256)
        self.assertEqual(http_projection.LISTEN_BACKLOG, 1024)
        span_slots, http_slots = span_bridge.worker_budgets()
        self.assertIsNot(span_slots, http_slots)
        for _connection in range(256):
            self.assertTrue(span_slots.acquire(blocking=False))
            self.assertTrue(http_slots.acquire(blocking=False))
        self.assertFalse(span_slots.acquire(blocking=False))
        self.assertFalse(http_slots.acquire(blocking=False))
        self.assertLessEqual(
            2 * (span_bridge.MAX_CONNECTIONS + http_projection.MAX_CONNECTIONS)
            * stream_relay.MAX_PENDING,
            256 * 1024 * 1024,
        )

    def test_fatal_listener_before_readiness_returns_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = root / "spans.json"
            routes = root / "routes.json"
            token = root / "token"
            sockets = root / "sockets"
            config.write_text('[{"name":"echo","port":1234}]')
            routes.write_text('{"v":1,"routes":{}}')
            token.write_text("a" * 32)
            context = mock.Mock(options=0)
            bridge = mock.Mock()

            def make_bridge(*args):
                stopping, failed = args[-2:]
                bridge.start.side_effect = lambda: (failed.set(), stopping.set())
                return bridge

            http = mock.Mock()
            argv = [
                "--config", str(config), "--socket-dir", str(sockets),
                "--tls-ca", "ca", "--tls-cert", "cert", "--tls-key", "key",
                "--ready-token-file", str(token), "--http-config", str(routes),
            ]
            with mock.patch.object(span_bridge.ssl, "create_default_context", return_value=context), \
                 mock.patch.object(span_bridge, "Bridge", side_effect=make_bridge), \
                 mock.patch.object(span_bridge, "HttpProjection", return_value=http):
                self.assertEqual(span_bridge.main(argv), 1)
            self.assertFalse((sockets / ".ready").exists())
            bridge.stop.assert_called_once_with()
            http.stop.assert_called_once_with()


class LauncherGrantTests(unittest.TestCase):
    def run_launcher(self, root: Path, names: tuple[str, ...]):
        public = root / "public"
        public.mkdir()
        relay_server, relay_client = root / "relay-server", root / "relay-client"
        relay_server.mkdir()
        relay_client.mkdir()
        calls = []
        runtime = mock.Mock(ports={"tool": 49153})
        client = root / "tool-client"
        client.write_text("#!/bin/sh\nexit 0\n")
        client.chmod(0o755)
        span = span_runtime.Span("tool", client, root / "tool-span")
        completed = mock.Mock(returncode=0)
        with mock.patch.object(devc2, "require_docker"), \
             mock.patch.object(devc2, "ensure_v1_not_running"), \
             mock.patch.object(devc2, "ensure_tact_config"), \
             mock.patch.object(devc2, "repository_lock", return_value=contextlib.nullcontext()), \
             mock.patch.object(devc2, "prepare", return_value=public), \
             mock.patch.object(devc2, "generate_relay_pki", return_value=(relay_server, relay_client)), \
             mock.patch.object(devc2, "remove_span_volume"), \
             mock.patch.object(devc2, "compose", side_effect=lambda *args, **kwargs: calls.append(args)), \
             mock.patch.object(devc2, "require_bound_image", side_effect=lambda image: "sha256:"+("1" if image==devc2.RUNTIME_IMAGE else "2")*64), \
             mock.patch.object(devc2.subprocess, "run", return_value=completed), \
             mock.patch.object(devc2.span_runtime, "load_catalog", return_value=[span] if names else []) as load_catalog, \
             mock.patch.object(devc2.span_runtime, "SpanRuntime", return_value=runtime) as start_runtime:
            self.assertEqual(devc2._start_unlocked(root, span_names=names), 0)
        return calls, load_catalog, start_runtime, runtime, public

    def test_no_grant_starts_no_provider_or_span_sidecar(self):
        with tempfile.TemporaryDirectory() as raw:
            calls, load_catalog, start_runtime, runtime, public = self.run_launcher(Path(raw), ())
            load_catalog.assert_called_once()
            args=load_catalog.call_args.args
            self.assertEqual(args[:3],(devc2.SPAN_CATALOG,(),Path(raw)))
            self.assertEqual((args[3].name,args[4].name),("span-clients","span-providers"))
            self.assertEqual(args[5],devc2.ROOT/"spans")
            start_runtime.assert_not_called()
            runtime.stop.assert_not_called()
            self.assertFalse((public / "spans.json").exists())
            self.assertEqual(calls[0][1].get("COMPOSE_PROFILES"),"spans")
            self.assertNotIn("span-proxy", " ".join(str(part) for call in calls for part in call[2:]))

    def test_explicit_grant_starts_and_stops_only_that_span(self):
        with tempfile.TemporaryDirectory() as raw:
            calls, load_catalog, start_runtime, runtime, public = self.run_launcher(Path(raw), ("tool",))
            load_catalog.assert_called_once()
            args=load_catalog.call_args.args
            self.assertEqual(args[:3],(devc2.SPAN_CATALOG,("tool",),Path(raw)))
            self.assertEqual((args[3].name,args[4].name),("span-clients","span-providers"))
            self.assertEqual(args[5],devc2.ROOT/"spans")
            start_runtime.assert_called_once()
            runtime.stop.assert_called_once()
            self.assertEqual(json.loads((public / "spans.json").read_text()), [{"name": "tool", "port": 49153}])
            self.assertIn("span-proxy", " ".join(str(part) for call in calls for part in call))
            up=next(call for call in calls if "up" in call)
            self.assertIn("never",up)
            self.assertIn("--no-build",up)


if __name__ == "__main__":
    unittest.main()
