"""Container-side owner for one Herdr worker process group."""
from __future__ import annotations

import os
import pty
import ctypes
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time


FRAME = struct.Struct("!cI")
MAX_FRAME = 64 * 1024
HEARTBEAT_TIMEOUT = 3
PR_SET_CHILD_SUBREAPER = 36


def read_exact(descriptor: int, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        block = os.read(descriptor, size - len(result))
        if not block:
            raise EOFError
        result.extend(block)
    return bytes(result)


def write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        view = view[os.write(descriptor, view):]


def send(kind: bytes, payload: bytes = b"") -> None:
    write_all(1, FRAME.pack(kind, len(payload)) + payload)


def enable_subreaper() -> None:
    if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "could not become a child subreaper")


def descendants() -> set[int]:
    parents = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", encoding="ascii") as stat_file:
                stat_line = stat_file.read()
            fields = stat_line[stat_line.rfind(")") + 2:].split()
            parents[int(name)] = int(fields[1])
        except (FileNotFoundError, IndexError, OSError, ValueError):
            continue
    result = set()
    frontier = {os.getpid()}
    while frontier:
        found = {pid for pid, parent in parents.items() if parent in frontier and pid not in result}
        result.update(found)
        frontier = found
    return result


def reap(exclude: int) -> None:
    for pid in descendants() - {exclude}:
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass


def stop_group(process: subprocess.Popen[bytes]) -> None:
    try:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    for pid in descendants():
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        direct_done = process.poll() is not None
        reap(process.pid)
        if direct_done and not descendants():
            return
        time.sleep(0.05)
    try:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    for pid in descendants():
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def main(argv: list[str]) -> int:
    if len(argv) < 4 or argv[2] != "--":
        return 125
    try:
        rows, columns = int(argv[0]), int(argv[1])
    except ValueError:
        return 125
    if not 1 <= rows <= 1000 or not 1 <= columns <= 1000:
        return 125
    command = argv[3:]
    enable_subreaper()
    master, slave = pty.openpty()
    termios.tcsetwinsize(slave, (rows, columns))
    environment = os.environ.copy()
    environment["TERM"] = "xterm-256color"
    environment["COLORTERM"] = "truecolor"
    process = subprocess.Popen(
        command, stdin=slave, stdout=slave, stderr=slave,
        env=environment, start_new_session=True,
    )
    os.close(slave)
    stopping = threading.Event()
    heartbeat = [time.monotonic()]

    def request_stop(_signum: int | None = None, _frame: object = None) -> None:
        stopping.set()

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, request_stop)

    def input_loop() -> None:
        try:
            while not stopping.is_set():
                kind, size = FRAME.unpack(read_exact(0, FRAME.size))
                if size > MAX_FRAME or kind not in (b"I", b"H", b"C"):
                    break
                payload = read_exact(0, size)
                heartbeat[0] = time.monotonic()
                if kind == b"I":
                    write_all(master, payload)
                elif kind == b"C":
                    break
            stopping.set()
        except (EOFError, OSError, struct.error):
            stopping.set()

    reader = threading.Thread(target=input_loop, daemon=True)
    reader.start()
    code = 125
    try:
        send(b"R")
        while process.poll() is None:
            if stopping.is_set() or time.monotonic() - heartbeat[0] > HEARTBEAT_TIMEOUT:
                stop_group(process)
                break
            readable, _writable, _errors = select.select([master], [], [], 0.1)
            if readable:
                try:
                    block = os.read(master, MAX_FRAME)
                except OSError:
                    break
                if block:
                    send(b"O", block)
        stop_group(process)
        try:
            code = process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            stop_group(process)
            code = process.wait(timeout=2)
        while select.select([master], [], [], 0)[0]:
            try:
                block = os.read(master, MAX_FRAME)
            except OSError:
                break
            if not block:
                break
            send(b"O", block)
        code = code if 0 <= code <= 255 else 255
        send(b"E", bytes([code]))
        return code
    finally:
        stopping.set()
        stop_group(process)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        os.close(master)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
