#!/usr/bin/env python3
"""A deliberately overpowered Slack-like World used only by this experiment."""

import json
import os
import socket
import threading


MAX_REQUEST = 16 * 1024


def receive_line(stream) -> dict:
    raw = stream.readline(MAX_REQUEST + 1)
    if not raw or len(raw) > MAX_REQUEST or not raw.endswith(b"\n"):
        raise ValueError("invalid request frame")
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError("request must be an object")
    return document


class SlackState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls = 0

    def execute(self, request: dict) -> dict:
        operation = request.get("operation")
        arguments = request.get("arguments")
        if not isinstance(operation, str) or not isinstance(arguments, dict):
            raise ValueError("invalid Slack operation")
        with self.lock:
            self.calls += 1
            call = self.calls

        # This upstream intentionally accepts a dangerous operation. The Gate,
        # rather than this World, is responsible for keeping it unreachable.
        if operation == "channels.list":
            result = ["engineering", "private-executives"]
        elif operation == "messages.read":
            result = {"channel": arguments.get("channel"), "messages": ["deploy complete"]}
        elif operation == "messages.send":
            result = {"sent": True, **arguments}
        elif operation == "admin.export":
            result = {"exported": "every private conversation"}
        else:
            result = {"executed_unknown_operation": operation}
        return {"ok": True, "upstream_call": call, "result": result}


if __name__ == "__main__":
    descriptor = int(os.environ["SLACK_LINK_FD"])
    state = SlackState()
    with socket.socket(fileno=descriptor) as link:
        stream = link.makefile("rwb")
        print("fake-slack World received one private Link", flush=True)
        while True:
            try:
                request = receive_line(stream)
            except ValueError as error:
                if str(error) == "invalid request frame":
                    break
                response = {"ok": False, "error": str(error)}
            except json.JSONDecodeError as error:
                response = {"ok": False, "error": str(error)}
            else:
                response = state.execute(request)
            stream.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
            stream.flush()
