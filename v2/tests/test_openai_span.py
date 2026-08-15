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
PROVIDER_PATH = EXAMPLE / "openai-provider"
CLIENT_PATH = EXAMPLE / "openai"
REGISTER = EXAMPLE / "register.py"


def load(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


P = load(PROVIDER_PATH, "openai_span_provider")
C = load(CLIENT_PATH, "openai_span_client")


class ProviderTests(unittest.TestCase):
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

    def test_ca_control_returns_only_the_public_certificate(self):
        certificate = b"-----BEGIN CERTIFICATE-----\npublic\n-----END CERTIFICATE-----\n"
        manager = mock.Mock()
        manager.get.return_value = mock.Mock(certificate=certificate)
        left, right = socket.socketpair()
        thread = threading.Thread(target=P.handle, args=(left, manager))
        thread.start()
        with right:
            right.sendall(b"C")
            size = P.LENGTH.unpack(C.receive(right, P.LENGTH.size))[0]
            result = C.receive(right, size)
        thread.join(timeout=2)
        self.assertEqual(result, certificate)
        self.assertNotIn(b"PRIVATE KEY", result)


class ClientTests(unittest.TestCase):
    def test_help_needs_no_provider(self):
        result = subprocess.run([str(CLIENT_PATH), "--help"], text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertIn("openai run --", result.stdout)

    def test_fake_auth_matches_provider_placeholders(self):
        document = json.loads(C.fake_auth())
        self.assertEqual(document["tokens"]["access_token"], P.FAKE_ACCESS)
        self.assertEqual(document["tokens"]["account_id"], P.FAKE_ACCOUNT)
        self.assertEqual(document["tokens"]["refresh_token"], "not-a-real-refresh-token")

    def test_run_scopes_bootstrap_to_exact_child_and_removes_it(self):
        seen = {}

        class FakeAdapter:
            url = "http://127.0.0.1:43210"

            def __init__(self, path, fallback):
                seen["socket"] = path
                seen["fallback"] = fallback

            def close(self):
                seen["closed"] = True

        class FakeProcess:
            pid = 99999999

            def __init__(self, command, env, start_new_session):
                seen["command"] = command
                seen["env"] = env
                seen["root"] = Path(env["TACT_AUTH_FILE"]).parent
                seen["auth"] = Path(env["TACT_AUTH_FILE"]).read_bytes()
                self.returncode = None

            def wait(self, timeout=None):
                self.returncode = 0
                return 0

            def poll(self):
                return self.returncode

        with mock.patch.dict(C.os.environ, {"DEVC2_OPENAI_SOCKET": "/span.sock"}, clear=True), \
             mock.patch.object(C, "receive_ca", return_value=b"span-ca\n"), \
             mock.patch.object(C, "system_ca", return_value=b"system-ca\n"), \
             mock.patch.object(C, "Adapter", FakeAdapter), \
             mock.patch.object(C.subprocess, "Popen", FakeProcess):
            self.assertEqual(C.run(["tact", "run", "hello"]), 0)
        self.assertEqual(seen["command"], ["tact", "run", "hello"])
        self.assertEqual(seen["socket"], "/span.sock")
        self.assertIsNone(seen["fallback"])
        self.assertEqual(seen["env"]["HTTPS_PROXY"], FakeAdapter.url)
        self.assertEqual(seen["env"]["http_proxy"], FakeAdapter.url)
        self.assertIn("localhost", seen["env"]["NO_PROXY"])
        self.assertIn(".internal", seen["env"]["NO_PROXY"])
        self.assertEqual(json.loads(seen["auth"])["tokens"]["access_token"], P.FAKE_ACCESS)
        self.assertTrue(seen["closed"])
        self.assertFalse(seen["root"].exists())

    def test_loopback_adapter_selects_tunnel_and_preserves_bytes(self):
        with tempfile.TemporaryDirectory() as raw:
            path = str(Path(raw) / "span.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path)
            server.listen(1)
            observed = {}
            request = b"CONNECT chatgpt.com:443 HTTP/1.1\r\nHost: chatgpt.com:443\r\n\r\nhello"

            def provider():
                connection, _address = server.accept()
                with connection:
                    observed["operation"] = C.receive(connection, 1)
                    observed["request"] = C.receive(connection, len(request))
                    connection.sendall(b"world")

            thread = threading.Thread(target=provider)
            thread.start()
            adapter = C.Adapter(path)
            try:
                with socket.create_connection(("127.0.0.1", adapter.server.getsockname()[1]), timeout=2) as client:
                    client.sendall(request)
                    self.assertEqual(C.receive(client, 5), b"world")
            finally:
                adapter.close()
                server.close()
            thread.join(timeout=2)
            self.assertEqual(observed, {"operation": b"T", "request": request})

    def test_loopback_adapter_preserves_inherited_proxy_for_other_destinations(self):
        fallback = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        fallback.bind(("127.0.0.1", 0))
        fallback.listen(1)
        request = b"CONNECT github.com:443 HTTP/1.1\r\nHost: github.com:443\r\n\r\n"
        response = b"HTTP/1.1 200 Connection Established\r\n\r\n"
        observed = {}

        def proxy():
            connection, _address = fallback.accept()
            with connection:
                observed["request"] = C.receive(connection, len(request))
                connection.sendall(response)

        thread = threading.Thread(target=proxy)
        thread.start()
        adapter = C.Adapter(
            "/span-must-not-be-used.sock",
            f"http://127.0.0.1:{fallback.getsockname()[1]}",
        )
        try:
            with socket.create_connection(("127.0.0.1", adapter.server.getsockname()[1]), timeout=2) as client:
                client.sendall(request)
                self.assertEqual(C.receive(client, len(response)), response)
        finally:
            adapter.close()
            fallback.close()
        thread.join(timeout=2)
        self.assertEqual(observed["request"], request)


class InstallerTests(unittest.TestCase):
    def test_installer_preserves_catalog_and_installs_immutable_generation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            catalog = root / "config" / "spans.json"
            catalog.parent.mkdir()
            catalog.write_text(json.dumps({"spans": {"other": {"provider": "/p", "client": "/c"}}}))
            catalog.chmod(0o600)
            install_root = root / "installed"
            result = subprocess.run(
                [sys.executable, str(REGISTER), str(catalog), str(install_root), str(PROVIDER_PATH), str(CLIENT_PATH)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads(catalog.read_text())
            self.assertIn("other", document["spans"])
            entry = document["spans"]["openai"]
            for field in ("provider", "client"):
                path = Path(entry[field])
                self.assertTrue(path.is_file())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o555)


if __name__ == "__main__":
    unittest.main()
