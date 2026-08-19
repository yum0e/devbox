from __future__ import annotations

import base64
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).parents[1]
WORLD = ROOT / "spans" / "http-credential-world"
loader = importlib.machinery.SourceFileLoader("github_world", str(WORLD))
spec = importlib.util.spec_from_loader("github_world", loader)
W = importlib.util.module_from_spec(spec)
loader.exec_module(W)
GITHUB = W.POLICIES["github"]


def connect(request: bytes):
    left, right = socket.socketpair()
    with left, right:
        right.sendall(request)
        return W.read_connect(left, GITHUB)


class GitHubPolicyTests(unittest.TestCase):
    def test_exact_executable_basename_selects_provider_fail_closed(self):
        self.assertIs(W.select_policy("/snapshot/openai"), W.POLICIES["openai"])
        self.assertIs(W.select_policy("/snapshot/github"), GITHUB)
        for name in ("http-credential-world", "GitHub", "github.py", "github "):
            with self.subTest(name=name), self.assertRaises(W.ProviderError):
                W.select_policy(name)

    def test_connect_scope_is_exact(self):
        for target in (b"api.github.com:443", b"uploads.github.com:443"):
            request = b"CONNECT " + target + b" HTTP/1.1\r\nHost: " + target + b"\r\n\r\nbody"
            header, trailing = connect(request)
            self.assertEqual(header + trailing, request)
        for request in (
            b"CONNECT github.com:443 HTTP/1.1\r\nHost: github.com:443\r\n\r\n",
            b"CONNECT evilapi.github.com:443 HTTP/1.1\r\nHost: evilapi.github.com:443\r\n\r\n",
            b"CONNECT api.github.com:443 HTTP/1.1\r\nHost: uploads.github.com:443\r\n\r\n",
            b"CONNECT api.github.com:443 HTTP/1.1\r\nHost: api.github.com:443\r\nHost: api.github.com:443\r\n\r\n",
        ):
            with self.subTest(request=request), self.assertRaises(W.ProviderError):
                connect(request)

    def test_routes_methods_paths_and_header_transform_are_explicit(self):
        sentinel = "random-lifetime-sentinel"
        config = W.proxy_config(GITHUB, sentinel)
        secret = config["transforms"][0]["config"]["secrets"][0]
        self.assertEqual(secret["replace"], {"proxy_value": sentinel,
            "match_headers": ["Authorization"], "require": True})
        self.assertEqual({rule["host"] for rule in secret["rules"]},
                         {"api.github.com", "uploads.github.com"})
        for rule in secret["rules"]:
            self.assertEqual(rule["paths"], ["/", "/*"])
            self.assertEqual(rule["methods"],
                ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
        self.assertNotIn('"host": "github.com"', json.dumps(config))

    def test_manifest_is_public_scoped_and_lifetime_random(self):
        certificate = b"-----BEGIN CERTIFICATE-----\npublic\n-----END CERTIFICATE-----\n"
        first, second = W.ProxyManager(GITHUB), W.ProxyManager(GITHUB)
        self.assertNotEqual(first.sentinel, second.sentinel)
        first.proxy = W.Proxy(Path("/unused"), "a" * 64, 1234, certificate)
        with mock.patch.object(first, "_state", return_value="running"):
            one = first.bootstrap()
            self.assertIs(one, first.bootstrap())
        document = json.loads(one)
        self.assertEqual(document["routes"], ["api.github.com:443", "uploads.github.com:443"])
        self.assertEqual(document["environment"]["GH_TOKEN"], first.sentinel)
        self.assertEqual(document["environment"]["GITHUB_TOKEN"], first.sentinel)
        self.assertNotIn("HTTP_PROXY", document["environment"])
        hosts = base64.b64decode(document["files"][0]["contents_b64"])
        self.assertIn(first.sentinel.encode(), hosts)
        self.assertNotIn(b"PRIVATE KEY", one)

    def test_auth_parser_is_private_bounded_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw) / "gh"
            directory.mkdir(mode=0o700)
            hosts = directory / "hosts.yml"
            hosts.write_text("github.com:\n  oauth_token: real-token_1\n")
            hosts.chmod(0o600)
            with mock.patch.object(W, "auth_directory", return_value=directory):
                self.assertEqual(W.read_github_auth(), ("real-token_1", ""))
            with mock.patch.object(W, "auth_directory", return_value=directory), \
                 mock.patch.object(W.os, "read", return_value=b"x" * (W.MAX_AUTH + 1)), \
                 self.assertRaisesRegex(W.ProviderError, "bounded private file"):
                W.read_github_auth()
            hosts.chmod(0o622)
            with mock.patch.object(W, "auth_directory", return_value=directory), self.assertRaises(W.ProviderError):
                W.read_github_auth()

    def test_exited_helper_refreshes_github_token_without_changing_manifest(self):
        certificate = b"-----BEGIN CERTIFICATE-----\npublic\n-----END CERTIFICATE-----\n"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "github-token").write_text("old-token")
            (root / "proxy.yaml").write_text("old-config")
            manager = W.ProxyManager(GITHUB)
            proxy = W.Proxy(root, "a" * 64, 1111, certificate)
            manager.proxy = proxy

            def docker(arguments, timeout=120, stopping=None):
                if arguments[:3] == ["run", "--pull", "never"]:
                    return subprocess.CompletedProcess(arguments, 0, "b" * 64 + "\n", "")
                if arguments[:2] == ["port", "b" * 64]:
                    return subprocess.CompletedProcess(arguments, 0, "127.0.0.1:43210\n", "")
                raise AssertionError(arguments)

            with mock.patch.object(manager, "_state", side_effect=["running", "exited"]), \
                 mock.patch.object(W, "remove_container", return_value=True), \
                 mock.patch.object(W, "read_github_auth", return_value=("new-token", "")), \
                 mock.patch.object(W, "run_docker", side_effect=docker), \
                 mock.patch.object(W.socket, "create_connection", return_value=mock.MagicMock()), \
                 mock.patch.dict(W.os.environ, {"DEVC2_WORKSPACE": "/workspace"}, clear=True):
                manifest = manager.bootstrap()
                restarted = manager.get()

            self.assertIs(restarted, proxy)
            self.assertEqual((root / "github-token").read_text(), "new-token")
            self.assertNotIn("new-token", (root / "proxy.yaml").read_text())
            with mock.patch.object(manager, "_state", return_value="running"):
                self.assertIs(manager.bootstrap(), manifest)

    def test_single_executable_source_contract(self):
        self.assertTrue(WORLD.stat().st_mode & stat.S_IXUSR)
        self.assertFalse((ROOT / "spans/openai/world").exists())
        self.assertFalse((ROOT / "spans/github/world").exists())


if __name__ == "__main__":
    unittest.main()
