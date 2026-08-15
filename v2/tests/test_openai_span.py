from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "openai-span"
PRODUCTION = ROOT / "spans" / "openai"
WORLD_PATH = PRODUCTION / "world"
PROJECTION_PATH = ROOT / "launcher" / "scoped_exec_projection.py"


def load(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


P = load(WORLD_PATH, "openai_world")
C = load(PROJECTION_PATH, "scoped_exec_projection")


class WorldTests(unittest.TestCase):
    def test_stale_private_roots_are_removed_only_when_safe(self):
        with tempfile.TemporaryDirectory() as raw:
            temporary = Path(raw)
            stale = temporary / "devc2-openai-deadbeef-generation"
            stale.mkdir(mode=0o700)
            (stale / "access-token").write_text("secret")
            with mock.patch.object(P.tempfile, "gettempdir", return_value=raw):
                P.cleanup_stale_roots("devc2-openai-deadbeef-")
            self.assertFalse(stale.exists())

    def valid_auth(self) -> dict:
        return {
            "tokens": {
                "access_token": P.jwt({"exp": time.time() + 7200}),
                "account_id": "account-one",
            }
        }

    def test_auth_is_bounded_owned_nofollow_and_never_returned_in_config(self):
        with tempfile.TemporaryDirectory() as raw:
            auth = Path(raw) / "auth.json"
            auth.write_text(json.dumps(self.valid_auth()))
            auth.chmod(0o600)
            with mock.patch.dict(P.os.environ, {"DEVC2_OPENAI_AUTH_FILE": str(auth)}, clear=True):
                access, account = P.read_auth()
            self.assertEqual(account, "account-one")
            self.assertNotIn(access, json.dumps(P.proxy_config()))
            self.assertEqual(P.proxy_config()["transforms"][0]["config"]["secrets"][0]["source"]["type"], "file")

            auth.chmod(0o620)
            with mock.patch.dict(P.os.environ, {"DEVC2_OPENAI_AUTH_FILE": str(auth)}, clear=True), \
                 self.assertRaisesRegex(P.ProviderError, "regular file"):
                P.read_auth()

            auth.chmod(0o600)
            link = Path(raw) / "link.json"
            link.symlink_to(auth)
            with mock.patch.dict(P.os.environ, {"DEVC2_OPENAI_AUTH_FILE": str(link)}, clear=True), \
                 self.assertRaises(P.ProviderError):
                P.read_auth()

    def test_auth_rejects_near_expiry_and_malformed_values(self):
        documents = (
            {"tokens": {"access_token": P.jwt({"exp": time.time() + 10}), "account_id": "a"}},
            {"tokens": {"access_token": "not-a-jwt", "account_id": "a"}},
            {"tokens": {"access_token": P.jwt({"exp": time.time() + 7200}), "account_id": "bad account"}},
        )
        for document in documents:
            with self.subTest(document=document), tempfile.TemporaryDirectory() as raw:
                auth = Path(raw) / "auth.json"
                auth.write_text(json.dumps(document))
                auth.chmod(0o600)
                with mock.patch.dict(P.os.environ, {"DEVC2_OPENAI_AUTH_FILE": str(auth)}, clear=True), \
                     self.assertRaises(P.ProviderError):
                    P.read_auth()

    def test_docker_subprocess_environment_excludes_host_credentials(self):
        inherited = {
            "PATH": "/trusted/bin",
            "HOME": "/host/home",
            "DOCKER_HOST": "unix:///trusted/docker.sock",
            "XDG_RUNTIME_DIR": "/trusted/runtime",
            "SSH_AUTH_SOCK": "/host/agent.sock",
            "GH_TOKEN": "github-secret",
            "OPENAI_API_KEY": "openai-secret",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
        }
        with mock.patch.dict(P.os.environ, inherited, clear=True):
            environment = P.docker_environment()
        self.assertEqual(environment, {
            "PATH": "/trusted/bin",
            "HOME": "/host/home",
            "DOCKER_HOST": "unix:///trusted/docker.sock",
            "XDG_RUNTIME_DIR": "/trusted/runtime",
        })

    def test_connect_scope_is_exact_and_rejects_header_ambiguity(self):
        accepted = b"CONNECT chatgpt.com:443 HTTP/1.1\r\nHost: chatgpt.com:443\r\n\r\nopaque"
        left, right = socket.socketpair()
        with left, right:
            right.sendall(accepted)
            request, trailing = P.read_connect(left)
        self.assertTrue(request.endswith(b"\r\n\r\n"))
        self.assertEqual(trailing, b"opaque")

        rejected = (
            b"CONNECT api.openai.com:443 HTTP/1.1\r\n\r\n",
            b"CONNECT chatgpt.com:80 HTTP/1.1\r\n\r\n",
            b"GET https://chatgpt.com/ HTTP/1.1\r\n\r\n",
            b"CONNECT chatgpt.com:443 HTTP/1.1\r\nHost: chatgpt.com:443\r\nHost: chatgpt.com:443\r\n\r\n",
            b"CONNECT chatgpt.com:443 HTTP/1.1\r\nHost: other:443\r\n\r\n",
        )
        for payload in rejected:
            with self.subTest(payload=payload):
                left, right = socket.socketpair()
                with left, right:
                    right.sendall(payload)
                    with self.assertRaises(P.ProviderError):
                        P.read_connect(left)

    def test_lazy_helper_is_pinned_hardened_scoped_and_contains_secrets_only_in_files(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "state"
            root.mkdir()
            calls = []

            def docker(arguments, timeout=120):
                calls.append(arguments)
                if arguments[:2] == ["ps", "--all"]:
                    return subprocess.CompletedProcess(arguments, 0, "", "")
                if "generate-ca" in arguments:
                    (root / "ca.crt").write_bytes(b"-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----\n")
                    (root / "ca.key").write_bytes(b"private")
                    return subprocess.CompletedProcess(arguments, 0, "", "")
                if arguments[:3] == ["run", "--pull", "never"] and "--detach" in arguments:
                    return subprocess.CompletedProcess(arguments, 0, "c" * 64 + "\n", "")
                if arguments[:2] == ["port", "c" * 64]:
                    return subprocess.CompletedProcess(arguments, 0, "127.0.0.1:43123\n", "")
                raise AssertionError(arguments)

            manager = P.ProxyManager()
            with mock.patch.dict(P.os.environ, {"DEVC2_WORKSPACE": "/workspace"}, clear=True), \
                 mock.patch.object(P.tempfile, "mkdtemp", return_value=str(root)), \
                 mock.patch.object(P, "read_auth", return_value=("real-secret", "real-account")), \
                 mock.patch.object(P.socket, "create_connection", return_value=mock.MagicMock()), \
                 mock.patch.object(P, "remove_named_container"), \
                 mock.patch.object(P, "run_docker", side_effect=docker):
                proxy = manager._start()
            self.assertEqual(proxy.port, 43123)
            launches = [call for call in calls if call[:3] == ["run", "--pull", "never"]]
            self.assertEqual(len(launches), 2)
            helper = next(call for call in launches if "--detach" in call)
            for option in ("--read-only", "--cap-drop", "--security-opt", "--log-driver"):
                self.assertIn(option, helper)
            self.assertIn("127.0.0.1::8080", helper)
            config = (root / "proxy.yaml").read_text()
            self.assertNotIn("real-secret", config)
            self.assertNotIn("real-account", config)
            self.assertEqual(stat.S_IMODE((root / "access-token").stat().st_mode), 0o600)
            with mock.patch.object(P, "remove_container") as remove:
                remove.return_value = True
                proxy.stop()
            remove.assert_called_once_with("c" * 64)
            self.assertFalse(root.exists())

    def test_helper_identity_and_secret_state_are_retained_when_cleanup_cannot_be_verified(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "secrets"
            root.mkdir()
            (root / "access-token").write_text("secret")
            proxy = P.Proxy(root, "e" * 64, 1234, b"certificate")
            with mock.patch.object(P, "remove_container", return_value=False), \
                 self.assertRaisesRegex(P.ProviderError, "cleanup"):
                proxy.stop()
            self.assertTrue(root.is_dir())
            self.assertEqual(proxy.container, "e" * 64)

    def test_secret_state_is_retained_when_directory_cleanup_fails(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "secrets"
            root.mkdir()
            proxy = P.Proxy(root, "e" * 64, 1234, b"certificate")
            with mock.patch.object(P, "remove_container", return_value=True), \
                 mock.patch.object(P.shutil, "rmtree", side_effect=OSError("busy")), \
                 self.assertRaisesRegex(P.ProviderError, "credential cleanup"):
                proxy.stop()
            self.assertTrue(root.is_dir())

    def test_stale_helper_discovery_failure_is_not_treated_as_empty(self):
        manager = P.ProxyManager()
        failed = subprocess.CompletedProcess([], 1, "", "no")
        with mock.patch.object(P, "run_docker", return_value=failed), \
             self.assertRaisesRegex(P.ProviderError, "inspect stale"):
            manager._remove_stale("a" * 16)

    def test_container_removal_requires_a_successful_absence_query(self):
        removal = subprocess.CompletedProcess([], 1, "", "failed")
        unavailable = subprocess.CompletedProcess([], 1, "", "daemon unavailable")
        with mock.patch.object(P, "run_docker", side_effect=[removal, unavailable] * 3):
            self.assertFalse(P.remove_container("f" * 64))

        absent = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(P, "run_docker", side_effect=[removal, absent]):
            self.assertTrue(P.remove_container("f" * 64))

    def test_bootstrap_returns_only_public_and_fake_material(self):
        certificate = b"-----BEGIN CERTIFICATE-----\npublic\n-----END CERTIFICATE-----\n"
        manager = mock.Mock()
        manager.get.return_value = mock.Mock(certificate=certificate)
        left, right = socket.socketpair()
        thread = threading.Thread(target=P.handle, args=(left, manager))
        thread.start()
        with right:
            right.sendall(b"B")
            size = P.LENGTH.unpack(C.receive(right, P.LENGTH.size))[0]
            result = json.loads(C.receive(right, size))
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(set(result), {"v", "ca_b64", "files", "environment", "routes"})
        self.assertEqual(C.base64.b64decode(result["ca_b64"]), certificate)
        auth = json.loads(C.base64.b64decode(result["files"][0]["contents_b64"]))
        self.assertEqual(auth["tokens"]["access_token"], P.FAKE_ACCESS)
        self.assertEqual(auth["tokens"]["account_id"], P.FAKE_ACCOUNT)
        self.assertNotIn(b"PRIVATE KEY", json.dumps(result).encode())
        self.assertEqual(result["routes"], ["chatgpt.com:443"])


class ScopedExecLinkTests(unittest.TestCase):
    def manifest(self):
        return (
            b"public-ca",
            [("auth.json", P.fake_auth()), ("codex/auth.json", P.fake_auth())],
            {
                "HTTPS_PROXY": "${PROXY}",
                "TACT_AUTH_FILE": "${ROOT}/auth.json",
                "CODEX_HOME": "${ROOT}/codex",
                "SSL_CERT_FILE": "${CA}",
            },
            {"chatgpt.com:443"},
        )

    def test_help_is_local_and_projection_has_no_capability_semantics(self):
        with tempfile.TemporaryDirectory() as raw:
            projected = Path(raw) / "openai"
            projected.symlink_to(PROJECTION_PATH)
            result = subprocess.run([str(projected), "--help"], text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertIn("openai run --", result.stdout)
        source = PROJECTION_PATH.read_bytes().lower()
        for term in (b"openai", b"chatgpt", b"tact", b"codex"):
            self.assertNotIn(term, source)
        legacy = PRODUCTION / "client"
        self.assertTrue(legacy.is_file())
        self.assertEqual(stat.S_IMODE(legacy.stat().st_mode), 0o644)
        self.assertIn("Never projected", legacy.read_text())

    def test_run_materializes_manifest_for_exact_child_and_removes_it(self):
        seen = {}

        class FakeAdapter:
            url = "http://127.0.0.1:43210"

            def __init__(self, path, routes, fallback):
                seen.update(socket=path, routes=routes, fallback=fallback)

            def close(self):
                seen["closed"] = True

        class FakeProcess:
            pid = 99999999

            def __init__(self, command, env, start_new_session):
                seen["command"] = command
                seen["env"] = env
                seen["root"] = Path(env["TACT_AUTH_FILE"]).parent
                seen["auth"] = Path(env["TACT_AUTH_FILE"]).read_bytes()
                seen["mode"] = stat.S_IMODE(Path(env["TACT_AUTH_FILE"]).stat().st_mode)
                self.returncode = None

            def wait(self, timeout=None):
                self.returncode = 0
                return 0

            def poll(self):
                return self.returncode

        with mock.patch.dict(C.os.environ, {"HOME": "/island/home"}, clear=True), \
             mock.patch.object(C, "bootstrap", return_value=self.manifest()), \
             mock.patch.object(C, "system_ca", return_value=b"system-ca\n"), \
             mock.patch.object(C, "Adapter", FakeAdapter), \
             mock.patch.object(C.subprocess, "Popen", FakeProcess):
            self.assertEqual(C.run(["tact", "run", "hello"], "/span.sock"), 0)
        self.assertEqual(seen["command"], ["tact", "run", "hello"])
        self.assertEqual(seen["socket"], "/span.sock")
        self.assertEqual(seen["routes"], {"chatgpt.com:443"})
        self.assertEqual(seen["env"]["HOME"], "/island/home")
        self.assertEqual(seen["env"]["HTTPS_PROXY"], FakeAdapter.url)
        self.assertEqual(json.loads(seen["auth"])["tokens"]["access_token"], P.FAKE_ACCESS)
        self.assertEqual(seen["mode"], 0o600)
        self.assertTrue(seen["closed"])
        self.assertFalse(seen["root"].exists())

    def test_loopback_adapter_selects_declared_world_route(self):
        with tempfile.TemporaryDirectory() as raw:
            path = str(Path(raw) / "span.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path); server.listen(1)
            observed = {}
            request = b"CONNECT chatgpt.com:443 HTTP/1.1\r\nHost: chatgpt.com:443\r\n\r\nhello"

            def world():
                connection, _address = server.accept()
                with connection:
                    observed["operation"] = C.receive(connection, 1)
                    observed["request"] = C.receive(connection, len(request))
                    connection.sendall(b"world")

            thread = threading.Thread(target=world); thread.start()
            adapter = C.Adapter(path, {"chatgpt.com:443"}, None)
            try:
                with socket.create_connection(("127.0.0.1", adapter.server.getsockname()[1]), timeout=2) as client:
                    client.sendall(request)
                    self.assertEqual(C.receive(client, 5), b"world")
            finally:
                adapter.close(); server.close()
            thread.join(timeout=2)
            self.assertEqual(observed, {"operation": b"T", "request": request})

    def test_undeclared_route_preserves_inherited_proxy(self):
        fallback = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        fallback.bind(("127.0.0.1", 0)); fallback.listen(1)
        request = b"CONNECT github.com:443 HTTP/1.1\r\nHost: github.com:443\r\n\r\n"
        response = b"HTTP/1.1 200 Connection Established\r\n\r\n"
        observed = {}

        def proxy():
            connection, _address = fallback.accept()
            with connection:
                observed["request"] = C.receive(connection, len(request))
                connection.sendall(response)

        thread = threading.Thread(target=proxy); thread.start()
        adapter = C.Adapter("/must-not-be-used.sock", {"chatgpt.com:443"}, fallback.getsockname())
        try:
            with socket.create_connection(("127.0.0.1", adapter.server.getsockname()[1]), timeout=2) as client:
                client.sendall(request)
                self.assertEqual(C.receive(client, len(response)), response)
        finally:
            adapter.close(); fallback.close()
        thread.join(timeout=2)
        self.assertEqual(observed["request"], request)

    def test_manifest_and_connect_validation_fail_closed(self):
        certificate = b"-----BEGIN CERTIFICATE-----\npublic\n-----END CERTIFICATE-----\n"
        valid = {
            "v": 1,
            "ca_b64": C.base64.b64encode(certificate).decode(),
            "files": [{"name": "private/config.json", "contents_b64": "e30="}],
            "environment": {"CONFIG": "${ROOT}/private/config.json", "CA": "${CA}"},
            "routes": ["service.example:443"],
        }
        with mock.patch.object(C.ssl, "create_default_context"):
            C.validate_manifest(valid)
            invalid = dict(valid, extra=True)
            with self.assertRaises(C.LinkError):
                C.validate_manifest(invalid)
            invalid = dict(valid, files=[{"name": "../escape", "contents_b64": "e30="}])
            with self.assertRaises(C.LinkError):
                C.validate_manifest(invalid)
            invalid = dict(valid, routes=["user@service.example:443"])
            with self.assertRaises(C.LinkError):
                C.validate_manifest(invalid)
        with self.assertRaises(C.LinkError):
            C.child_environment({"X": "${UNKNOWN}"}, Path("/root"), Path("/ca"), "http://proxy")
        for line in (b"GET / HTTP/1.1", b"CONNECT example.com:80 HTTP/1.1", b"CONNECT user@example.com:443 HTTP/1.1"):
            with self.subTest(line=line), self.assertRaises(C.LinkError):
                C.connect_target(line)

    def test_bootstrap_failure_never_launches_the_child(self):
        with mock.patch.object(C, "bootstrap", side_effect=C.LinkError("denied")), \
             mock.patch.object(C.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(C.LinkError, "denied"):
                C.run(["must-not-run"], "/missing.sock")
        launch.assert_not_called()

    def test_invalid_inherited_https_proxy_never_falls_back(self):
        with mock.patch.dict(C.os.environ, {
            "HTTPS_PROXY": "http://user:secret@proxy.example:3128",
            "HTTP_PROXY": "http://other.example:8080",
        }, clear=True), self.assertRaisesRegex(C.LinkError, "HTTPS proxy"):
            C.inherited_proxy()

    def test_descendant_group_cleanup_and_sigkill_status_are_explicit(self):
        with mock.patch.object(C, "process_group_exists", side_effect=[True, False]), \
             mock.patch.object(C.os, "killpg") as kill_group:
            C.terminate_process_group(1234)
        kill_group.assert_called_once_with(1234, C.signal.SIGTERM)

        with mock.patch.object(C.signal, "signal") as reset, \
             mock.patch.object(C.os, "kill") as kill:
            self.assertEqual(C.preserve_signal_status(-C.signal.SIGKILL), 128 + C.signal.SIGKILL)
        reset.assert_not_called()
        kill.assert_called_once_with(C.os.getpid(), C.signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
