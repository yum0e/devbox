#!/usr/bin/env python3
"""Attenuate a broad Slack-like World into one narrow projected command."""

import json
import os
import socket
import socketserver
import subprocess
import threading

from world_sandbox import restrict_world


MAX_REQUEST = 16 * 1024
ALLOWED_CHANNEL = "engineering"


UPSTREAM = socket.socket(fileno=int(os.environ["SLACK_LINK_FD"]))
UPSTREAM_STREAM = UPSTREAM.makefile("rwb")
UPSTREAM_LOCK = threading.Lock()
ESCAPE_FILE = os.environ["ESCAPE_FILE"]
ESCAPE_PORT = int(os.environ["ESCAPE_PORT"])


def receive_line(stream) -> dict:
    raw = stream.readline(MAX_REQUEST + 1)
    if not raw or len(raw) > MAX_REQUEST or not raw.endswith(b"\n"):
        raise ValueError("invalid request frame")
    document = json.loads(raw)
    if not isinstance(document, dict) or set(document) != {"command", "argv"}:
        raise ValueError("request must contain only command and argv")
    if document["command"] != "slack":
        raise PermissionError("command is not exported by this World")
    argv = document["argv"]
    if not isinstance(argv, list) or len(argv) > 16 or not all(isinstance(item, str) and len(item) <= 4096 for item in argv):
        raise ValueError("invalid argv")
    return document


def attenuate(argv: list[str]) -> dict:
    if argv == ["channels", "list"]:
        return {"operation": "channels.list", "arguments": {}}
    if len(argv) == 3 and argv[:2] == ["messages", "read"]:
        if argv[2] != ALLOWED_CHANNEL:
            raise PermissionError("only #engineering may be read")
        return {"operation": "messages.read", "arguments": {"channel": argv[2]}}
    if len(argv) == 5 and argv[:2] == ["messages", "send"] and argv[2] == ALLOWED_CHANNEL and argv[4] == "--confirm":
        return {"operation": "messages.send", "arguments": {"channel": argv[2], "text": argv[3]}}
    if argv[:2] == ["messages", "send"]:
        raise PermissionError("sending requires #engineering and --confirm")
    raise PermissionError("operation is not granted")


def attempt_ambient_escape() -> None:
    """Model surprising behavior in an otherwise allowed upstream CLI action."""
    escaped = []
    try:
        with open(ESCAPE_FILE, "w", encoding="utf-8") as stream:
            stream.write("escaped")
    except PermissionError:
        pass
    else:
        escaped.append("filesystem")
    try:
        with socket.create_connection(("127.0.0.1", ESCAPE_PORT), timeout=0.2):
            pass
    except PermissionError:
        pass
    else:
        escaped.append("network")
    try:
        subprocess.run(["/bin/true"], check=False)
    except PermissionError:
        pass
    else:
        escaped.append("exec")
    try:
        os.kill(os.getppid(), 0)
    except PermissionError:
        pass
    else:
        escaped.append("process")
    if escaped:
        raise RuntimeError("ambient authority remained: " + ", ".join(escaped))


def invoke_upstream(request: dict) -> dict:
    payload = json.dumps(request, separators=(",", ":")).encode() + b"\n"
    with UPSTREAM_LOCK:
        UPSTREAM_STREAM.write(payload)
        UPSTREAM_STREAM.flush()
        response = UPSTREAM_STREAM.readline(MAX_REQUEST + 1)
    if not response or len(response) > MAX_REQUEST or not response.endswith(b"\n"):
        raise RuntimeError("invalid upstream response")
    document = json.loads(response)
    if not isinstance(document, dict):
        raise RuntimeError("invalid upstream response")
    return document


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            request = receive_line(self.rfile)
            attenuated = attenuate(request["argv"])
            if attenuated["operation"] == "messages.send":
                attempt_ambient_escape()
            response = invoke_upstream(attenuated)
        except PermissionError as error:
            response = {"ok": False, "kind": "denied", "error": str(error)}
        except (ValueError, json.JSONDecodeError) as error:
            response = {"ok": False, "kind": "invalid", "error": str(error)}
        except (OSError, RuntimeError) as error:
            response = {"ok": False, "kind": "unavailable", "error": str(error)}
        self.wfile.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")


class Server(socketserver.UnixStreamServer):
    pass


if __name__ == "__main__":
    gate_socket = os.environ["GATE_SOCKET"]
    with Server(gate_socket, Handler) as server:
        os.chmod(gate_socket, 0o600)
        restrict_world()
        print("Slack Gate World exports one structurally attenuated command", flush=True)
        server.serve_forever()
