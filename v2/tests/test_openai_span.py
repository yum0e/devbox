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
WORLD_PATH = ROOT / "spans" / "http-credential-world"


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
    def test_http_world_capacity_matches_transport_limit(self):
        self.assertEqual(P.MAX_HANDLERS, 256)

    def test_http_world_waits_for_handler_capacity_instead_of_resetting(self):
        slots = mock.Mock()
        slots.acquire.side_effect = [False, False, True]
        self.assertTrue(P.wait_for_handler_slot(slots, threading.Event()))
        self.assertEqual(slots.acquire.call_count, 3)
        slots.acquire.assert_called_with(timeout=0.5)

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
            config = P.proxy_config(P.POLICIES["openai"], "fake-access", "fake-account")
            self.assertNotIn(access, json.dumps(config))
            self.assertEqual(config["transforms"][0]["config"]["secrets"][0]["source"]["type"], "file")

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

    def test_cancelled_docker_operation_terminates_the_cli(self):
        stopping = threading.Event()
        stopping.set()
        process = mock.Mock()
        process.poll.return_value = None
        process.communicate.return_value = ("", "")
        with mock.patch.object(P, "docker_executable", return_value="/usr/bin/docker"), \
             mock.patch.object(P.subprocess, "Popen", return_value=process), \
             self.assertRaisesRegex(P.ProviderError, "stopped"):
            P.run_docker(["version"], stopping=stopping)
        process.terminate.assert_called_once_with()
        process.communicate.assert_called_once_with(timeout=1)

    def test_failed_detached_launch_is_cleaned_by_name_without_an_id(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manager = P.ProxyManager()
            with mock.patch.object(P, "read_auth", return_value=("access", "account")), \
                 mock.patch.object(P, "run_docker", side_effect=P.ProviderError("failed")), \
                 mock.patch.object(P, "remove_named_container", return_value=True) as remove, \
                 self.assertRaisesRegex(P.ProviderError, "failed"):
                manager._launch(root, "workspace-tag")
        remove.assert_called_once()
        self.assertRegex(
            remove.call_args.args[0],
            r"^devc2-openai-proxy-[0-9a-f]{32}$",
        )

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
        manager.policy = P.POLICIES["openai"]
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

            def docker(arguments, timeout=120, stopping=None):
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

    def test_bootstrap_is_stable_and_lifetime_sentinels_are_independent(self):
        certificate = b"-----BEGIN CERTIFICATE-----\npublic\n-----END CERTIFICATE-----\n"
        first = P.ProxyManager()
        second = P.ProxyManager()
        first.proxy = P.Proxy(Path("/unused"), "a" * 64, 1234, certificate)
        with mock.patch.object(first, "_state", return_value="running"):
            one = first.bootstrap()
            two = first.bootstrap()
        self.assertIs(one, two)
        self.assertEqual(one, two)
        self.assertNotEqual(first.fake_account, second.fake_account)
        self.assertNotEqual(first.fake_access, second.fake_access)

        document = json.loads(one)
        auth = json.loads(base64.b64decode(document["files"][0]["contents_b64"]))
        self.assertEqual(auth["tokens"]["account_id"], first.fake_account)
        self.assertEqual(auth["tokens"]["access_token"], first.fake_access)

    def test_exited_helper_restarts_with_refreshed_auth_and_same_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "ca.crt").write_bytes(b"certificate")
            (root / "ca.key").write_bytes(b"private")
            (root / "access-token").write_text("old-access")
            (root / "account-id").write_text("old-account")
            (root / "proxy.yaml").write_text("old-config")
            manager = P.ProxyManager()
            proxy = P.Proxy(root, "a" * 64, 1111, b"certificate")
            manager.proxy = proxy

            calls = []
            def docker(arguments, timeout=120, stopping=None):
                calls.append(arguments)
                if arguments[:3] == ["run", "--pull", "never"]:
                    return subprocess.CompletedProcess(arguments, 0, "b" * 64 + "\n", "")
                if arguments[:2] == ["port", "b" * 64]:
                    return subprocess.CompletedProcess(arguments, 0, "127.0.0.1:43210\n", "")
                raise AssertionError(arguments)

            with mock.patch.object(manager, "_state", side_effect=["running", "exited"]), \
                 mock.patch.object(P, "remove_container", return_value=True), \
                 mock.patch.object(P, "read_auth", return_value=("new-access", "new-account")), \
                 mock.patch.object(P, "run_docker", side_effect=docker), \
                 mock.patch.object(P.socket, "create_connection", return_value=mock.MagicMock()), \
                 mock.patch.dict(P.os.environ, {"DEVC2_WORKSPACE": "/workspace"}, clear=True):
                manifest = manager.bootstrap()
                restarted = manager.get()

            self.assertIs(restarted, proxy)
            self.assertEqual(restarted.container, "b" * 64)
            self.assertEqual(restarted.port, 43210)
            self.assertEqual(restarted.certificate, b"certificate")
            self.assertEqual((root / "access-token").read_text(), "new-access")
            self.assertEqual((root / "account-id").read_text(), "new-account")
            with mock.patch.object(manager, "_state", return_value="running"):
                self.assertEqual(manager.bootstrap(), manifest)
            self.assertEqual(
                json.loads((root / "proxy.yaml").read_text())["transforms"][0]["config"]["secrets"][0]["replace"]["proxy_value"],
                manager.fake_access,
            )
            launch = next(call for call in calls if call[:3] == ["run", "--pull", "never"])
            self.assertNotIn("new-access", launch)
            self.assertNotIn("new-account", launch)

    def test_restart_fails_closed_on_unknown_state_or_removal_failure(self):
        manager = P.ProxyManager()
        manager.proxy = P.Proxy(Path("/unused"), "a" * 64, 1234, b"certificate")
        failed = subprocess.CompletedProcess([], 1, "", "daemon unavailable")
        with mock.patch.object(P, "run_docker", side_effect=[failed, failed]), \
             self.assertRaisesRegex(P.ProviderError, "verify"):
            manager.get()

        with mock.patch.object(manager, "_state", return_value="exited"), \
             mock.patch.object(P, "remove_container", return_value=False), \
             mock.patch.object(manager, "_launch") as launch, \
             self.assertRaisesRegex(P.ProviderError, "cleanup"):
            manager.get()
        launch.assert_not_called()

    def test_running_but_unreachable_helper_is_recycled_once(self):
        manager = P.ProxyManager()
        proxy = P.Proxy(Path("/private"), "a" * 64, 1111, b"certificate")
        manager.proxy = proxy
        with mock.patch.object(P, "remove_container", return_value=True) as remove, \
             mock.patch.object(manager, "_launch", return_value=("b" * 64, 2222)) as launch, \
             mock.patch.object(manager, "workspace_tag", return_value="tag"), \
             mock.patch.object(P, "recovery_event") as event:
            self.assertIs(manager.recover(proxy, "a" * 64), proxy)
            self.assertIs(manager.recover(proxy, "a" * 64), proxy)
        remove.assert_called_once_with("a" * 64)
        launch.assert_called_once_with(Path("/private"), "tag")
        self.assertEqual((proxy.container, proxy.port), ("b" * 64, 2222))
        self.assertEqual([call.args[0] for call in event.call_args_list], [b"S", b"R"])

    def test_recovery_fails_closed_and_has_a_bounded_cooldown(self):
        manager = P.ProxyManager()
        proxy = P.Proxy(Path("/private"), "a" * 64, 1111, b"certificate")
        manager.proxy = proxy
        with mock.patch.object(P.time, "monotonic", return_value=10.0), \
             mock.patch.object(P, "remove_container", return_value=False), \
             mock.patch.object(manager, "_launch") as launch, \
             mock.patch.object(P, "recovery_event") as event, \
             self.assertRaisesRegex(P.ProviderError, "cleanup"):
            manager.recover(proxy, proxy.container)
        launch.assert_not_called()
        self.assertEqual([call.args[0] for call in event.call_args_list], [b"S", b"F"])

        with mock.patch.object(manager, "_state", return_value="exited"), \
             mock.patch.object(P.time, "monotonic", return_value=11.0), \
             mock.patch.object(P, "remove_container") as remove, \
             self.assertRaisesRegex(P.ProviderError, "cooling down"):
            manager.get()
        remove.assert_not_called()

    def test_stopping_manager_cannot_resurrect_a_helper(self):
        manager = P.ProxyManager()
        proxy = P.Proxy(Path("/private"), "a" * 64, 1111, b"certificate")
        manager.proxy = proxy
        manager.stopping.set()
        with mock.patch.object(P, "remove_container") as remove, \
             mock.patch.object(manager, "_launch") as launch, \
             self.assertRaisesRegex(P.ProviderError, "stopping"):
            manager.recover(proxy, proxy.container)
        remove.assert_not_called()
        launch.assert_not_called()

    def test_replacement_is_removed_if_shutdown_wins_before_publish(self):
        manager = P.ProxyManager()
        original = "a" * 64
        replacement = "b" * 64
        proxy = P.Proxy(Path("/private"), original, 1111, b"certificate")
        manager.proxy = proxy

        def launch(_root, _tag):
            manager.stopping.set()
            return replacement, 2222

        with mock.patch.object(P, "remove_container", return_value=True) as remove, \
             mock.patch.object(manager, "_launch", side_effect=launch), \
             mock.patch.object(manager, "workspace_tag", return_value="tag"), \
             self.assertRaisesRegex(P.ProviderError, "stopping"):
            manager.recover(proxy, original)
        self.assertEqual(remove.call_args_list, [mock.call(original), mock.call(replacement)])
        self.assertEqual((proxy.container, proxy.port), (original, 1111))

    def test_upstream_proxy_failure_never_recycles_the_helper(self):
        request = b"CONNECT chatgpt.com:443 HTTP/1.1\r\n\r\n"
        proxy = P.Proxy(Path("/private"), "a" * 64, 1111, b"certificate")
        manager = mock.Mock()
        manager.get.return_value = proxy
        upstream = mock.MagicMock(spec=socket.socket)
        upstream.recv.return_value = b"HTTP/1.1 503 Service Unavailable\r\n\r\n"
        with mock.patch.object(P.socket, "create_connection", return_value=upstream):
            result, response = P.open_proxy_tunnel(manager, request)
        self.assertIsNone(result)
        self.assertEqual(response, b"HTTP/1.1 503 Service Unavailable\r\n\r\n")
        manager.recover.assert_not_called()
        upstream.close.assert_called_once_with()

    def test_connect_recovery_happens_before_tunnel_bytes_are_exposed(self):
        request = b"CONNECT chatgpt.com:443 HTTP/1.1\r\n\r\n"
        proxy = P.Proxy(Path("/private"), "a" * 64, 1111, b"certificate")
        manager = mock.Mock()
        manager.get.return_value = proxy
        manager.recover.return_value = proxy
        healthy = mock.MagicMock(spec=socket.socket)
        healthy.recv.return_value = b"HTTP/1.1 200 Connection Established\r\n\r\n"
        with mock.patch.object(
            P.socket, "create_connection",
            side_effect=[ConnectionRefusedError(), healthy],
        ), mock.patch.object(P.time, "sleep") as sleep:
            upstream, response = P.open_proxy_tunnel(manager, request)
        self.assertIs(upstream, healthy)
        self.assertEqual(response, b"HTTP/1.1 200 Connection Established\r\n\r\n")
        manager.recover.assert_called_once_with(proxy, "a" * 64)
        healthy.sendall.assert_called_once_with(request)
        sleep.assert_called_once_with(0.25)

    def test_handle_never_replays_trailing_tls_or_application_bytes(self):
        connection = mock.MagicMock(spec=socket.socket)
        connection.__enter__.return_value = connection
        connection.recv.return_value = b"T"
        upstream = mock.MagicMock(spec=socket.socket)
        upstream.__enter__.return_value = upstream
        request = b"CONNECT chatgpt.com:443 HTTP/1.1\r\n\r\n"
        trailing = b"tls-and-opaque-application"
        with mock.patch.object(P, "read_connect", return_value=(request, trailing)), \
             mock.patch.object(
                 P, "open_proxy_tunnel",
                 return_value=(upstream, b"HTTP/1.1 200 OK\r\n\r\n"),
             ), mock.patch.object(P, "relay") as relay:
            P.handle(connection, mock.Mock(policy=P.POLICIES["openai"]))
        upstream.sendall.assert_called_once_with(trailing)
        connection.sendall.assert_called_once_with(b"HTTP/1.1 200 OK\r\n\r\n")
        relay.assert_called_once_with(connection,upstream)

    def test_bootstrap_returns_only_public_and_fake_material(self):
        certificate = b"-----BEGIN CERTIFICATE-----\npublic\n-----END CERTIFICATE-----\n"
        manager = P.ProxyManager()
        manager.proxy = P.Proxy(Path("/unused"), "a" * 64, 1234, certificate)
        left, right = socket.socketpair()
        with mock.patch.object(manager, "_state", return_value="running"):
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
        self.assertEqual(auth["tokens"]["access_token"], manager.fake_access)
        self.assertEqual(auth["tokens"]["account_id"], manager.fake_account)
        self.assertEqual(result["environment"]["DEVC2_PI_OPENAI_TOKEN"], manager.fake_access)
        self.assertNotIn("HTTP_PROXY", result["environment"])
        self.assertNotIn("http_proxy", result["environment"])
        access_claims = json.loads(base64.urlsafe_b64decode(
            auth["tokens"]["access_token"].split(".")[1] + "=="
        ))
        self.assertEqual(
            access_claims["https://api.openai.com/auth"]["chatgpt_account_id"],
            manager.fake_account,
        )
        self.assertNotIn(b"PRIVATE KEY", json.dumps(result).encode())
        self.assertEqual(result["routes"], ["chatgpt.com:443"])
