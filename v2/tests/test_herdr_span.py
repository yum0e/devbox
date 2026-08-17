from __future__ import annotations

import base64
import importlib.machinery
import importlib.util
import json
import os
import shlex
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "herdr-span"
PROVIDER_PATH = EXAMPLE / "herdr-span"
REGISTER = EXAMPLE / "register.py"
AGENT_PATH = EXAMPLE / "docker_agent.py"
WORKER_PATH = EXAMPLE / "worker_link.py"
BUNDLE_PATH = EXAMPLE / "bundle.py"
SUPERVISOR_PATH = EXAMPLE / "island_supervisor.py"


def load(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


H = load(PROVIDER_PATH, "herdr_span_example")
S = load(EXAMPLE / "island_supervisor.py", "island_supervisor")
D = load(AGENT_PATH, "herdr_docker_agent")
W = load(WORKER_PATH, "herdr_worker_link")
sys.modules.update({"herdr_world": H, "docker_agent": D, "worker_link": W})
B = load(BUNDLE_PATH, "herdr_bundle")


class FakeHerdr:
    def __init__(self):
        self.calls = []
        self.output = ""
        self.panes = {
            "w1:p1": "terminal-anchor",
            "w1:p2": "terminal-new",
        }
        self.world = None

    def call(self, method, params, timeout=10):
        self.calls.append((method, params, timeout))
        if method == "pane.split":
            self.panes["w1:p2"] = "terminal-new"
            return {"pane": {"pane_id": "w1:p2", "terminal_id": "terminal-new"}}
        if method == "pane.get":
            pane_id = params["pane_id"]
            if pane_id not in self.panes:
                raise H.HerdrRejected("not_found", "pane not found")
            return {"pane": {"pane_id": pane_id, "terminal_id": self.panes[pane_id]}}
        if method == "pane.close":
            self.panes.pop(params["pane_id"], None)
            return {"type": "ok"}
        if method == "pane.send_input" and self.world is not None:
            with self.world.status_condition:
                pending = [job for job in self.world.tokens.values() if not job.started]
                if pending:
                    pending[-1].started = True
                    self.world.status_condition.notify_all()
        if method == "pane.read":
            return {"read": {"text": self.output}}
        if method == "pane.wait_for_output":
            return {"type": "output_matched"}
        return {"type": "ok"}


def provider() -> H.World:
    item = H.World.__new__(H.World)
    item.anchor = "w1:p1"
    item.herdr = FakeHerdr()
    item.worker_executable = "/trusted/herdr-span"
    item.worker_socket = Path("/private/worker.sock")
    item.jobs = {}
    item.owned = {}
    item.tokens = {}
    item.lock = threading.RLock()
    item.status_condition = threading.Condition(item.lock)
    item.stopping = threading.Event()
    item.status = mock.Mock()
    item.status_thread = mock.Mock()
    item.herdr.world = item
    return item


def docker_backend() -> D.DockerIsland:
    item = D.DockerIsland.__new__(D.DockerIsland)
    item.workspace = Path("/workspace")
    item.docker = "/bin/true"
    return item


class WorldAuthorityTests(unittest.TestCase):
    def test_herdr_socket_address_is_compatible_with_host_python_39(self):
        self.assertEqual(H.Herdr(Path("/host/herdr.sock")).path, "/host/herdr.sock")

    def test_upstream_validation_is_lazy_retryable_and_nonfatal(self):
        status_world, status_agent = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        environment = {
            "HERDR_ENV": "1",
            "HERDR_SOCKET_PATH": "/host/herdr.sock",
            "HERDR_PANE_ID": "w1:p1",
            "DEVC2_HERDR_WORKER": "/trusted/herdr-span",
            "DEVC2_HERDR_WORKER_SOCKET": "/private/worker.sock",
            "DEVC2_HERDR_STATUS_FD": str(status_world.detach()),
        }
        responses = [H.SpanError("Herdr is offline"), {"pane": {"pane_id": "w1:p1"}}]
        with mock.patch.dict(H.os.environ, environment, clear=True), \
             mock.patch.object(H, "safe_socket", return_value=Path("/host/herdr.sock")), \
             mock.patch.object(H, "safe_executable", return_value="/bin/true"), \
             mock.patch.object(H.Herdr, "call", side_effect=responses) as call:
            item = H.World()
            self.assertEqual(call.call_count, 0, "World startup must not contact semantic upstreams")
            with self.assertRaisesRegex(H.SpanError, "offline"):
                item.handle({"op": "list"})
            self.assertEqual(item.handle({"op": "list"}), [])
            item.cleanup()
        status_agent.close()

    def test_spawn_constructs_only_a_worker_invocation(self):
        item = provider()
        argv = ["sh", "-lc", "printf '%s' \"$HOME; $(id)\"", "", "snowman-☃"]
        self.assertEqual(item.spawn({"op": "spawn", "name": "reviewer", "argv": argv}), "reviewer")
        self.assertEqual(item.herdr.calls[0], (
            "pane.split", {"target_pane_id": "w1:p1", "direction": "right", "focus": False}, 10,
        ))
        method, params, _timeout = item.herdr.calls[1]
        self.assertEqual(method, "pane.send_input")
        self.assertEqual(params["pane_id"], "w1:p2")
        self.assertEqual(params["keys"], ["Enter"])
        self.assertTrue(params["text"].startswith("printf '\\033[2J\\033[3J\\033[H'; exec "))
        host_argv = shlex.split(params["text"])
        exec_index = host_argv.index("exec")
        self.assertEqual(host_argv[exec_index:exec_index + 4], [
            "exec", "/usr/bin/env", "-i", "PATH=/usr/local/bin:/usr/bin:/bin",
        ])
        self.assertEqual(host_argv[-3:-1], ["/trusted/herdr-span", "worker"])
        task = W.decode(host_argv[-1])
        self.assertEqual(task["socket"], "/private/worker.sock")
        self.assertEqual(task["argv"], argv)
        self.assertRegex(task["token"], r"^[0-9a-f]{32}$")
        self.assertEqual(item.herdr.calls[2][0], "pane.rename")
        self.assertNotIn("pane.wait_for_output", [call[0] for call in item.herdr.calls])
        self.assertTrue(item.jobs["reviewer"].started)
        self.assertEqual(set(item.jobs), {"reviewer"})
        self.assertEqual(set(item.owned), {"w1:p2"})

    def test_private_status_channel_drives_lifecycle_without_terminal_markers(self):
        item = provider()
        world_status, agent_status = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        item.status = world_status
        item.status.settimeout(0.05)
        job = H.Job("worker", "w1:p2", "terminal-new", "6" * 32)
        item.jobs[job.name] = job
        item.owned[job.pane_id] = job
        item.tokens[job.token] = job
        thread = threading.Thread(target=item.receive_status)
        thread.start()
        try:
            agent_status.send(H.STATUS.pack(b"R", job.token.encode(), 0))
            deadline = time.monotonic() + 1
            while not job.started and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(job.started)
            agent_status.send(H.STATUS.pack(b"E", job.token.encode(), 23))
            deadline = time.monotonic() + 1
            while job.exit_code is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(job.exit_code, 23)
        finally:
            item.stopping.set()
            world_status.close()
            agent_status.close()
            thread.join(1)

    def test_spawn_rolls_back_a_split_when_submission_fails(self):
        item = provider()
        original = item.herdr.call

        def fail(method, params, timeout=10):
            if method == "pane.send_input":
                raise H.SpanError("no")
            return original(method, params, timeout)

        item.herdr.call = fail
        with self.assertRaisesRegex(H.SpanError, "no"):
            item.spawn({"op": "spawn", "name": "worker", "argv": ["true"]})
        self.assertEqual(item.herdr.calls[-1][0], "pane.close")
        self.assertFalse(item.jobs)
        self.assertFalse(item.owned)

    def test_failed_spawn_keeps_only_private_cleanup_authority_when_close_fails(self):
        item = provider()
        item.herdr.world = None
        original = item.herdr.call

        def fail(method, params, timeout=10):
            if method == "pane.close":
                raise H.SpanError("close failed")
            return original(method, params, timeout)

        item.herdr.call = fail
        with mock.patch.object(H, "START_TIMEOUT_MS", 1), \
             self.assertRaisesRegex(H.SpanError, "private readiness"):
                item.spawn({"op": "spawn", "name": "worker", "argv": ["true"]})
        self.assertFalse(item.jobs)
        self.assertEqual(set(item.owned), {"w1:p2"})

    def test_spawn_cannot_race_past_teardown(self):
        item = provider()
        item.stopping.set()
        with self.assertRaisesRegex(H.SpanError, "stopping"):
            item.spawn({"op": "spawn", "name": "late", "argv": ["true"]})
        self.assertNotIn("pane.split", [call[0] for call in item.herdr.calls])

    def test_only_owned_names_can_be_addressed(self):
        item = provider()
        item.jobs["owned"] = H.Job("owned", "w1:p2", "terminal-new", "__DEVC2_HERDR_" + "0" * 32 + "_EXIT_")
        with self.assertRaises(H.SpanError):
            item.handle({"op": "read", "name": "w1:p99", "lines": 10})
        self.assertEqual(item.handle({"op": "read", "name": "owned", "lines": 10}), "")
        self.assertNotIn("pane.list", [call[0] for call in item.herdr.calls])

    def test_terminal_identity_is_rechecked(self):
        item = provider()
        item.jobs["owned"] = H.Job(
            "owned", "w1:p2", "different-terminal", "__DEVC2_HERDR_" + "0" * 32 + "_EXIT_"
        )
        with self.assertRaisesRegex(H.SpanError, "no longer owns"):
            item.handle({"op": "send", "name": "owned", "text": "hello"})

    def test_wait_returns_the_worker_exit_code(self):
        item = provider()
        item.jobs["tests"] = H.Job("tests", "w1:p2", "terminal-new", "1" * 32, exit_code=17)
        self.assertEqual(item.handle({"op": "wait", "name": "tests", "timeout_ms": 1000}), {"exit_code": 17})

    def test_read_returns_only_worker_output(self):
        item = provider()
        item.jobs["clean"] = H.Job("clean", "w1:p2", "terminal-new", "5" * 32, started=True)
        item.herdr.output = "worker output\n"
        self.assertEqual(item.handle({"op": "read", "name": "clean", "lines": 10}), "worker output\n")
        self.assertNotIn("PRIVATE_PAYLOAD", item.handle({"op": "read", "name": "clean", "lines": 10}))

    def test_finished_worker_rejects_input(self):
        item = provider()
        item.jobs["done"] = H.Job("done", "w1:p2", "terminal-new", "2" * 32, exit_code=0)
        with self.assertRaisesRegex(H.SpanError, "has exited"):
            item.handle({"op": "send", "name": "done", "text": "touch /tmp/host"})

    def test_close_removes_only_a_revalidated_owned_pane(self):
        item = provider()
        job = H.Job("done", "w1:p2", "terminal-new", "__DEVC2_HERDR_" + "a" * 32 + "_EXIT_", exit_code=0)
        item.jobs[job.name] = job
        item.owned[job.pane_id] = job
        self.assertIsNone(item.handle({"op": "close", "name": "done"}))
        self.assertFalse(item.jobs)
        self.assertFalse(item.owned)
        self.assertEqual(item.herdr.calls[-2][0], "pane.close")
        self.assertEqual(item.herdr.calls[-1][:2], ("pane.get", {"pane_id": "w1:p2"}))

    def test_close_failure_retains_public_and_cleanup_ownership(self):
        item = provider()
        job = H.Job("live", "w1:p2", "terminal-new", "__DEVC2_HERDR_" + "b" * 32 + "_EXIT_")
        item.jobs[job.name] = job
        item.owned[job.pane_id] = job
        original = item.herdr.call

        def fail(method, params, timeout=10):
            if method == "pane.close":
                raise H.SpanError("close failed")
            return original(method, params, timeout)

        item.herdr.call = fail
        with self.assertRaisesRegex(H.SpanError, "close failed"):
            item.handle({"op": "close", "name": "live"})
        self.assertIs(item.jobs["live"], job)
        self.assertIs(item.owned["w1:p2"], job)

    def test_close_success_without_disappearance_retains_ownership(self):
        item = provider()
        job = H.Job("live", "w1:p2", "terminal-new", "__DEVC2_HERDR_" + "c" * 32 + "_EXIT_")
        item.jobs[job.name] = job
        item.owned[job.pane_id] = job
        original = item.herdr.call

        def acknowledge_without_closing(method, params, timeout=10):
            if method == "pane.close":
                return {"type": "ok"}
            return original(method, params, timeout)

        item.herdr.call = acknowledge_without_closing
        with mock.patch.object(H.time, "sleep", return_value=None), \
             mock.patch.object(H.time, "monotonic", side_effect=[0, 0, 3]):
            with self.assertRaisesRegex(H.SpanError, "close was not confirmed"):
                item.handle({"op": "close", "name": "live"})
        self.assertIs(item.jobs["live"], job)
        self.assertIs(item.owned["w1:p2"], job)

    def test_close_lost_response_forgets_a_provably_gone_pane(self):
        item = provider()
        job = H.Job("gone", "w1:p2", "terminal-new", "__DEVC2_HERDR_" + "d" * 32 + "_EXIT_")
        item.jobs[job.name] = job
        item.owned[job.pane_id] = job
        original = item.herdr.call
        closed = False

        def lose_response(method, params, timeout=10):
            nonlocal closed
            if method == "pane.close":
                closed = True
                raise OSError("response lost")
            if method == "pane.get" and closed:
                raise H.HerdrRejected("not_found", "pane not found")
            return original(method, params, timeout)

        item.herdr.call = lose_response
        self.assertIsNone(item.handle({"op": "close", "name": "gone"}))
        self.assertTrue(job.closed)
        self.assertFalse(item.jobs)
        self.assertFalse(item.owned)

    def test_close_accepts_herdrs_pane_specific_not_found_code(self):
        item = provider()
        job = H.Job("gone", "w1:p2", "terminal-new", "8" * 32)
        item.jobs[job.name] = job
        item.owned[job.pane_id] = job
        original = item.herdr.call
        closed = False

        def close_then_report_missing(method, params, timeout=10):
            nonlocal closed
            if method == "pane.close":
                closed = True
                return {"type": "ok"}
            if method == "pane.get" and params["pane_id"] == job.pane_id and closed:
                raise H.HerdrRejected("pane_not_found", f"pane {job.pane_id} not found")
            return original(method, params, timeout)

        item.herdr.call = close_then_report_missing
        self.assertIsNone(item.handle({"op": "close", "name": "gone"}))
        self.assertTrue(job.closed)
        self.assertFalse(item.jobs)
        self.assertFalse(item.owned)

    def test_send_and_close_are_serialized_per_worker(self):
        item = provider()
        job = H.Job("live", "w1:p2", "terminal-new", "__DEVC2_HERDR_" + "e" * 32 + "_EXIT_")
        item.jobs[job.name] = job
        item.owned[job.pane_id] = job
        original = item.herdr.call
        sending = threading.Event()
        release = threading.Event()
        errors = []

        def block_send(method, params, timeout=10):
            if method == "pane.send_input" and params.get("text") == "hello":
                sending.set()
                release.wait(2)
            return original(method, params, timeout)

        def run(request):
            try:
                item.handle(request)
            except Exception as error:
                errors.append(error)

        item.herdr.call = block_send
        sender = threading.Thread(target=run, args=({"op": "send", "name": "live", "text": "hello"},))
        closer = threading.Thread(target=run, args=({"op": "close", "name": "live"},))
        sender.start()
        self.assertTrue(sending.wait(1))
        closer.start()
        self.assertFalse(any(call[0] == "pane.close" for call in item.herdr.calls))
        release.set()
        sender.join(2)
        closer.join(2)
        self.assertFalse(sender.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertFalse(errors)
        self.assertEqual(sum(call[0] == "pane.close" for call in item.herdr.calls), 1)

    def test_cleanup_cannot_double_close_a_public_close(self):
        item = provider()
        job = H.Job("live", "w1:p2", "terminal-new", "__DEVC2_HERDR_" + "f" * 32 + "_EXIT_")
        item.jobs[job.name] = job
        item.owned[job.pane_id] = job
        original = item.herdr.call
        closing = threading.Event()
        release = threading.Event()
        errors = []

        def block_close(method, params, timeout=10):
            if method == "pane.close":
                closing.set()
                release.wait(2)
            return original(method, params, timeout)

        def public_close():
            try:
                item.handle({"op": "close", "name": "live"})
            except Exception as error:
                errors.append(error)

        item.herdr.call = block_close
        closer = threading.Thread(target=public_close)
        closer.start()
        self.assertTrue(closing.wait(1))
        teardown = threading.Thread(target=item.cleanup)
        teardown.start()
        release.set()
        closer.join(2)
        teardown.join(3)
        self.assertFalse(errors)
        self.assertFalse(closer.is_alive())
        self.assertFalse(teardown.is_alive())
        self.assertEqual(sum(call[0] == "pane.close" for call in item.herdr.calls), 1)
        self.assertFalse(item.owned)

    def test_cleanup_retains_ownership_after_a_non_not_found_rejection(self):
        item = provider()
        job = H.Job("live", "w1:p2", "terminal-new", "__DEVC2_HERDR_" + "7" * 32 + "_EXIT_")
        item.jobs[job.name] = job
        item.owned[job.pane_id] = job
        original = item.herdr.call

        def reject_close(method, params, timeout=10):
            if method == "pane.close":
                raise H.HerdrRejected("permission_denied", "close denied")
            return original(method, params, timeout)

        item.herdr.call = reject_close
        item.cleanup()
        self.assertIs(item.jobs[job.name], job)
        self.assertIs(item.owned[job.pane_id], job)
        self.assertFalse(job.closed)

    def test_spawn_boundary_makes_immediate_send_safe(self):
        item = provider()
        item.spawn({"op": "spawn", "name": "starting", "argv": ["sh"]})
        self.assertIsNone(item.handle({"op": "send", "name": "starting", "text": "hello"}))
        methods = [call[0] for call in item.herdr.calls]
        sent = len(methods) - 1
        self.assertEqual(methods[sent], "pane.send_input")
        self.assertEqual(methods.count("pane.send_input"), 2)
        self.assertNotIn("pane.wait_for_output", methods)

    def test_worker_uses_private_readiness_and_clears_bootstrap_ui(self):
        self.assertEqual(W.CLEAR_TERMINAL, b"\x1b[2J\x1b[3J\x1b[H")
        self.assertNotIn(b"DEVC2_HERDR", WORKER_PATH.read_bytes())

    def test_worker_projects_a_bounded_initial_terminal_size(self):
        with mock.patch.object(W.os, "get_terminal_size", return_value=os.terminal_size((132, 43))):
            self.assertEqual(W.terminal_size(), (43, 132))
        with mock.patch.object(W.os, "get_terminal_size", side_effect=OSError):
            self.assertEqual(W.terminal_size(), (24, 80))

    def test_send_rejects_terminal_control_characters(self):
        item = provider()
        item.jobs["live"] = H.Job(
            "live", "w1:p2", "terminal-new", "__DEVC2_HERDR_" + "4" * 32 + "_EXIT_"
        )
        for control in ("\x03", "\x04", "\x1a", "\x1b", "\x7f"):
            with self.subTest(control=repr(control)), self.assertRaisesRegex(H.SpanError, "control"):
                item.handle({"op": "send", "name": "live", "text": "hello" + control})
        self.assertNotIn("pane.send_input", [call[0] for call in item.herdr.calls])

    def test_protocol_rejects_extra_authority_fields(self):
        item = provider()
        attempts = (
            {"op": "spawn", "name": "x", "argv": ["true"], "container": "host"},
            {"op": "spawn", "name": "x", "argv": ["true"], "cwd": "/"},
            {"op": "read", "name": "x", "lines": 1, "pane_id": "w1:p1"},
            {"op": "close", "name": "x", "pane_id": "w1:p1"},
        )
        for request in attempts:
            with self.subTest(request=request), self.assertRaises(H.SpanError):
                item.handle(request)

    def test_container_selection_requires_the_exact_oneoff_island_bind(self):
        item = docker_backend()
        labels = {
            "dev.devbox.generation": "2",
            "dev.devbox.workspace-hash": item.workspace_hash,
            "com.docker.compose.project": f"devc2-{item.workspace_hash}",
            "com.docker.compose.service": "devbox",
            "com.docker.compose.oneoff": "True",
        }
        inspect = [{
            "Id": "c" * 64,
            "State": {"Running": True},
            "Config": {"Labels": labels},
            "Mounts": [{"Destination": "/workspace", "Type": "bind", "RW": True, "Source": "/workspace"}],
        }]
        responses = (
            subprocess.CompletedProcess([], 0, stdout="short-id\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=json.dumps(inspect), stderr=""),
        )
        with mock.patch.object(D.subprocess, "run", side_effect=responses) as run:
            self.assertEqual(item.container(), "c" * 64)
        ps = run.call_args_list[0].args[0]
        self.assertIn("label=com.docker.compose.oneoff=True", ps)

        inspect[0]["Mounts"][0]["Source"] = "/other"
        responses = (
            subprocess.CompletedProcess([], 0, stdout="short-id\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=json.dumps(inspect), stderr=""),
        )
        with mock.patch.object(D.subprocess, "run", side_effect=responses), self.assertRaisesRegex(D.AgentError, "granted Island"):
            item.container()

    def test_container_selection_accepts_only_exact_docker_desktop_workspace_encoding(self):
        item = docker_backend()
        labels = {
            "dev.devbox.generation": "2",
            "dev.devbox.workspace-hash": item.workspace_hash,
            "com.docker.compose.project": f"devc2-{item.workspace_hash}",
            "com.docker.compose.service": "devbox",
            "com.docker.compose.oneoff": "True",
        }
        inspect = [{
            "Id": "d" * 64,
            "State": {"Running": True},
            "Config": {"Labels": labels},
            "Mounts": [{
                "Destination": "/workspace", "Type": "bind", "RW": True,
                "Source": "/host_mnt/workspace",
            }],
        }]
        responses = (
            subprocess.CompletedProcess([], 0, stdout="short-id\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=json.dumps(inspect), stderr=""),
        )
        with mock.patch.object(D.sys, "platform", "darwin"), \
             mock.patch.object(D.subprocess, "run", side_effect=responses):
            self.assertEqual(item.container(), "d" * 64)

        inspect[0]["Mounts"][0]["Source"] = "/host_mnt/other"
        responses = (
            subprocess.CompletedProcess([], 0, stdout="short-id\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=json.dumps(inspect), stderr=""),
        )
        with mock.patch.object(D.sys, "platform", "darwin"), \
             mock.patch.object(D.subprocess, "run", side_effect=responses), \
             self.assertRaisesRegex(D.AgentError, "granted Island"):
            item.container()

    def test_worker_protocol_exposes_no_backend_options(self):
        task = {
            "socket": "/private/worker.sock",
            "argv": ["sh", "-lc", "echo $HOME; $(id)"],
            "token": "3" * 32,
        }
        raw = base64.urlsafe_b64encode(json.dumps(task).encode()).decode()
        self.assertEqual(W.decode(raw), task)
        for field in ("docker", "container", "cwd", "environment", "user", "pty"):
            altered = {**task, field: "forbidden"}
            payload = base64.urlsafe_b64encode(json.dumps(altered).encode()).decode()
            with self.subTest(field=field), self.assertRaises(W.WorkerError):
                W.decode(payload)

    def test_agent_owns_the_fixed_docker_invocation(self):
        item = docker_backend()
        item.container = lambda: "b" * 64
        self.assertEqual(item.command(["sh", "-lc", "echo ok"], 43, 132), [
            "/bin/true", "exec", "--interactive", "--user", "1000:1000",
            "--workdir", "/workspace", "b" * 64,
            "/usr/bin/python3", "-c", D.SUPERVISOR_SOURCE,
            "43", "132", "--", "sh", "-lc", "echo ok",
        ])

    def test_agent_worker_request_is_exact_and_bounded(self):
        valid = {
            "argv": ["printf", "ok"], "token": "1" * 32,
            "rows": 43, "columns": 132,
        }

        def decode(document):
            left, right = socket.socketpair()
            try:
                payload = json.dumps(document).encode()
                left.sendall(struct.pack("!I", len(payload)) + payload)
                return D.request(right)
            finally:
                left.close()
                right.close()

        self.assertEqual(decode(valid), valid)
        for extra in ("docker", "container", "cwd", "environment", "pane_id"):
            with self.subTest(extra=extra), self.assertRaises(D.AgentError):
                decode({**valid, extra: "forbidden"})

    def test_agent_relays_a_real_pty_and_publishes_exit(self):
        class LocalBackend:
            def command(self, argv, rows, columns):
                return argv

        listener = socket.socket()
        agent = D.Agent(listener, LocalBackend())
        worker, projection = socket.socketpair()
        thread = threading.Thread(target=agent.run_worker, args=(worker, {
            "argv": ["/bin/sh", "-c", "printf 'pty-proof:%s:' \"$TERM\"; stty size"],
            "token": "2" * 32,
            "rows": 31,
            "columns": 97,
        }))
        thread.start()
        projection.settimeout(3)
        output = bytearray()
        exit_code = None
        try:
            while exit_code is None:
                kind, size = D.FRAME.unpack(D.receive(projection, D.FRAME.size))
                payload = D.receive(projection, size)
                if kind == b"S":
                    self.assertEqual(payload, b"")
                elif kind == b"O":
                    output.extend(payload)
                elif kind == b"E":
                    exit_code = payload[0]
        finally:
            projection.close()
            worker.close()
            thread.join(3)
            listener.close()
        self.assertFalse(thread.is_alive())
        self.assertIn(b"pty-proof:xterm-256color:31 97", output)
        self.assertEqual(exit_code, 0)

    def test_agent_stops_the_pty_process_when_the_worker_disconnects(self):
        class LocalBackend:
            def command(self, argv, rows, columns):
                return argv

        listener = socket.socket()
        agent = D.Agent(listener, LocalBackend())
        worker, projection = socket.socketpair()
        errors = []

        def relay():
            try:
                agent.run_worker(worker, {
                    "argv": ["/bin/sh", "-c", "sleep 30"], "token": "3" * 32,
                    "rows": 24, "columns": 80,
                })
            except OSError as error:
                errors.append(error)

        thread = threading.Thread(target=relay)
        thread.start()
        projection.close()
        thread.join(4)
        worker.close()
        listener.close()
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)

    def test_supervisor_projects_terminal_identity_and_initial_size(self):
        class SupervisedBackend:
            supervised = True

            def command(self, argv, rows, columns):
                return [
                    sys.executable, str(SUPERVISOR_PATH),
                    str(rows), str(columns), "--", *argv,
                ]

        listener = socket.socket()
        agent = D.Agent(listener, SupervisedBackend())
        worker, projection = socket.socketpair()
        thread = threading.Thread(target=agent.run_worker, args=(worker, {
            "argv": [
                "/bin/sh", "-c",
                "printf 'terminal:%s:' \"$TERM\"; stty size",
            ],
            "token": "9" * 32, "rows": 37, "columns": 101,
        }))
        thread.start()
        projection.settimeout(3)
        output = bytearray()
        exit_code = None
        try:
            while exit_code is None:
                kind, size = D.FRAME.unpack(D.receive(projection, D.FRAME.size))
                payload = D.receive(projection, size)
                if kind == b"O":
                    output.extend(payload)
                elif kind == b"E":
                    exit_code = payload[0]
        finally:
            projection.close()
            worker.close()
            thread.join(3)
            listener.close()
        self.assertFalse(thread.is_alive())
        self.assertEqual(exit_code, 0)
        self.assertIn(b"terminal:xterm-256color:37 101", output)

    def test_supervisor_reaps_the_exact_worker_after_link_disconnect(self):
        class SupervisedBackend:
            supervised = True

            def command(self, argv, rows, columns):
                return [
                    sys.executable, str(SUPERVISOR_PATH),
                    str(rows), str(columns), "--", *argv,
                ]

        with tempfile.TemporaryDirectory() as raw:
            pid_file = Path(raw) / "worker.pid"
            listener = socket.socket()
            agent = D.Agent(listener, SupervisedBackend())
            worker, projection = socket.socketpair()
            command = [
                sys.executable, "-c",
                ("import os,sys,time; child=os.fork(); "
                 "(os.setsid(), open(sys.argv[1],'a').write(str(os.getpid())+'\\n'), time.sleep(30)) "
                 "if child == 0 else "
                 "(open(sys.argv[1],'a').write(str(os.getpid())+'\\n'), time.sleep(30))"),
                str(pid_file),
            ]
            thread = threading.Thread(
                target=agent.run_worker,
                args=(worker, {
                    "argv": command, "token": "4" * 32,
                    "rows": 24, "columns": 80,
                }),
            )
            thread.start()
            deadline = time.monotonic() + 3
            while (not pid_file.exists() or len(pid_file.read_text().splitlines()) < 2) \
                    and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(pid_file.exists())
            worker_pids = [int(value) for value in pid_file.read_text().splitlines()]
            self.assertEqual(len(worker_pids), 2)
            projection.close()
            thread.join(6)
            worker.close()
            listener.close()
            self.assertFalse(thread.is_alive())
            for worker_pid in worker_pids:
                with self.subTest(worker_pid=worker_pid), self.assertRaises(ProcessLookupError):
                    os.kill(worker_pid, 0)


class InstallerTests(unittest.TestCase):
    def test_bundle_keeps_world_authorities_disjoint(self):
        self.assertNotIn("HERDR_SOCKET_PATH", B.AGENT_ENVIRONMENT)
        self.assertNotIn("HERDR_PANE_ID", B.AGENT_ENVIRONMENT)
        self.assertNotIn("DEVC2_SPAN_SOCKET_FD", B.AGENT_ENVIRONMENT)
        self.assertNotIn("DEVC2_WORKSPACE", B.HERDR_ENVIRONMENT)
        self.assertNotIn("DEVC2_ISLAND_ID", B.HERDR_ENVIRONMENT)
        self.assertNotIn("DOCKER_HOST", B.HERDR_ENVIRONMENT)

    def test_installer_preserves_catalog_and_installs_immutable_generation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = root / "config"
            config.mkdir()
            catalog = config / "spans.json"
            catalog.write_text(json.dumps({"spans": {"other": {"provider": "/old/provider", "client": "/old/client"}}}))
            catalog.chmod(0o600)
            completed = subprocess.run(
                [sys.executable, str(REGISTER), str(catalog), str(root / "spans"), str(EXAMPLE)],
                text=True, capture_output=True, check=True,
            )
            self.assertIn("registered Herdr command Link", completed.stdout)
            document = json.loads(catalog.read_text())
            self.assertIn("other", document["spans"])
            world = Path(document["spans"]["herdr"])
            self.assertTrue(world.is_file())
            self.assertEqual(stat.S_IMODE(world.stat().st_mode), 0o555)
            self.assertEqual(set(world.parent.iterdir()), {world})
            with zipfile.ZipFile(world) as archive:
                self.assertEqual(set(archive.namelist()), {
                    "__main__.py", "herdr_world.py", "docker_agent.py",
                    "island_supervisor.py", "worker_link.py",
                })
                herdr_source = archive.read("herdr_world.py").lower()
            for backend_word in (b"docker", b"container", b"compose"):
                self.assertNotIn(backend_word, herdr_source)
            invoked = subprocess.run([world, "invalid"], text=True, capture_output=True)
            self.assertEqual(invoked.returncode, 2)
            self.assertIn("invalid bundle invocation", invoked.stderr)

            repeated = subprocess.run(
                [sys.executable, str(REGISTER), str(catalog), str(root / "spans"), str(EXAMPLE)],
                text=True, capture_output=True, check=True,
            )
            self.assertIn(str(world.parent), repeated.stdout)
            self.assertEqual(Path(json.loads(catalog.read_text())["spans"]["herdr"]), world)

            world.chmod(0o755)
            rejected = subprocess.run(
                [sys.executable, str(REGISTER), str(catalog), str(root / "spans"), str(EXAMPLE)],
                text=True, capture_output=True,
            )
            self.assertEqual(rejected.returncode, 1)
            self.assertIn("generation is inconsistent", rejected.stderr)


class CommandAffordanceTests(unittest.TestCase):
    def invoke(self, item, argv, stdin=b""):
        return H.invoke_command(item, {
            "v": 1,
            "command": "herdr",
            "argv": argv,
            "stdin_b64": base64.b64encode(stdin).decode(),
        })

    def output(self, response, name):
        return base64.b64decode(response[name]).decode()

    def test_help_is_exported_by_the_world_without_contacting_herdr(self):
        item = provider()
        response = self.invoke(item, ["--help"])
        self.assertEqual(response["exit_code"], 0)
        self.assertFalse(item.herdr.calls)
        output = self.output(response, "stdout_b64")
        for command in ("spawn", "list", "read", "send", "close", "wait"):
            self.assertIn(command, output)

    def test_close_cli_removes_an_owned_worker(self):
        item = provider()
        job = H.Job("agent", "w1:p2", "terminal-new", "__DEVC2_HERDR_" + "c" * 32 + "_EXIT_")
        item.jobs[job.name] = job
        item.owned[job.pane_id] = job
        response = self.invoke(item, ["close", "agent"])
        self.assertEqual(response["exit_code"], 0)
        self.assertEqual(self.output(response, "stdout_b64"), "")
        self.assertFalse(item.jobs)
        self.assertFalse(item.owned)

    def test_world_owns_cli_semantics_and_exit_status(self):
        item = provider()
        listed = self.invoke(item, ["list"])
        self.assertEqual(listed["exit_code"], 0)
        self.assertEqual(self.output(listed, "stdout_b64"), "")
        marker = "__DEVC2_HERDR_" + "9" * 32 + "_EXIT_"
        item.jobs["done"] = H.Job("done", "w1:p2", "terminal-new", marker, exit_code=23)
        waited = self.invoke(item, ["wait", "done"])
        self.assertEqual(waited["exit_code"], 23)

    def test_world_returns_normal_command_output_through_the_link(self):
        item = provider()

        def handle(request):
            if request["op"] == "spawn":
                return "w1:p2"
            if request["op"] == "list":
                return [{"name": "worker", "state": "running"}]
            if request["op"] == "read":
                return "worker output"
            self.fail(f"unexpected operation: {request['op']}")

        item.handle = mock.Mock(side_effect=handle)
        spawned = self.invoke(item, ["spawn", "worker", "--", "true"])
        listed = self.invoke(item, ["list"])
        read = self.invoke(item, ["read", "worker"])
        self.assertEqual(self.output(spawned, "stdout_b64"), "w1:p2\n")
        self.assertEqual(self.output(listed, "stdout_b64"), "worker\trunning\n")
        self.assertEqual(self.output(read, "stdout_b64"), "worker output\n")

    def test_world_rejects_other_commands_and_hidden_stdin(self):
        item = provider()
        with self.assertRaisesRegex(H.SpanError, "not exported"):
            H.invoke_command(item, {"v": 1, "command": "bash", "argv": [], "stdin_b64": ""})
        response = self.invoke(item, ["list"], b"hidden")
        self.assertEqual(response["exit_code"], 1)
        self.assertIn("only herdr send", self.output(response, "stderr_b64"))


if __name__ == "__main__":
    unittest.main()
