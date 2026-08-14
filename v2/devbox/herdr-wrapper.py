#!/usr/bin/env python3
"""Run pinned Herdr and normalize its documented clean server-shutdown exit."""
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

UPSTREAM=Path("/usr/local/libexec/devc2/herdr-upstream")
CLEAN_SHUTDOWN=b"herdr: server shut down: server is shutting down"

def main():
    process=subprocess.Popen([str(UPSTREAM),*sys.argv[1:]],stdin=None,stdout=None,stderr=subprocess.PIPE)
    clean=threading.Event()
    def copy_stderr():
        assert process.stderr is not None
        for line in iter(process.stderr.readline,b""):
            sys.stderr.buffer.write(line); sys.stderr.buffer.flush()
            if line.rstrip(b"\r\n")==CLEAN_SHUTDOWN: clean.set()
    pump=threading.Thread(target=copy_stderr,daemon=True); pump.start()
    def forward(signum,_frame):
        try: process.send_signal(signum)
        except ProcessLookupError: pass
    previous={signum:signal.signal(signum,forward) for signum in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP)}
    try: status=process.wait()
    finally:
        pump.join(timeout=2)
        for signum,handler in previous.items(): signal.signal(signum,handler)
    # Herdr v0.8 returns 1 for a normal server quit, while detach returns 0.
    # Normalize only the exact trusted v0.8 shutdown diagnostic; retain every
    # other nonzero status so startup failures and crashes remain visible.
    return 0 if status==1 and clean.is_set() else status

if __name__=="__main__": raise SystemExit(main())
