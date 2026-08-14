#!/usr/bin/env python3
"""Stable devc2 dispatcher: lock the installation and select one immutable runtime."""
import fcntl
import os
import sys
from pathlib import Path

def xdg(name,default): return Path(os.environ.get(name,Path.home()/default)).expanduser()

def main():
    data=Path(os.environ.get("DEVC2_DATA_DIR",Path.home()/".local/share/devc2")).expanduser()
    state=xdg("XDG_STATE_HOME",".local/state")/"devc2"
    state.mkdir(parents=True,exist_ok=True,mode=0o700)
    lock_path=state/"install.lock"
    descriptor=os.open(lock_path,os.O_RDWR|os.O_CREAT,0o600)
    command=sys.argv[1] if len(sys.argv)>1 else ""
    management=command in {"update","rollback","_install-assets"}
    mode=fcntl.LOCK_EX if management else fcntl.LOCK_SH
    try: fcntl.flock(descriptor,mode|fcntl.LOCK_NB)
    except BlockingIOError:
        action="update devc2 while a session is active" if management else "start devc2 while an install or update is active"
        print(f"devc2: error: cannot {action}",file=sys.stderr)
        return 1
    pointer="manager" if command in {"update","rollback"} else "current"
    try: runtime=(data/pointer).resolve(strict=True)
    except OSError:
        print("devc2: error: installed runtime pointer is missing; reinstall devc2",file=sys.stderr)
        return 1
    launcher=runtime/"launcher"/"devc2.py"
    if not launcher.is_file() or launcher.is_symlink():
        print("devc2: error: installed runtime is invalid; reinstall devc2",file=sys.stderr)
        return 1
    os.set_inheritable(descriptor,True)
    environment=dict(os.environ)
    environment["DEVC2_RUNTIME_ROOT"]=str(runtime)
    environment["DEVC2_CONTROL_LOCK_FD"]=str(descriptor)
    environment["DEVC2_CONTROL_LOCK_MODE"]="exclusive" if management else "shared"
    os.execve(sys.executable,[sys.executable,str(launcher),*sys.argv[1:]],environment)

if __name__=="__main__": raise SystemExit(main())
