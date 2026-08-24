from __future__ import annotations

import base64
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from credential_proxy import http_projection
from launcher import world_attachment


PUBLIC_CERTIFICATE = (
    b"-----BEGIN CERTIFICATE-----\n"
    b"public-test-certificate\n"
    b"-----END CERTIFICATE-----\n"
)


def manifest(**changes) -> bytes:
    document = {
        "v": 1,
        "ca_b64": base64.b64encode(PUBLIC_CERTIFICATE).decode("ascii"),
        "files": [],
        "environment": {},
        "routes": [],
    }
    document.update(changes)
    return json.dumps(document, separators=(",", ":")).encode()


class WorldAttachmentValidationTests(unittest.TestCase):
    def validate(self, payload: bytes):
        with mock.patch.object(world_attachment.ssl, "create_default_context"):
            return world_attachment.validate_manifest(payload)

    def test_manifest_validation_accepts_bounded_public_material(self):
        payload = manifest(
            files=[{
                "name": "nested/bootstrap.json",
                "contents_b64": base64.b64encode(b"fake-public-bootstrap").decode("ascii"),
            }],
            environment={"TOOL_CONFIG": "${ROOT}/nested/bootstrap.json", "HTTPS_PROXY": "${PROXY}"},
            routes=["api.example.com:443"],
        )
        certificate, files, environment, routes = self.validate(payload)
        self.assertEqual(certificate, PUBLIC_CERTIFICATE)
        self.assertEqual(files, [("nested/bootstrap.json", b"fake-public-bootstrap")])
        self.assertEqual(environment["TOOL_CONFIG"], "${ROOT}/nested/bootstrap.json")
        self.assertEqual(routes, ["api.example.com:443"])

    def test_manifest_validation_accepts_the_github_fake_token(self):
        _certificate, _files, environment, _routes = self.validate(
            manifest(environment={"GH_TOKEN": "[REDACTED]"})
        )
        self.assertEqual(environment, {"GH_TOKEN": "[REDACTED]"})

    def test_manifest_validation_rejects_unsafe_shapes(self):
        invalid = (
            manifest(files=[{"name": "../escape", "contents_b64": ""}]),
            manifest(environment={"BAD-NAME": "value"}),
            manifest(environment={"VALUE": "line-one\nline-two"}),
            manifest(environment={"VALUE": "${HOME}"}),
            manifest(environment={"VALUE": "quoted value # changed"}),
            manifest(routes=["EXAMPLE.com:443"]),
            manifest(routes=["example.com:443", "example.com:443"]),
            json.dumps({"v": 1}).encode(),
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(world_attachment.AttachmentError):
                self.validate(payload)

    def test_unresolved_environment_placeholder_is_rejected(self):
        with self.assertRaisesRegex(world_attachment.AttachmentError, "unresolved"):
            world_attachment.expand_environment({"VALUE": "${UNKNOWN}"}, "alpha")


class WorldAttachmentCompositionTests(unittest.TestCase):
    def attachment(self, certificate: bytes, filename: str, contents: bytes,
                   environment: dict[str, str], routes: list[str]):
        return certificate, [(filename, contents)], environment, routes

    def test_materialize_namespaces_roots_coalesces_environment_and_writes_artifacts(self):
        first_certificate = PUBLIC_CERTIFICATE
        second_certificate = PUBLIC_CERTIFICATE.replace(b"public-test", b"second-public")
        bootstraps = {
            ("127.0.0.1", 4101): self.attachment(
                first_certificate,
                "config/auth.json",
                b"fake-openai-bootstrap",
                {
                    "HTTPS_PROXY": "${PROXY}",
                    "SSL_CERT_FILE": "${CA}",
                    "TACT_AUTH_FILE": "${ROOT}/config/auth.json",
                },
                ["chatgpt.com:443"],
            ),
            ("127.0.0.1", 4102): self.attachment(
                second_certificate,
                "config/hosts.yml",
                b"fake-github-bootstrap",
                {
                    "HTTPS_PROXY": "${PROXY}",
                    "SSL_CERT_FILE": "${CA}",
                    "GH_CONFIG_DIR": "${ROOT}/config",
                },
                ["api.github.com:443"],
            ),
        }
        with tempfile.TemporaryDirectory() as raw:
            public = Path(raw) / "public"
            public.mkdir()
            with mock.patch.object(world_attachment, "bootstrap", side_effect=lambda address: bootstraps[address]):
                environment = world_attachment.materialize(public, [
                    ("openai", ("127.0.0.1", 4101)),
                    ("github", ("127.0.0.1", 4102)),
                ])

            self.assertEqual(environment["HTTPS_PROXY"], world_attachment.ISLAND_PROXY)
            self.assertEqual(environment["SSL_CERT_FILE"], world_attachment.ISLAND_CA)
            self.assertEqual(
                environment["TACT_AUTH_FILE"],
                world_attachment.ISLAND_ROOT + "/openai/config/auth.json",
            )
            self.assertEqual(
                environment["GH_CONFIG_DIR"],
                world_attachment.ISLAND_ROOT + "/github/config",
            )
            self.assertEqual(
                (public / "http-attachments/openai/config/auth.json").read_bytes(),
                b"fake-openai-bootstrap",
            )
            self.assertEqual(
                (public / "http-attachments/github/config/hosts.yml").read_bytes(),
                b"fake-github-bootstrap",
            )
            self.assertEqual(
                json.loads((public / "http-routes.json").read_text()),
                {"v": 1, "routes": {
                    "chatgpt.com:443": "openai",
                    "api.github.com:443": "github",
                }},
            )
            self.assertEqual(json.loads((public / "world-environment.json").read_text()), environment)
            self.assertEqual(
                (public / "http-ca.pem").read_bytes(),
                first_certificate.rstrip(b"\n") + b"\n" + second_certificate,
            )
            env_lines = (public / "world.env").read_text().splitlines()
            self.assertEqual(env_lines, sorted(env_lines))
            self.assertTrue(all("real-secret" not in line for line in env_lines))

    def test_conflicting_environment_is_rejected(self):
        responses = iter((
            (PUBLIC_CERTIFICATE, [], {"SHARED": "one"}, []),
            (PUBLIC_CERTIFICATE, [], {"SHARED": "two"}, []),
        ))
        with tempfile.TemporaryDirectory() as raw, \
             mock.patch.object(world_attachment, "bootstrap", side_effect=lambda _address: next(responses)), \
             self.assertRaisesRegex(world_attachment.AttachmentError, "environment variable: SHARED"):
            world_attachment.materialize(Path(raw), [
                ("alpha", ("127.0.0.1", 1)),
                ("beta", ("127.0.0.1", 2)),
            ])

    def test_conflicting_route_is_rejected_even_for_identical_authority(self):
        responses = iter((
            (PUBLIC_CERTIFICATE, [], {}, ["api.example.com:443"]),
            (PUBLIC_CERTIFICATE, [], {}, ["api.example.com:443"]),
        ))
        with tempfile.TemporaryDirectory() as raw, \
             mock.patch.object(world_attachment, "bootstrap", side_effect=lambda _address: next(responses)), \
             self.assertRaisesRegex(world_attachment.AttachmentError, "HTTPS route"):
            world_attachment.materialize(Path(raw), [
                ("alpha", ("127.0.0.1", 1)),
                ("beta", ("127.0.0.1", 2)),
            ])


class HttpProjectionTests(unittest.TestCase):
    def projection(self, routes: dict[str, str], socket_dir: Path):
        projection = object.__new__(http_projection.HttpProjection)
        projection.routes = routes
        projection.socket_dir = socket_dir
        projection.stopping = threading.Event()
        projection.slots = threading.BoundedSemaphore(1)
        projection.slots.acquire()
        projection.lock = threading.Lock()
        projection.connections = set()
        projection.threads = set()
        projection.resume_epoch = 0
        return projection

    def test_route_config_is_exact_and_restricted_to_granted_worlds(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "routes.json"
            path.write_text(json.dumps({
                "v": 1,
                "routes": {"api.example.com:443": "alpha"},
            }))
            self.assertEqual(
                http_projection.load_routes(path, {"alpha"}),
                {"api.example.com:443": "alpha"},
            )
            for document in (
                {"v": 1, "routes": {"api.example.com:443": "ungranted"}},
                {"v": 1, "routes": {"API.example.com:443": "alpha"}},
                {"v": 1, "routes": {"api.example.com:80": "alpha"}},
                {"v": 2, "routes": {}},
            ):
                with self.subTest(document=document):
                    path.write_text(json.dumps(document))
                    with self.assertRaises(http_projection.ProjectionError):
                        http_projection.load_routes(path, {"alpha"})

    def test_route_config_read_is_bounded(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "routes.json"
            path.write_bytes(b'{' + b' ' * http_projection.MAX_CONFIG)
            with self.assertRaisesRegex(
                http_projection.ProjectionError, "HTTP attachment config is too large",
            ):
                http_projection.load_routes(path, set())

    def test_connect_target_requires_the_worlds_canonical_lowercase_authority(self):
        self.assertEqual(
            http_projection.connect_target(b"CONNECT api.example.com:443 HTTP/1.1"),
            ("api.example.com", "api.example.com:443"),
        )
        with self.assertRaises(http_projection.ProjectionError):
            http_projection.connect_target(b"CONNECT API.example.com:443 HTTP/1.1")

    def test_declared_route_uses_named_unix_world_socket(self):
        with tempfile.TemporaryDirectory() as raw:
            projection = self.projection({"api.example.com:443": "alpha"}, Path(raw))
            client, peer = socket.socketpair()
            upstream = mock.MagicMock()
            with client, peer, \
                 mock.patch.object(http_projection.socket, "socket", return_value=upstream), \
                 mock.patch.object(http_projection.socket, "create_connection") as direct, \
                 mock.patch.object(http_projection, "duplex_stream"):
                peer.sendall(b"CONNECT api.example.com:443 HTTP/1.1\r\nHost: api.example.com\r\n\r\nopaque")
                projection._handle(client,projection.resume_epoch)
            upstream.connect.assert_called_once_with(str(Path(raw) / "alpha.sock"))
            upstream.sendall.assert_called_once_with(
                b"TCONNECT api.example.com:443 HTTP/1.1\r\nHost: api.example.com\r\n\r\nopaque"
            )
            direct.assert_not_called()

    def test_http_projection_retires_pre_resume_tunnels_on_a_wall_clock_gap(self):
        with tempfile.TemporaryDirectory() as raw:
            projection=self.projection({},Path(raw))
            projection.last_observed_time=(100,1000)
            proxy,client=socket.socketpair()
            projection._remember(projection.resume_epoch,proxy)
            try:
                with mock.patch("builtins.print") as output:
                    self.assertTrue(projection._retire_after_resume((101,1020)))
                output.assert_called_once_with(
                    "span-bridge: HTTP projection retired pre-resume transport",flush=True,
                )
                client.settimeout(1); self.assertEqual(client.recv(1),b"")
            finally:
                projection._retire_connections(); proxy.close(); client.close()

    def test_http_projection_rejects_a_pre_resume_epoch_before_upstream_use(self):
        with tempfile.TemporaryDirectory() as raw:
            slots=threading.BoundedSemaphore(1)
            self.assertTrue(slots.acquire(blocking=False))
            projection=http_projection.HttpProjection({},Path(raw),port=0,slots=slots)
            projection.resume_epoch=1
            client,peer=socket.socketpair()
            try:
                with mock.patch.object(http_projection.socket,"create_connection") as connect:
                    projection._handle(client,0)
                connect.assert_not_called()
                peer.settimeout(1)
                self.assertEqual(peer.recv(1),b"")
                self.assertTrue(slots.acquire(blocking=False))
            finally:
                client.close(); peer.close(); projection.server.close()

    def test_saturated_http_projection_backpressures_until_capacity_is_available(self):
        with tempfile.TemporaryDirectory() as raw:
            slots = threading.BoundedSemaphore(1)
            self.assertTrue(slots.acquire(blocking=False))
            projection = http_projection.HttpProjection(
                {}, Path(raw), port=0, slots=slots,
            )
            handled = threading.Event()

            def handle(client,epoch):
                self.assertEqual(epoch,projection.resume_epoch)
                with client:
                    client.sendall(b"accepted")
                slots.release()
                handled.set()

            projection._handle = handle
            projection.start()
            try:
                with socket.create_connection(projection.server.getsockname(), timeout=1) as client:
                    client.settimeout(0.1)
                    with self.assertRaises(TimeoutError):
                        client.recv(1)
                    slots.release()
                    client.settimeout(2)
                    self.assertEqual(client.recv(8), b"accepted")
                self.assertTrue(handled.wait(1))
            finally:
                projection.stop()

    def test_undeclared_route_uses_direct_fallback_without_forwarding_connect_headers(self):
        with tempfile.TemporaryDirectory() as raw:
            projection = self.projection({}, Path(raw))
            client, peer = socket.socketpair()
            upstream = mock.MagicMock()
            with client, peer, \
                 mock.patch.object(http_projection.socket, "create_connection", return_value=upstream) as direct, \
                 mock.patch.object(http_projection, "enable_tcp_keepalive") as keepalive, \
                 mock.patch.object(http_projection, "duplex_stream"):
                peer.sendall(b"CONNECT public.example:443 HTTP/1.1\r\nHost: public.example\r\n\r\ntls")
                projection._handle(client,projection.resume_epoch)
                response = peer.recv(4096)
            direct.assert_called_once_with(("public.example", 443), timeout=10)
            keepalive.assert_called_once_with(upstream)
            upstream.sendall.assert_called_once_with(b"tls")
            self.assertEqual(response, b"HTTP/1.1 200 Connection Established\r\n\r\n")

    def test_fatal_http_listener_failure_stops_the_sidecar(self):
        stopping = threading.Event()
        failed = threading.Event()
        slots = threading.BoundedSemaphore(http_projection.MAX_CONNECTIONS)
        projection = http_projection.HttpProjection(
            {}, Path("/unused"), port=0, slots=slots, stopping=stopping, failed=failed,
        )
        self.assertIs(projection.slots, slots)
        projection.start()
        projection.server.close()
        try:
            self.assertTrue(stopping.wait(2))
            self.assertTrue(failed.is_set())
            projection.thread.join(2)
            self.assertFalse(projection.thread.is_alive())
        finally:
            projection.stop()


if __name__ == "__main__":
    unittest.main()
