from __future__ import annotations

import base64
import importlib.machinery
import importlib.util
import json
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "herdr-span"
PROVIDER_PATH = EXAMPLE / "herdr-span"
REGISTER = EXAMPLE / "register.py"


def load(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


H = load(PROVIDER_PATH, "herdr_span_example")


class FakeHerdr:
    def __init__(self):
        self.calls = []
        self.output = ""

    def call(self, method, params, timeout=10):
        self.calls.append((method, params, timeout))
        if method == "pane.split":
            return {"pane": {"pane_id": "w1:p2", "terminal_id": "terminal-new"}}
        if method == "pane.get":
            return {"pane": {"pane_id": params["pane_id"], "terminal_id": "terminal-new"}}
        if method == "pane.read":
            return {"read": {"text": self.output}}
        if method == "pane.wait_for_output":
            return {"type": "output_matched"}
        return {"type": "ok"}


def provider() -> H.World:
    item = H.World.__new__(H.World)
    item.island_id = "island-one"
    item.workspace = Path("/workspace")
    item.anchor = "w1:p1"
    item.herdr = FakeHerdr()
    item.docker = "/bin/true"
    item.world_executable = "/trusted/herdr-span"
    item.jobs = {}
    item.lock = threading.RLock()
    item.stopping = threading.Event()
    item.island_container = lambda: "a" * 64
    return item


class WorldAuthorityTests(unittest.TestCase):
    def test_herdr_socket_address_is_compatible_with_host_python_39(self):
        self.assertEqual(H.Herdr(Path("/host/herdr.sock")).path, "/host/herdr.sock")

    def test_upstream_validation_is_lazy_retryable_and_nonfatal(self):
        environment = {
            "DEVC2_ISLAND_ID": "island-one",
            "DEVC2_WORKSPACE": "/workspace",
            "HERDR_ENV": "1",
            "HERDR_SOCKET_PATH": "/host/herdr.sock",
            "HERDR_PANE_ID": "w1:p1",
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
        host_argv = shlex.split(params["text"])
        self.assertEqual(host_argv[:3], ["exec", "/trusted/herdr-span", "worker"])
        task = H.decode_worker(host_argv[3])
        self.assertEqual(task["docker"], "/bin/true")
        self.assertEqual(task["container"], "a" * 64)
        self.assertEqual(task["argv"], argv)
        self.assertRegex(task["marker"], r"^__DEVC2_HERDR_[0-9a-f]{32}_EXIT_$")
        self.assertEqual(item.herdr.calls[2][0], "pane.rename")
        self.assertEqual(set(item.jobs), {"reviewer"})

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
        marker = "__DEVC2_HERDR_" + "1" * 32 + "_EXIT_"
        item.jobs["tests"] = H.Job("tests", "w1:p2", "terminal-new", marker)
        item.herdr.output = f"some output\n{marker}17\n"
        self.assertEqual(item.handle({"op": "wait", "name": "tests", "timeout_ms": 1000}), {"exit_code": 17})

    def test_read_returns_only_worker_output(self):
        item = provider()
        marker = "__DEVC2_HERDR_" + "5" * 32 + "_EXIT_"
        item.jobs["clean"] = H.Job("clean", "w1:p2", "terminal-new", marker)
        item.herdr.output = (
            "exec /private/host/herdr worker PRIVATE_PAYLOAD\n"
            "devbox ➜ exec /private/host/herdr worker PRIVATE_PAYLOAD\n"
            f"{H.start_marker(marker)}\n"
            "worker output\n"
            f"{marker}0\n"
        )
        self.assertEqual(item.handle({"op": "read", "name": "clean", "lines": 10}), "worker output\n")
        self.assertNotIn("PRIVATE_PAYLOAD", item.handle({"op": "read", "name": "clean", "lines": 10}))

    def test_finished_worker_rejects_input(self):
        item = provider()
        marker = "__DEVC2_HERDR_" + "2" * 32 + "_EXIT_"
        item.jobs["done"] = H.Job("done", "w1:p2", "terminal-new", marker)
        item.herdr.output = marker + "0"
        with self.assertRaisesRegex(H.SpanError, "has exited"):
            item.handle({"op": "send", "name": "done", "text": "touch /tmp/host"})

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
        )
        for request in attempts:
            with self.subTest(request=request), self.assertRaises(H.SpanError):
                item.handle(request)

    def test_container_selection_requires_the_exact_oneoff_island_bind(self):
        item = provider()
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
        del item.island_container
        with mock.patch.object(H.subprocess, "run", side_effect=responses) as run:
            self.assertEqual(item.island_container(), "c" * 64)
        ps = run.call_args_list[0].args[0]
        self.assertIn("label=com.docker.compose.oneoff=True", ps)

        inspect[0]["Mounts"][0]["Source"] = "/other"
        responses = (
            subprocess.CompletedProcess([], 0, stdout="short-id\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=json.dumps(inspect), stderr=""),
        )
        with mock.patch.object(H.subprocess, "run", side_effect=responses), self.assertRaisesRegex(H.SpanError, "metadata"):
            item.island_container()

    def test_container_selection_accepts_only_exact_docker_desktop_workspace_encoding(self):
        item = provider()
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
        del item.island_container
        with mock.patch.object(H.sys, "platform", "darwin"), \
             mock.patch.object(H.subprocess, "run", side_effect=responses):
            self.assertEqual(item.island_container(), "d" * 64)

        inspect[0]["Mounts"][0]["Source"] = "/host_mnt/other"
        responses = (
            subprocess.CompletedProcess([], 0, stdout="short-id\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=json.dumps(inspect), stderr=""),
        )
        with mock.patch.object(H.sys, "platform", "darwin"), \
             mock.patch.object(H.subprocess, "run", side_effect=responses), \
             self.assertRaisesRegex(H.SpanError, "metadata"):
            item.island_container()

    def test_worker_uses_fixed_docker_options_and_parks(self):
        task = {
            "docker": "/bin/true", "container": "b" * 64,
            "argv": ["sh", "-lc", "echo $HOME; $(id)"],
            "marker": "__DEVC2_HERDR_" + "3" * 32 + "_EXIT_",
        }
        raw = base64.urlsafe_b64encode(json.dumps(task).encode()).decode()
        with mock.patch.dict(H.os.environ, {"REQUIRED_HOST_SETTING": "yes"}, clear=True), \
             mock.patch.object(H.subprocess, "run", return_value=mock.Mock(returncode=23)) as run, \
             mock.patch.object(H.signal, "signal"), \
             mock.patch.object(H.time, "sleep", side_effect=RuntimeError("parked")), \
             self.assertRaisesRegex(RuntimeError, "parked"):
            H.worker(raw)
        run.assert_called_once_with([
            "/bin/true", "exec", "--interactive", "--tty", "--user", "1000:1000",
            "--workdir", "/workspace", "b" * 64, "sh", "-lc", "echo $HOME; $(id)",
        ], check=False, env={"REQUIRED_HOST_SETTING": "yes", "DOCKER_CLI_HINTS": "false"})


class InstallerTests(unittest.TestCase):
    def test_installer_preserves_catalog_and_installs_immutable_generation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = root / "config"
            config.mkdir()
            catalog = config / "spans.json"
            catalog.write_text(json.dumps({"spans": {"other": {"provider": "/opt/p", "client": "/opt/c"}}}))
            catalog.chmod(0o600)
            completed = subprocess.run(
                [sys.executable, str(REGISTER), str(catalog), str(root / "spans"), str(PROVIDER_PATH)],
                text=True, capture_output=True, check=True,
            )
            self.assertIn("registered Herdr command Link", completed.stdout)
            document = json.loads(catalog.read_text())
            self.assertIn("other", document["spans"])
            world = Path(document["spans"]["herdr"])
            self.assertTrue(world.is_file())
            self.assertEqual(stat.S_IMODE(world.stat().st_mode), 0o555)
            self.assertEqual(set(world.parent.iterdir()), {world})


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
        for command in ("spawn", "list", "read", "send", "wait"):
            self.assertIn(command, output)

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
