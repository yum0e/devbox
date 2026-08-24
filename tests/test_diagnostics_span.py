from __future__ import annotations

import base64
import json
import os
import socket
import struct
import tempfile
import time
import unittest
from pathlib import Path

from launcher import devc2, span_runtime


ROOT = Path(__file__).parents[1]
WORLD = ROOT / "spans" / "diagnostics" / "world"
COMMAND_LINK = ROOT / "launcher" / "command_projection.py"
HEADER = struct.Struct("!I")


def receive(connection: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        block = connection.recv(size - len(result))
        if not block:
            raise RuntimeError("response ended early")
        result.extend(block)
    return bytes(result)


class DiagnosticsWorldTests(unittest.TestCase):
    def test_builtin_uses_the_generic_command_link(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "snapshot").mkdir()
            item = span_runtime.load_catalog(
                root / "missing.json",
                ("diagnostics",),
                root / "workspace",
                root / "snapshot" / "links",
                root / "snapshot" / "worlds",
                ROOT / "spans",
                command_projection=COMMAND_LINK,
            )[0]
            self.assertEqual(item.provider.read_bytes(), WORLD.read_bytes())
            self.assertEqual(item.client.read_bytes(), COMMAND_LINK.read_bytes())
            self.assertNotIn(b"diagnostics", COMMAND_LINK.read_bytes().lower())

    def test_report_exposes_only_sanitized_launch_local_state(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = span_runtime.DiagnosticsState(root / "state.json", ["diagnostics", "ssh-agent"])
            state.accepted("ssh-agent")
            state.update("ssh-agent", last_stage="World connection")
            state.finished("ssh-agent", "World connection", ConnectionRefusedError("secret detail"))
            document = json.loads((root / "state.json").read_text())
            encoded = json.dumps(document)
            self.assertNotIn(str(root), encoded)
            self.assertNotIn("secret detail", encoded)
            ssh = next(item for item in document["links"] if item["name"] == "ssh-agent")
            self.assertEqual(ssh["accepted"], 1)
            self.assertEqual(ssh["active"], 0)
            self.assertEqual(ssh["failed"], 1)
            self.assertEqual(ssh["last_stage"], "World connection")
            self.assertEqual(ssh["last_result"], "failed")
            self.assertEqual(ssh["last_error"], "ConnectionRefusedError")
            self.assertEqual(ssh["world_status"], "starting")
            self.assertIsNone(ssh["world_exit"])
            self.assertEqual(ssh["recovery_state"], "healthy")
            self.assertEqual(ssh["recoveries"], 0)
            self.assertEqual(ssh["recovery_failures"], 0)
            self.assertEqual((root / "state.json").stat().st_mode & 0o777, 0o600)

    def test_world_serves_report_and_rejects_extra_authority(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = span_runtime.DiagnosticsState(root / "state.json", ["diagnostics"])
            span = span_runtime.Span("diagnostics", COMMAND_LINK, WORLD)
            provider = span_runtime.Provider(span, root, "island-one", state.path)
            try:
                provider.check_started()
                for argv, expected_status in ((["report"], 0), (["shell", "id"], 2)):
                    request = json.dumps({
                        "v": 1,
                        "command": "diagnostics",
                        "argv": argv,
                        "stdin_b64": "",
                    }, separators=(",", ":")).encode()
                    with socket.create_connection(provider.address, timeout=2) as connection:
                        connection.sendall(HEADER.pack(len(request)) + request)
                        size = HEADER.unpack(receive(connection, HEADER.size))[0]
                        response = json.loads(receive(connection, size))
                    self.assertTrue(response["ok"])
                    self.assertEqual(response["exit_code"], expected_status)
                    output = base64.b64decode(response["stdout_b64"])
                    error = base64.b64decode(response["stderr_b64"])
                    if expected_status == 0:
                        self.assertEqual(json.loads(output), json.loads(state.path.read_text()))
                    else:
                        self.assertIn(b"only report and help", error)
            finally:
                provider.stop()

    def test_state_write_failure_never_breaks_an_observed_link(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = span_runtime.DiagnosticsState(root / "state.json", ["ssh-agent"])
            state.path = root / "missing" / "state.json"
            state.accepted("ssh-agent")
            state.update("ssh-agent", last_stage="stream relay")
            state.finished("ssh-agent", "stream relay", None)
            self.assertEqual(state.links["ssh-agent"]["completed"], 1)

    def test_recovery_state_is_bounded_and_contains_no_error_details(self):
        with tempfile.TemporaryDirectory() as raw:
            state = span_runtime.DiagnosticsState(Path(raw) / "state.json", ["openai"])
            state.recovery_started("openai")
            state.recovery_failed("openai")
            state.recovery_started("openai")
            state.recovery_succeeded("openai")
            link = state.links["openai"]
            self.assertEqual(link["recovery_state"], "healthy")
            self.assertEqual(link["recoveries"], 1)
            self.assertEqual(link["recovery_failures"], 1)
            self.assertEqual(set(link), {
                "startup", "world_status", "world_exit", "accepted", "active",
                "completed", "failed", "rejected", "last_stage", "last_result",
                "last_error", "recovery_state", "recoveries", "recovery_failures",
            })

    def test_runtime_records_a_world_that_exits_after_structural_startup(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            delayed = root / "delayed-world"
            delayed.write_text("#!/bin/sh\nsleep 0.35\nexit 23\n")
            delayed.chmod(0o755)
            server_tls, _client_tls = devc2.generate_relay_pki(root / "pki")
            runtime = span_runtime.SpanRuntime([
                span_runtime.Span("diagnostics", COMMAND_LINK, WORLD),
                span_runtime.Span("delayed", COMMAND_LINK, delayed),
            ], root, "island-one", server_tls)
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    delayed_state = runtime.diagnostics.links["delayed"]
                    if delayed_state["world_status"] == "exited":
                        break
                    time.sleep(0.05)
                self.assertEqual(delayed_state["world_status"], "exited")
                self.assertEqual(delayed_state["world_exit"], 23)
            finally:
                runtime.stop()


if __name__ == "__main__":
    unittest.main()
