#!/usr/bin/env python3
"""Verify command/socket projections, attenuation, and independent revocation."""

import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import time


def slack(*arguments: str, expected: int = 0) -> dict | None:
    completed = subprocess.run(["slack", *arguments], text=True, capture_output=True, check=False)
    if completed.returncode != expected:
        raise AssertionError(
            f"slack {arguments!r}: expected {expected}, got {completed.returncode}\n"
            f"stdout={completed.stdout!r}\nstderr={completed.stderr!r}"
        )
    if expected:
        if "denied" not in completed.stderr:
            raise AssertionError(f"denial was not explicit: {completed.stderr!r}")
        return None
    return json.loads(completed.stdout)


def sign(payload: bytes) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(os.environ["SIGNER_SOCKET"])
        connection.sendall(b"sign\0" + payload)
        connection.shutdown(socket.SHUT_WR)
        response = connection.recv(4097)
    marker, separator, signature = response.partition(b"\0")
    assert separator and marker == b"ok" and len(signature) == 64
    return signature


def wait_for_gate() -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        completed = subprocess.run(["slack", "channels", "list"], capture_output=True, check=False)
        if completed.returncode == 0:
            return
        time.sleep(0.1)
    raise AssertionError("Slack Gate World did not become ready")


def initial() -> None:
    wait_for_gate()
    assert "SLACK_LINK_FD" not in os.environ
    assert "SIGNER_LINK_FD" not in os.environ
    assert "SIGNER_SECRET" not in os.environ
    projected = Path(os.environ["PROJECTED_BIN"]) / "slack"
    signer = Path(os.environ["SIGNER_SOCKET"])
    assert projected.is_symlink() and projected.resolve().name == "shima-link.py"
    assert stat.S_ISSOCK(signer.stat().st_mode)
    assert signer.stat().st_mode & 0o777 == 0o600
    assert projected.parent.stat().st_mode & 0o777 == 0o700

    first = slack("messages", "read", "engineering")
    assert first and first["upstream_call"] == 2
    slack("messages", "read", "private-executives", expected=77)
    slack("admin", "export", expected=77)
    slack("messages", "send", "engineering", "oops", expected=77)
    slack("channels", "list", "; touch /tmp/pwned", expected=77)
    second = slack("channels", "list")
    assert second and second["upstream_call"] == 3
    sent = slack("messages", "send", "engineering", "ship it", "--confirm")
    assert sent and sent["upstream_call"] == 4

    signature = sign(b"same payload")
    assert sign(b"same payload") == signature
    assert sign(b"different payload") != signature
    print("PASS: command and native-socket affordances were projected independently")
    print("PASS: denied and unknown Slack operations failed closed before upstream")
    print("PASS: caller received neither upstream Link nor signer secret")


def signer_revoked() -> None:
    assert not Path(os.environ["SIGNER_SOCKET"]).exists()
    assert slack("channels", "list")
    print("PASS: revoking the socket Link preserved the command projection")


def slack_revoked() -> None:
    projected = Path(os.environ["PROJECTED_BIN"]) / "slack"
    assert not projected.exists() and not projected.is_symlink()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        try:
            connection.connect(os.environ["SHIMA_LINK_SLACK"].removeprefix("unix:"))
        except OSError:
            pass
        else:
            raise AssertionError("revoked Slack Link remained reachable")
    assert sign(b"socket survived Slack revocation")
    print("PASS: revoking the command Link preserved the socket projection")


if __name__ == "__main__":
    phases = {"initial": initial, "signer-revoked": signer_revoked, "slack-revoked": slack_revoked}
    if len(sys.argv) != 2 or sys.argv[1] not in phases:
        raise SystemExit("usage: verify.py initial|signer-revoked|slack-revoked")
    phases[sys.argv[1]]()
