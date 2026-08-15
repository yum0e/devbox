from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import socket
import struct
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "examples" / "probe-span"
PROVIDER = EXAMPLE / "probe-span"
CLIENT = EXAMPLE / "probe-span-client"


def load_provider():
    loader = importlib.machinery.SourceFileLoader("probe_span_example", str(PROVIDER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class ProbeSpanTests(unittest.TestCase):
    def test_description_is_the_exact_lean_contract(self):
        completed = subprocess.run([str(PROVIDER), "describe"], text=True, capture_output=True, check=True)
        self.assertEqual(
            json.loads(completed.stdout),
            {"name": "probe", "version": "0.1.0", "client": str(CLIENT.resolve())},
        )

    def test_provider_info_and_binary_echo(self):
        provider = load_provider()
        environment = {
            **os.environ,
            "DEVC2_ISLAND_ID": "test-island",
            "DEVC2_WORKSPACE": "/host/workspace",
        }
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        environment["DEVC2_SPAN_SOCKET_FD"] = str(listener.fileno())
        process = subprocess.Popen(
            [str(PROVIDER), "serve"], env=environment,
            pass_fds=(listener.fileno(),), stdout=subprocess.DEVNULL,
        )
        try:
            for operation, payload in ((b"I", b""), (b"E", b"opaque\x00bytes\n")):
                with socket.create_connection(listener.getsockname(), timeout=2) as connection:
                    connection.sendall(provider.HEADER.pack(operation, len(payload)) + payload)
                    status, size = provider.HEADER.unpack(provider.receive(connection, provider.HEADER.size))
                    response = provider.receive(connection, size)
                self.assertEqual(status, b"O")
                if operation == b"I":
                    document = json.loads(response)
                    self.assertEqual(document["island_id"], "test-island")
                    self.assertEqual(document["workspace"], "/host/workspace")
                    self.assertEqual(document["span"], "probe")
                else:
                    self.assertEqual(response, payload)
        finally:
            process.terminate()
            process.wait(timeout=5)
            listener.close()

    def test_client_and_provider_are_executable_and_bounded(self):
        self.assertTrue(os.access(PROVIDER, os.X_OK))
        self.assertTrue(os.access(CLIENT, os.X_OK))
        provider = load_provider()
        self.assertEqual(provider.MAX_PAYLOAD, 1024 * 1024)
        self.assertNotIn("subprocess", PROVIDER.read_text())
        self.assertNotIn("subprocess", CLIENT.read_text())

    def test_real_client_and_provider_agree_over_a_unix_stream(self):
        provider = load_provider()
        with tempfile.TemporaryDirectory() as raw:
            endpoint = str(Path(raw) / "probe.sock")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(endpoint)
            listener.listen()

            def serve(count):
                for _ in range(count):
                    connection, _address = listener.accept()
                    with connection:
                        provider.handle(connection)

            thread = threading.Thread(target=serve, args=(2,))
            environment={
                **os.environ,
                "DEVC2_PROBE_SOCKET": endpoint,
                "DEVC2_ISLAND_ID": "client-test",
                "DEVC2_WORKSPACE": "/host/client-workspace",
            }
            try:
                with mock.patch.dict(os.environ,environment,clear=True):
                    thread.start()
                    info=subprocess.run([str(CLIENT),"info"],env=environment,capture_output=True,check=True)
                    document=json.loads(info.stdout)
                    self.assertEqual(document["island_id"],"client-test")
                    payload=b"client\x00provider\n"
                    echo=subprocess.run([str(CLIENT),"echo"],env=environment,input=payload,capture_output=True,check=True)
                    self.assertEqual(echo.stdout,payload)
            finally:
                listener.close()
                thread.join(timeout=2)
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
