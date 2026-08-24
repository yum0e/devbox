#!/usr/bin/env python3
"""Tie one Span World process group to its devc2 launcher's lifetime."""
from __future__ import annotations

import argparse
import os
import select
import signal
import subprocess
import time


def stop_group(process: subprocess.Popen) -> None:
    leader_running = process.poll() is None
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    if leader_running:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    deadline = time.monotonic() + 0.1
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        process.wait(timeout=5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--world", required=True)
    parser.add_argument("--listener-fd", required=True, type=int)
    parser.add_argument("--lifetime-fd", required=True, type=int)
    parser.add_argument("--started-fd", required=True, type=int)
    parser.add_argument("--recovery-fd", type=int)
    args = parser.parse_args(argv)
    stopping = False

    def request_stop(_signum=None, _frame=None):
        nonlocal stopping
        stopping = True

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, request_stop)

    pass_fds = [args.listener_fd]
    if args.recovery_fd is not None:
        pass_fds.append(args.recovery_fd)
    world = subprocess.Popen(
        [args.world],
        stdin=subprocess.DEVNULL,
        pass_fds=tuple(pass_fds),
        start_new_session=True,
    )
    os.close(args.listener_fd)
    if args.recovery_fd is not None:
        os.close(args.recovery_fd)
    try:
        deadline = time.monotonic() + 0.1
        while time.monotonic() < deadline and world.poll() is None and not stopping:
            time.sleep(0.01)
        if world.poll() is not None:
            return world.returncode
        if stopping:
            stop_group(world)
            return 0
        # This is deliberately structural: the child exec succeeded and did not
        # exit immediately. Semantic readiness belongs to the World protocol.
        os.write(args.started_fd, b"1")
        os.close(args.started_fd)
        while not stopping:
            status = world.poll()
            if status is not None:
                return status
            readable, _writable, _errors = select.select([args.lifetime_fd], [], [], 0.1)
            if readable and not os.read(args.lifetime_fd, 1):
                break
        stop_group(world)
        return 0
    finally:
        try:
            os.close(args.started_fd)
        except OSError:
            pass
        os.close(args.lifetime_fd)
        stop_group(world)


if __name__ == "__main__":
    raise SystemExit(main())
