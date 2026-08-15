#!/usr/bin/env python3
"""Materialize process Worlds, two affordances, and independently revocable Links."""

import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time

from world_sandbox import landlock_abi


ROOT = Path(__file__).resolve().parent


def stop(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def wait_for(path: Path, *processes: subprocess.Popen) -> None:
    deadline = time.monotonic() + 5
    while not path.exists():
        if any(process.poll() is not None for process in processes):
            raise RuntimeError("a World exited during startup")
        if time.monotonic() >= deadline:
            raise RuntimeError(f"projection did not appear: {path}")
        time.sleep(0.02)


def start_projection(descriptor: int, path: Path) -> subprocess.Popen:
    path.unlink(missing_ok=True)
    environment = os.environ.copy()
    environment["AFFORDANCE_LINK_FD"] = str(descriptor)
    environment["PROJECTION_SOCKET"] = os.fspath(path)
    return subprocess.Popen(
        [sys.executable, os.fspath(ROOT / "socket_projection.py")],
        env=environment,
        pass_fds=(descriptor,),
    )


def verify(phase: str, environment: dict[str, str]) -> None:
    completed = subprocess.run(
        [sys.executable, os.fspath(ROOT / "verify.py"), phase],
        env=environment,
        close_fds=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"verification phase {phase!r} failed")


def main() -> int:
    if sys.platform != "linux" or landlock_abi() < 6:
        raise RuntimeError("this structural experiment requires Linux Landlock ABI 6")

    processes: list[subprocess.Popen] = []
    projection: subprocess.Popen | None = None
    with tempfile.TemporaryDirectory(prefix="shima-gated-world-") as raw_runtime:
        runtime = Path(raw_runtime)
        gate_socket = runtime / "gate.sock"
        signer_socket = runtime / "signer.sock"
        escape_file = runtime / "gate-escaped"
        projected_bin = runtime / "bin"
        projected_bin.mkdir(mode=0o700)
        slack_projection = projected_bin / "slack"
        slack_projection.symlink_to(ROOT / "shima-link.py")

        escape_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        escape_listener.bind(("127.0.0.1", 0))
        escape_listener.listen()
        escape_listener.settimeout(0.05)

        gate_end, slack_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        projection_end, signer_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            slack_environment = os.environ.copy()
            slack_environment["SLACK_LINK_FD"] = str(slack_end.fileno())
            slack = subprocess.Popen(
                [sys.executable, os.fspath(ROOT / "fake_slack.py")],
                env=slack_environment,
                pass_fds=(slack_end.fileno(),),
            )
            processes.append(slack)

            gate_environment = os.environ.copy()
            gate_environment.update({
                "SLACK_LINK_FD": str(gate_end.fileno()),
                "GATE_SOCKET": os.fspath(gate_socket),
                "ESCAPE_FILE": os.fspath(escape_file),
                "ESCAPE_PORT": str(escape_listener.getsockname()[1]),
            })
            gate = subprocess.Popen(
                [sys.executable, os.fspath(ROOT / "slack_gate.py")],
                env=gate_environment,
                pass_fds=(gate_end.fileno(),),
            )
            processes.append(gate)

            signer_environment = os.environ.copy()
            signer_environment.update({
                "SIGNER_LINK_FD": str(signer_end.fileno()),
                "SIGNER_SECRET": secrets.token_hex(32),
            })
            signer = subprocess.Popen(
                [sys.executable, os.fspath(ROOT / "fake_signer.py")],
                env=signer_environment,
                pass_fds=(signer_end.fileno(),),
            )
            processes.append(signer)
        finally:
            gate_end.close()
            slack_end.close()
            signer_end.close()

        projection = start_projection(projection_end.fileno(), signer_socket)
        processes.append(projection)
        wait_for(gate_socket, gate, slack)
        wait_for(signer_socket, projection, signer)

        caller_environment = os.environ.copy()
        for name in ("SLACK_LINK_FD", "SIGNER_LINK_FD", "SIGNER_SECRET"):
            caller_environment.pop(name, None)
        caller_environment.update({
            "SHIMA_LINK_SLACK": "unix:" + os.fspath(gate_socket),
            "SIGNER_SOCKET": os.fspath(signer_socket),
            "PROJECTED_BIN": os.fspath(projected_bin),
            "PATH": os.fspath(projected_bin) + os.pathsep + caller_environment["PATH"],
        })

        try:
            verify("initial", caller_environment)
            if escape_file.exists():
                raise RuntimeError("Gate wrote outside its ambient-free World")
            try:
                escaped, _address = escape_listener.accept()
            except TimeoutError:
                pass
            else:
                escaped.close()
                raise RuntimeError("Gate reached ambient TCP authority")
            print("PASS: Landlock and seccomp blocked filesystem, network, exec, and process escape")

            # Revoke only the socket affordance; the projected Slack command remains.
            stop(projection)
            signer_socket.unlink(missing_ok=True)
            verify("signer-revoked", caller_environment)

            # Regrant the same signer affordance, then revoke only Slack.
            projection = start_projection(projection_end.fileno(), signer_socket)
            processes.append(projection)
            wait_for(signer_socket, projection, signer)
            stop(gate)
            gate_socket.unlink(missing_ok=True)
            slack_projection.unlink()
            verify("slack-revoked", caller_environment)
            return 0
        finally:
            escape_listener.close()
            projection_end.close()
            for process in reversed(processes):
                stop(process)


if __name__ == "__main__":
    raise SystemExit(main())
