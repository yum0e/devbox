from __future__ import annotations

import importlib.machinery
import importlib.util
import io
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


P = load(EXAMPLE / "provider", "github_span_provider")
C = load(EXAMPLE / "client", "github_span_client")


class ProviderPolicyTests(unittest.TestCase):
    def read_connect(self, request: bytes):
        left, right = socket.socketpair()
        try:
            right.sendall(request)
            return P.read_connect(left)
        finally:
            left.close()
            right.close()

    def test_connect_allows_only_api_and_uploads(self):
        for target in (b"api.github.com:443", b"uploads.github.com:443"):
            request = b"CONNECT " + target + b" HTTP/1.1\r\nHost: " + target + b"\r\n\r\ntrailing"
            header, trailing = self.read_connect(request)
            self.assertEqual(header + trailing, request)
            self.assertEqual(trailing, b"trailing")

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
            with self.subTest(request=request), self.assertRaises(P.ProviderError):
                self.read_connect(request)

    def test_proxy_policy_is_placeholder_only_and_has_no_git_https(self):
        document = P.proxy_config()
        serialized = json.dumps(document)
        secret = document["transforms"][0]["config"]["secrets"][0]
        self.assertEqual(secret["replace"], {
            "proxy_value": P.PLACEHOLDER, "match_headers": ["Authorization"], "require": True,
        })
        self.assertEqual({rule["host"] for rule in secret["rules"]}, {"api.github.com", "uploads.github.com"})
        self.assertNotIn('"host": "github.com"', serialized)
        self.assertNotIn("real-token", serialized)
        self.assertEqual(secret["source"]["type"], "file")

    def test_ca_control_returns_only_public_certificate(self):
        certificate = b"-----BEGIN CERTIFICATE-----\npublic\n-----END CERTIFICATE-----\n"
        manager = mock.Mock()
        manager.get.return_value.certificate = certificate
        server, client = socket.socketpair()
        thread = __import__("threading").Thread(target=P.handle, args=(server, manager))
        thread.start()
        try:
            client.sendall(b"C")
            size = P.LENGTH.unpack(C.receive(client, P.LENGTH.size))[0]
            self.assertEqual(C.receive(client, size), certificate)
        finally:
            client.close()
            thread.join(1)


class ProviderAuthTests(unittest.TestCase):
    def test_stale_private_roots_are_removed_only_when_safe(self):
        with tempfile.TemporaryDirectory() as raw:
            temporary = Path(raw)
            stale = temporary / "devc2-github-deadbeef-generation"
            stale.mkdir(mode=0o700)
            (stale / "github-token").write_text("secret")
            with mock.patch.object(P.tempfile, "gettempdir", return_value=raw):
                P.cleanup_stale_roots("devc2-github-deadbeef-")
            self.assertFalse(stale.exists())

    def test_stale_helper_identity_is_stable_per_workspace(self):
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            with mock.patch.dict(P.os.environ, {"DEVC2_WORKSPACE": str(workspace), "DEVC2_ISLAND_ID": "first"}, clear=True):
                first = P.ProxyManager.workspace_tag()
            with mock.patch.dict(P.os.environ, {"DEVC2_WORKSPACE": str(workspace), "DEVC2_ISLAND_ID": "second"}, clear=True):
                self.assertEqual(P.ProxyManager.workspace_tag(), first)

    def make_auth(self, root: Path) -> Path:
        directory = root / "gh"
        directory.mkdir(mode=0o700)
        (directory / "hosts.yml").write_text("github.com:\n  user: devc2\n  oauth_token: real-token\n")
        (directory / "hosts.yml").chmod(0o600)
        return directory

    def test_token_is_read_from_the_private_managed_file_without_a_helper(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = self.make_auth(Path(raw))
            with mock.patch.object(P, "auth_directory", return_value=directory), mock.patch.object(P, "run_docker") as run:
                self.assertEqual(P.read_token(), "real-token")
            run.assert_not_called()

    def test_auth_rejects_symlink_and_writable_file(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            directory = self.make_auth(root)
            (directory / "hosts.yml").chmod(0o622)
            with self.assertRaises(P.ProviderError):
                P.validate_auth(directory)
            (directory / "hosts.yml").unlink()
            target = root / "target"
            target.write_text("github.com: {}\n")
            (directory / "hosts.yml").symlink_to(target)
            with self.assertRaises(P.ProviderError):
                P.validate_auth(directory)

    def test_invalid_token_is_not_echoed_in_error(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = self.make_auth(Path(raw))
            (directory / "hosts.yml").write_text("github.com:\n  oauth_token: secret with spaces\n")
            with mock.patch.object(P, "auth_directory", return_value=directory):
                with self.assertRaisesRegex(P.ProviderError, "unavailable or invalid") as caught:
                    P.read_token()
            self.assertNotIn("secret", str(caught.exception))

    def test_helper_launch_is_hardened_and_token_is_file_only(self):
        source = (EXAMPLE / "provider").read_text()
        for value in ('"--read-only"', '"--cap-drop", "ALL"', '"no-new-privileges:true"',
                      '"--log-driver", "none"', '"127.0.0.1::8080"', '"--pids-limit"'):
            self.assertIn(value, source)
        self.assertIn('private_write(root / "github-token", token.encode())', source)
        self.assertNotIn('"--env", "GH_TOKEN=" + token', source)

    def test_docker_environment_excludes_host_credentials(self):
        with mock.patch.dict(P.os.environ, {
            "PATH": "/bin", "HOME": "/home/test", "GH_TOKEN": "secret",
            "GITHUB_TOKEN": "secret-two", "AWS_SECRET_ACCESS_KEY": "secret-three",
        }, clear=True):
            self.assertEqual(P.docker_environment(), {"PATH": "/bin", "HOME": "/home/test"})

    def test_container_cleanup_requires_verified_absence(self):
        identifier = "a" * 64
        responses = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        with mock.patch.object(P, "run_docker", side_effect=responses):
            self.assertTrue(P.remove_container(identifier))
        present = subprocess.CompletedProcess([], 0, identifier + "\n", "")
        with mock.patch.object(P, "run_docker", return_value=present):
            self.assertFalse(P.remove_container(identifier))
        self.assertFalse(P.remove_container("not-an-id"))

    def test_proxy_retains_secret_state_when_cleanup_fails(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "secret-state"
            root.mkdir()
            proxy = P.Proxy(root, "a" * 64, 1234, b"certificate")
            with mock.patch.object(P, "remove_container", return_value=False), self.assertRaises(P.ProviderError):
                proxy.stop()
            self.assertTrue(root.exists())


class ClientTests(unittest.TestCase):
    def test_help_needs_no_span(self):
        with mock.patch.object(C, "run") as run, mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(C.main(["--help"]), 0)
            run.assert_not_called()

    def test_child_environment_is_placeholder_scoped_and_ssh_git(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ca, config = root / "ca", root / "gh"
            config.mkdir()
            with mock.patch.dict(C.os.environ, {"GITHUB_TOKEN": "ambient-secret", "GH_TOKEN": "other"}, clear=True):
                environment = C.child_environment(ca, config, "http://127.0.0.1:1234")
            self.assertNotIn("GITHUB_TOKEN", environment)
            self.assertEqual(environment["GH_TOKEN"], C.PLACEHOLDER)
            self.assertEqual(environment["GH_CONFIG_DIR"], str(config))
            self.assertEqual(environment["HTTPS_PROXY"], "http://127.0.0.1:1234")

    def test_run_projects_placeholder_config_and_cleans_up(self):
        seen = {}
        class Adapter:
            url = "http://127.0.0.1:4321"
            def __init__(self, socket_path, fallback):
                seen["socket"] = socket_path
                seen["fallback"] = fallback
            def stop(self):
                seen["stopped"] = True
        class Child:
            pid = 999999
            def poll(self): return 0
            def wait(self): return 7
        def popen(command, env, start_new_session):
            seen["command"] = command
            seen["environment"] = env
            config = Path(env["GH_CONFIG_DIR"])
            seen["hosts"] = (config / "hosts.yml").read_text()
            seen["root"] = config.parent
            return Child()
        certificate = b"-----BEGIN CERTIFICATE-----\nspan\n-----END CERTIFICATE-----\n"
        with mock.patch.dict(C.os.environ, {"DEVC2_GITHUB_SOCKET": "/span.sock", "HTTPS_PROXY": "http://127.0.0.1:8080"}, clear=True), \
             mock.patch.object(C, "receive_ca", return_value=certificate), \
             mock.patch.object(C, "system_ca", return_value=b"-----BEGIN CERTIFICATE-----\nsystem\n-----END CERTIFICATE-----\n"), \
             mock.patch.object(C, "Adapter", Adapter), mock.patch.object(C.subprocess, "Popen", side_effect=popen):
            self.assertEqual(C.run(["gh", "api", "user"]), 7)
        self.assertEqual(seen["command"], ["gh", "api", "user"])
        self.assertIn("git_protocol: ssh", seen["hosts"])
        self.assertIn(C.PLACEHOLDER, seen["hosts"])
        self.assertNotIn("real-token", seen["hosts"])
        self.assertTrue(seen["stopped"])
        self.assertFalse(seen["root"].exists())

    def test_adapter_routes_only_exact_api_targets(self):
        self.assertEqual(C.Adapter._fallback("http://127.0.0.1:8080"), ("127.0.0.1", 8080))
        for invalid in ("https://proxy:443", "http://user:pass@proxy:80", "http://proxy/path"):
            with self.assertRaises(C.ClientError):
                C.Adapter._fallback(invalid)

    def test_direct_fallback_accepts_only_uncredentialed_https_connect(self):
        self.assertEqual(C.Adapter._direct(b"objects.githubusercontent.com:443"), ("objects.githubusercontent.com", 443))
        for target in (None, b"example.com:80", b"user@example.com:443", b"example.com:not-a-port"):
            with self.subTest(target=target), self.assertRaises(C.ClientError):
                C.Adapter._direct(target)
        self.assertEqual(C.TARGETS, {b"api.github.com:443", b"uploads.github.com:443"})
        self.assertNotIn(b"github.com:443", C.TARGETS)

    def test_adapter_selects_span_for_api_and_fallback_for_git_web(self):
        with tempfile.TemporaryDirectory() as raw:
            unix_path = str(Path(raw) / "span.sock")
            unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            unix.bind(unix_path)
            unix.listen(1)
            fallback = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            fallback.bind(("127.0.0.1", 0))
            fallback.listen(1)
            adapter = C.Adapter(unix_path, f"http://127.0.0.1:{fallback.getsockname()[1]}")
            try:
                api_request = b"CONNECT api.github.com:443 HTTP/1.1\r\nHost: api.github.com:443\r\n\r\n"
                api_client = socket.create_connection(("127.0.0.1", adapter.server.getsockname()[1]))
                api_client.sendall(api_request)
                upstream, _ = unix.accept()
                self.assertEqual(upstream.recv(1), b"T")
                self.assertEqual(C.receive(upstream, len(api_request)), api_request)
                upstream.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
                self.assertEqual(api_client.recv(19), b"HTTP/1.1 200 OK\r\n\r\n")
                api_client.close()
                upstream.close()

                git_request = b"CONNECT github.com:443 HTTP/1.1\r\nHost: github.com:443\r\n\r\n"
                git_client = socket.create_connection(("127.0.0.1", adapter.server.getsockname()[1]))
                git_client.sendall(git_request)
                fallback_upstream, _ = fallback.accept()
                self.assertEqual(C.receive(fallback_upstream, len(git_request)), git_request)
                git_client.close()
                fallback_upstream.close()
            finally:
                adapter.stop()
                unix.close()
                fallback.close()

    def test_run_terminates_a_live_child_when_wait_fails(self):
        class Adapter:
            url = "http://127.0.0.1:4321"
            def __init__(self, *_args): pass
            def stop(self): pass
        class Child:
            pid = 4242
            waits = 0
            def poll(self): return None
            def wait(self, timeout=None):
                self.waits += 1
                if timeout is None and self.waits == 1:
                    raise OSError("wait failed")
                return 0
        child = Child()
        certificate = b"-----BEGIN CERTIFICATE-----\nspan\n-----END CERTIFICATE-----\n"
        with mock.patch.object(C, "receive_ca", return_value=certificate), \
             mock.patch.object(C, "system_ca", return_value=certificate), \
             mock.patch.object(C, "Adapter", Adapter), \
             mock.patch.object(C.subprocess, "Popen", return_value=child), \
             mock.patch.object(C.os, "killpg") as killpg, self.assertRaises(OSError):
            C.run(["gh", "api", "user"])
        killpg.assert_called_with(child.pid, C.signal.SIGTERM)


class FileContractTests(unittest.TestCase):
    def test_span_files_are_executable_and_standalone(self):
        for name in ("provider", "client"):
            path = EXAMPLE / name
            self.assertTrue(path.is_file())
            self.assertTrue(path.stat().st_mode & stat.S_IXUSR)
            self.assertLess(path.stat().st_size, 64 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
