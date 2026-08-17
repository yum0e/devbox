"""Compose the Herdr World and one Docker Agent behind a private worker Link."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

AGENT_ENVIRONMENT = (
    "HOME", "PATH", "LANG", "LC_ALL", "TMPDIR",
    "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
    "DEVC2_WORKSPACE",
)
HERDR_ENVIRONMENT = (
    "HOME", "PATH", "LANG", "LC_ALL", "TMPDIR",
    "HERDR_ENV", "HERDR_SOCKET_PATH", "HERDR_PANE_ID",
    "DEVC2_SPAN_SOCKET_FD",
)


def environment(*names: str) -> dict[str, str]:
    return {name: os.environ[name] for name in names if os.environ.get(name)}


def stop(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def provider() -> int:
    try:
        span_descriptor = int(os.environ["DEVC2_SPAN_SOCKET_FD"])
    except (KeyError, ValueError) as error:
        raise RuntimeError("missing Herdr Span listener") from error
    archive = Path(sys.argv[0]).resolve(strict=True)
    private = Path(tempfile.mkdtemp(prefix="devc2-herdr-agent-"))
    private.chmod(0o700)
    worker_path = private / "worker.sock"
    agent_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    agent_listener.bind(os.fspath(worker_path))
    worker_path.chmod(0o600)
    agent_listener.listen(16)
    world_status, agent_status = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    agent_environment = environment(*AGENT_ENVIRONMENT)
    agent_environment["DEVC2_HERDR_AGENT_FD"] = str(agent_listener.fileno())
    agent_environment["DEVC2_HERDR_STATUS_FD"] = str(agent_status.fileno())
    herdr_environment = environment(*HERDR_ENVIRONMENT)
    herdr_environment.update({
        "DEVC2_HERDR_WORKER": os.fspath(archive),
        "DEVC2_HERDR_WORKER_SOCKET": os.fspath(worker_path),
        "DEVC2_HERDR_STATUS_FD": str(world_status.fileno()),
    })
    agent = herdr = None
    stopping = threading.Event()

    def request_stop(_signum=None, _frame=None):
        stopping.set()

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, request_stop)
    try:
        agent = subprocess.Popen(
            [sys.executable, os.fspath(archive), "agent"],
            env=agent_environment, stdin=subprocess.DEVNULL,
            pass_fds=(agent_listener.fileno(), agent_status.fileno()),
        )
        agent_listener.close()
        agent_status.close()
        herdr = subprocess.Popen(
            [sys.executable, os.fspath(archive), "herdr"],
            env=herdr_environment, stdin=subprocess.DEVNULL,
            pass_fds=(span_descriptor, world_status.fileno()),
        )
        world_status.close()
        os.close(span_descriptor)
        span_descriptor = -1
        while not stopping.wait(0.1):
            if agent.poll() is not None:
                return agent.returncode or 1
            if herdr.poll() is not None:
                return herdr.returncode or 1
        return 0
    finally:
        # Herdr closes every pane it owns before the Agent revokes workers.
        stop(herdr)
        stop(agent)
        agent_listener.close()
        world_status.close()
        agent_status.close()
        if span_descriptor >= 0:
            os.close(span_descriptor)
        worker_path.unlink(missing_ok=True)
        private.rmdir()


def main() -> int:
    if not sys.argv[1:]:
        return provider()
    if sys.argv[1:] == ["herdr"]:
        import herdr_world
        return herdr_world.serve()
    if sys.argv[1:] == ["agent"]:
        import docker_agent
        return docker_agent.main()
    if len(sys.argv) == 3 and sys.argv[1] == "worker":
        import worker_link
        return worker_link.run(sys.argv[2])
    print("herdr-span: invalid bundle invocation", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as error:
        print(f"herdr-span: {error}", file=sys.stderr)
        raise SystemExit(1)
