#!/usr/bin/env python3
"""One generic command projection; argv[0] selects the exported affordance."""

import json
import os
from pathlib import Path
import socket
import sys


MAX_RESPONSE = 16 * 1024


def address(value: str) -> str:
    prefix = "unix:"
    if not value.startswith(prefix) or not value[len(prefix):].startswith("/"):
        raise ValueError("invalid Link address")
    return value[len(prefix):]


def main() -> int:
    command = Path(sys.argv[0]).name
    variable = "SHIMA_LINK_" + command.upper().replace("-", "_")
    try:
        target = address(os.environ[variable])
        request = json.dumps({"command": command, "argv": sys.argv[1:]}, separators=(",", ":")).encode() + b"\n"
        if len(request) > 16 * 1024:
            raise ValueError("request is too large")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect(target)
            connection.sendall(request)
            raw = connection.makefile("rb").readline(MAX_RESPONSE + 1)
        if not raw or len(raw) > MAX_RESPONSE or not raw.endswith(b"\n"):
            raise RuntimeError("invalid Link response")
        response = json.loads(raw)
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            raise RuntimeError("invalid Link response")
        if not response["ok"]:
            print(f"{command}: {response.get('kind', 'error')}: {response.get('error', 'request failed')}", file=sys.stderr)
            return 77 if response.get("kind") == "denied" else 1
        print(json.dumps(response, separators=(",", ":")))
        return 0
    except (KeyError, ValueError, OSError, RuntimeError, json.JSONDecodeError) as error:
        print(f"{command}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
