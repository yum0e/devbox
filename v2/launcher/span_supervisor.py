#!/usr/bin/env python3
"""Tie one Span provider process group to its devc2 launcher's lifetime."""
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
    parser.add_argument("--provider", required=True)
    parser.add_argument("--listener-fd", required=True, type=int)
    parser.add_argument("--lifetime-fd", required=True, type=int)
    parser.add_argument("--ready-fd", required=True, type=int)
    args = parser.parse_args(argv)
    stopping = False

    def request_stop(_signum=None, _frame=None):
        nonlocal stopping
        stopping = True

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, request_stop)

    provider = subprocess.Popen(
        [args.provider],
        stdin=subprocess.DEVNULL,
        pass_fds=(args.listener_fd,),
        start_new_session=True,
    )
    os.close(args.listener_fd)
    try:
        deadline = time.monotonic() + 0.1
        while time.monotonic() < deadline and provider.poll() is None and not stopping:
            time.sleep(0.01)
        if provider.poll() is not None:
            return provider.returncode
        if stopping:
            stop_group(provider)
            return 0
        os.write(args.ready_fd, b"1")
        os.close(args.ready_fd)
        while not stopping:
            status = provider.poll()
            if status is not None:
                return status
            readable, _writable, _errors = select.select([args.lifetime_fd], [], [], 0.1)
            if readable and not os.read(args.lifetime_fd, 1):
                break
        stop_group(provider)
        return 0
    finally:
        try:
            os.close(args.ready_fd)
        except OSError:
            pass
        os.close(args.lifetime_fd)
        stop_group(provider)


if __name__ == "__main__":
    raise SystemExit(main())
