from __future__ import annotations

import base64
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "spans" / "github"


def load(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def receive(connection: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        block = connection.recv(size - len(result))
        if not block:
            raise RuntimeError("response ended early")
        result.extend(block)
    return bytes(result)


W = load(EXAMPLE / "world", "github_world")


class WorldPolicyTests(unittest.TestCase):
    def read_connect(self, request: bytes):
        left, right = socket.socketpair()
        try:
            right.sendall(request)
            return W.read_connect(left)
        finally:
            left.close()
            right.close()

    def test_connect_allows_only_api_and_uploads(self):
        for target in (b"api.github.com:443", b"uploads.github.com:443"):
            request = b"CONNECT " + target + b" HTTP/1.1\r\nHost: " + target + b"\r\n\r\ntrailing"
            header, trailing = self.read_connect(request)
            self.assertEqual(header + trailing, request)

    def test_connect_rejects_git_web_and_ambiguous_headers(self):
        rejected = (
            b"CONNECT github.com:443 HTTP/1.1\r\nHost: github.com:443\r\n\r\n",
            b"CONNECT evilapi.github.com:443 HTTP/1.1\r\nHost: evilapi.github.com:443\r\n\r\n",
            b"CONNECT api.github.com:444 HTTP/1.1\r\nHost: api.github.com:444\r\n\r\n",
            b"CONNECT api.github.com:443 HTTP/1.1\r\nHost: uploads.github.com:443\r\n\r\n",
            b"CONNECT api.github.com:443 HTTP/1.1\r\nHost: api.github.com:443\r\nHost: api.github.com:443\r\n\r\n",
            b"CONNECT api.github.com:443 HTTP/1.1\r\n folded: bad\r\n\r\n",
        )
        for request in rejected:
            with self.subTest(request=request), self.assertRaises(W.ProviderError):
                self.read_connect(request)

    def test_proxy_policy_is_placeholder_only_and_has_no_git_https(self):
        sentinel = "random-lifetime-sentinel"
        document = W.proxy_config(sentinel)
        serialized = json.dumps(document)
        secret = document["transforms"][0]["config"]["secrets"][0]
        self.assertEqual(secret["replace"], {
            "proxy_value": sentinel,
            "match_headers": ["Authorization"],
            "require": True,
        })
        self.assertEqual(
            {rule["host"] for rule in secret["rules"]},
            {"api.github.com", "uploads.github.com"},
        )
        self.assertNotIn('"host": "github.com"', serialized)
        self.assertNotIn("real-token", serialized)
        self.assertEqual(secret["source"]["type"], "file")

    def test_bootstrap_is_public_bounded_and_scoped(self):
        certificate = b"-----BEGIN CERTIFICATE-----\npublic\n-----END CERTIFICATE-----\n"
        sentinel = "random-lifetime-sentinel"
        document = json.loads(W.bootstrap(certificate, sentinel))
        self.assertEqual(document["v"], 1)
        self.assertEqual(base64.b64decode(document["ca_b64"]), certificate)
        self.assertEqual(document["routes"], ["api.github.com:443", "uploads.github.com:443"])
        self.assertEqual(document["environment"]["GH_TOKEN"], sentinel)
        self.assertEqual(document["environment"]["GITHUB_TOKEN"], sentinel)
        self.assertNotIn("HTTP_PROXY", document["environment"])
        self.assertNotIn("http_proxy", document["environment"])
        hosts = base64.b64decode(document["files"][0]["contents_b64"])
        self.assertIn(sentinel.encode(), hosts)
        self.assertNotIn(b"real-token", json.dumps(document).encode())

    def test_bootstrap_control_returns_the_manifest(self):
        certificate = b"-----BEGIN CERTIFICATE-----\npublic\n-----END CERTIFICATE-----\n"
        manager = mock.Mock()
        manager.manifest.return_value = W.bootstrap(certificate, "lifetime-sentinel")
        server, client = socket.socketpair()
        thread = threading.Thread(target=W.handle, args=(server, manager))
        thread.start()
        try:
            client.sendall(b"B")
            size = W.LENGTH.unpack(receive(client, W.LENGTH.size))[0]
            document = json.loads(receive(client, size))
            self.assertEqual(base64.b64decode(document["ca_b64"]), certificate)
        finally:
            client.close()
            thread.join(1)

    def test_manifest_is_byte_stable_and_lifetimes_have_random_sentinels(self):
        certificate = b"-----BEGIN CERTIFICATE-----\npublic\n-----END CERTIFICATE-----\n"
        first = W.ProxyManager()
        second = W.ProxyManager()
        self.assertNotEqual(first.sentinel, second.sentinel)
        self.assertGreaterEqual(len(first.sentinel), 32)
        first.root = Path("/already/bootstrapped")
        first.certificate = certificate
        first.bootstrap_payload = W.bootstrap(certificate, first.sentinel)
        first.proxy = W.Proxy(first.root, "a" * 64, 1000, certificate)
        with mock.patch.object(first, "_running", return_value=True):
            original = first.manifest()
            self.assertIs(first.manifest(), original)
            self.assertEqual(first.manifest(), original)
        document = json.loads(original)
        self.assertEqual(document["environment"]["GH_TOKEN"], first.sentinel)
        self.assertNotEqual(document["environment"]["GH_TOKEN"], second.sentinel)


class WorldAuthTests(unittest.TestCase):
    def make_auth(self, root: Path) -> Path:
        directory = root / "gh"
        directory.mkdir(mode=0o700)
        (directory / "hosts.yml").write_text(
            "github.com:\n  user: devc2\n  oauth_token: real-token\n"
        )
        (directory / "hosts.yml").chmod(0o600)
        return directory

    def test_stale_private_roots_are_removed_only_when_safe(self):
        with tempfile.TemporaryDirectory() as raw:
            stale = Path(raw) / "devc2-github-deadbeef-generation"
            stale.mkdir(mode=0o700)
            (stale / "github-token").write_text("secret")
            with mock.patch.object(W.tempfile, "gettempdir", return_value=raw):
                W.cleanup_stale_roots("devc2-github-deadbeef-")
            self.assertFalse(stale.exists())

    def test_stale_helper_identity_is_stable_per_workspace(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            with mock.patch.dict(W.os.environ, {
                "DEVC2_WORKSPACE": str(workspace), "DEVC2_ISLAND_ID": "first",
            }, clear=True):
                first = W.ProxyManager.workspace_tag()
            with mock.patch.dict(W.os.environ, {
                "DEVC2_WORKSPACE": str(workspace), "DEVC2_ISLAND_ID": "second",
            }, clear=True):
                self.assertEqual(W.ProxyManager.workspace_tag(), first)

    def test_token_is_read_from_private_managed_file_without_helper(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = self.make_auth(Path(raw))
            with mock.patch.object(W, "auth_directory", return_value=directory), \
                    mock.patch.object(W, "run_docker") as run:
                self.assertEqual(W.read_token(), "real-token")
            run.assert_not_called()

    def test_auth_rejects_symlink_writable_and_invalid_token(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            directory = self.make_auth(root)
            (directory / "hosts.yml").chmod(0o622)
            with self.assertRaises(W.ProviderError):
                W.validate_auth(directory)
            (directory / "hosts.yml").unlink()
            target = root / "target"
            target.write_text("github.com: {}\n")
            (directory / "hosts.yml").symlink_to(target)
            with self.assertRaises(W.ProviderError):
                W.validate_auth(directory)
            (directory / "hosts.yml").unlink()
            (directory / "hosts.yml").write_text(
                "github.com:\n  oauth_token: secret with spaces\n"
            )
            (directory / "hosts.yml").chmod(0o600)
            with mock.patch.object(W, "auth_directory", return_value=directory):
                with self.assertRaisesRegex(W.ProviderError, "unavailable or invalid") as caught:
                    W.read_token()
            self.assertNotIn("secret", str(caught.exception))

    def test_helper_is_hardened_and_token_is_file_only(self):
        source = (EXAMPLE / "world").read_text()
        for value in (
            '"--read-only"', '"--cap-drop", "ALL"', '"no-new-privileges:true"',
            '"--log-driver", "none"', '"127.0.0.1::8080"', '"--pids-limit"',
        ):
            self.assertIn(value, source)
        self.assertIn('private_replace(root / "github-token", token.encode())', source)
        self.assertIn('"--memory", "128m"', source)
        self.assertIn('"--pids-limit", "64"', source)
        self.assertNotIn('"--env", "GH_TOKEN=" + token', source)
        self.assertNotIn('PLACEHOLDER =', source)

    def test_docker_environment_excludes_host_credentials(self):
        with mock.patch.dict(W.os.environ, {
            "PATH": "/bin", "HOME": "/home/test", "GH_TOKEN": "secret",
            "GITHUB_TOKEN": "secret-two", "AWS_SECRET_ACCESS_KEY": "secret-three",
        }, clear=True):
            self.assertEqual(W.docker_environment(), {"PATH": "/bin", "HOME": "/home/test"})

    def test_container_cleanup_requires_verified_absence(self):
        identifier = "a" * 64
        responses = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        with mock.patch.object(W, "run_docker", side_effect=responses):
            self.assertTrue(W.remove_container(identifier))
        present = subprocess.CompletedProcess([], 0, identifier + "\n", "")
        with mock.patch.object(W, "run_docker", return_value=present):
            self.assertFalse(W.remove_container(identifier))
        self.assertFalse(W.remove_container("not-an-id"))

    def test_proxy_retains_secret_state_when_cleanup_fails(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "secret-state"
            root.mkdir()
            proxy = W.Proxy(root, "a" * 64, 1234, b"certificate")
            with mock.patch.object(W, "remove_container", return_value=False), \
                    self.assertRaises(W.ProviderError):
                proxy.stop()
            self.assertTrue(root.exists())

    def test_secret_state_cleanup_is_verified(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "secret-state"
            root.mkdir()
            with mock.patch.object(W.shutil, "rmtree", side_effect=OSError("busy")), \
                    self.assertRaisesRegex(W.ProviderError, "credential cleanup failed"):
                W.remove_private_root(root)
            self.assertTrue(root.exists())

    def test_helper_restarts_with_refreshed_auth_and_stable_identity(self):
        certificate = b"-----BEGIN CERTIFICATE-----\npublic\n-----END CERTIFICATE-----\n"
        with tempfile.TemporaryDirectory() as raw:
            manager = W.ProxyManager()
            manager.root = Path(raw)
            manager.certificate = certificate
            manager.bootstrap_payload = W.bootstrap(certificate, manager.sentinel)
            manager.proxy = W.Proxy(manager.root, "a" * 64, 1000, certificate)
            with mock.patch.object(manager, "_running", return_value=True):
                before = manager.manifest()
            responses = (
                subprocess.CompletedProcess([], 0, "false\n", ""),
                subprocess.CompletedProcess([], 0, "b" * 64 + "\n", ""),
                subprocess.CompletedProcess([], 0, "127.0.0.1:2000\n", ""),
            )
            connection = mock.MagicMock()
            with mock.patch.object(W, "run_docker", side_effect=responses), \
                    mock.patch.object(W, "remove_container", return_value=True), \
                    mock.patch.object(W, "read_token", return_value="refreshed-token") as read_token, \
                    mock.patch.object(W, "private_replace") as replace, \
                    mock.patch.object(manager, "workspace_tag", return_value="workspace-tag"), \
                    mock.patch.object(W.socket, "create_connection", return_value=connection):
                recovered = manager.get()
            read_token.assert_called_once_with()
            replace.assert_called_once_with(manager.root / "github-token", b"refreshed-token")
            self.assertEqual(recovered.container, "b" * 64)
            self.assertEqual(recovered.certificate, certificate)
            with mock.patch.object(manager, "_running", return_value=True):
                self.assertIs(manager.manifest(), before)
            self.assertEqual(manager.sentinel, json.loads(before)["environment"]["GH_TOKEN"])

    def test_helper_recovery_fails_closed_on_unknown_state_or_cleanup_failure(self):
        manager = W.ProxyManager()
        proxy = W.Proxy(Path("/private/lifetime"), "a" * 64, 1000, b"certificate")
        unknown = subprocess.CompletedProcess([], 1, "", "inspect failed")
        present = subprocess.CompletedProcess([], 0, proxy.container + "\n", "")
        with mock.patch.object(W, "run_docker", side_effect=(unknown, present)), \
                self.assertRaisesRegex(W.ProviderError, "verify GitHub helper exit"):
            manager._running(proxy)

        exited = subprocess.CompletedProcess([], 0, "false\n", "")
        with mock.patch.object(W, "run_docker", return_value=exited), \
                mock.patch.object(W, "remove_container", return_value=False), \
                self.assertRaisesRegex(W.ProviderError, "cleanup could not be verified"):
            manager._running(proxy)


class FileContractTests(unittest.TestCase):
    def test_world_is_the_only_executable_asset(self):
        world = EXAMPLE / "world"
        self.assertTrue(world.stat().st_mode & stat.S_IXUSR)
        self.assertLess(world.stat().st_size, 64 * 1024 * 1024)
        self.assertEqual(
            {path.name for path in EXAMPLE.iterdir() if path.is_file()},
            {"README.md", "world"},
        )


if __name__ == "__main__":
    unittest.main()
