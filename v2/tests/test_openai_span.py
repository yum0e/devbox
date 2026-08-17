from __future__ import annotations

import base64
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


def load(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


P = load(WORLD_PATH, "openai_world")


def receive(connection: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        block = connection.recv(size - len(result))
        if not block:
            raise RuntimeError("connection ended early")
        result.extend(block)
    return bytes(result)


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
        accepted = (
            (b"CONNECT chatgpt.com:443 HTTP/1.1\r\n"
             b"Host: chatgpt.com:443\r\n\r\nopaque", b"opaque"),
            (b"CONNECT chatgpt.com:443 HTTP/1.1\r\n"
             b"host: chatgpt.com\r\nconnection: close\r\n"
             b"proxy-connection: keep-alive\r\n\r\n", b""),
        )
        for payload, expected_trailing in accepted:
            with self.subTest(payload=payload):
                left, right = socket.socketpair()
                with left, right:
                    right.sendall(payload)
                    request, trailing = P.read_connect(left)
                self.assertTrue(request.endswith(b"\r\n\r\n"))
                self.assertEqual(trailing, expected_trailing)

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

    def test_invalid_tunnel_returns_a_bounded_proxy_error(self):
        client, world = socket.socketpair()
        manager = mock.Mock()
        worker = threading.Thread(target=P.handle, args=(world, manager))
        worker.start()
        with client:
            client.sendall(
                b"TCONNECT other.example:443 HTTP/1.1\r\n"
                b"Host: other.example:443\r\n\r\n"
            )
            response = client.recv(4096)
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(response.startswith(b"HTTP/1.1 400 Bad Request\r\n"))
        manager.get.assert_not_called()

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
            self.assertEqual(helper[helper.index("--memory") + 1], "128m")
            self.assertEqual(helper[helper.index("--pids-limit") + 1], "64")
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
            size = P.LENGTH.unpack(receive(right, P.LENGTH.size))[0]
            result = json.loads(receive(right, size))
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(set(result), {"v", "ca_b64", "files", "environment", "routes"})
        self.assertEqual(base64.b64decode(result["ca_b64"]), certificate)
        auth = json.loads(base64.b64decode(result["files"][0]["contents_b64"]))
        self.assertEqual(auth["tokens"]["access_token"], P.FAKE_ACCESS)
        self.assertEqual(auth["tokens"]["account_id"], P.FAKE_ACCOUNT)
        self.assertEqual(result["environment"]["DEVC2_PI_OPENAI_TOKEN"], P.FAKE_ACCESS)
        self.assertNotIn("HTTP_PROXY", result["environment"])
        self.assertNotIn("http_proxy", result["environment"])
        access_claims = json.loads(base64.urlsafe_b64decode(
            auth["tokens"]["access_token"].split(".")[1] + "=="
        ))
        self.assertEqual(
            access_claims["https://api.openai.com/auth"]["chatgpt_account_id"],
            P.FAKE_ACCOUNT,
        )
        self.assertNotIn(b"PRIVATE KEY", json.dumps(result).encode())
        self.assertEqual(result["routes"], ["chatgpt.com:443"])
