#!/usr/bin/env python3
"""Stable devc2 dispatcher: lock the installation and select one immutable runtime."""
import fcntl
import json
import os
import stat
import sys
from pathlib import Path

def xdg(name,default): return Path(os.environ.get(name,Path.home()/default)).expanduser()

def record_runtime(data,management):
    record=data/"installation.json"
    try: metadata=record.lstat()
    except FileNotFoundError:
        pointer="manager" if management else "current"
        try: return (data/pointer).resolve(strict=True)
        except OSError as error: raise RuntimeError("installed runtime pointer is missing; reinstall devc2") from error
    except OSError as error: raise RuntimeError("installed runtime record cannot be read; reinstall devc2") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("installed runtime record is invalid; reinstall devc2")
    if metadata.st_size>64*1024 or metadata.st_uid!=os.getuid() or metadata.st_mode & 0o022:
        raise RuntimeError("installed runtime record is invalid; reinstall devc2")
    try: document=json.loads(record.read_text(encoding="utf-8"))
    except (OSError,ValueError) as error: raise RuntimeError("installed runtime record is invalid; reinstall devc2") from error
    if not isinstance(document,dict) or document.get("schema")!=2:
        raise RuntimeError("installed runtime record is invalid; reinstall devc2")
    for required in ("current","manager","version","bin_dir","share_dir","install_kind"):
        value=document.get(required)
        if not isinstance(value,str) or not value:
            raise RuntimeError("installed runtime record is invalid; reinstall devc2")
    for path_key in ("current","manager","bin_dir","share_dir"):
        if not Path(document[path_key]).is_absolute():
            raise RuntimeError("installed runtime record is invalid; reinstall devc2")
    if Path(document["share_dir"])!=data:
        raise RuntimeError("installed runtime record is invalid; reinstall devc2")
    previous=document.get("previous")
    if previous is not None and (not isinstance(previous,str) or not Path(previous).is_absolute()):
        raise RuntimeError("installed runtime record is invalid; reinstall devc2")
    for optional in ("previous_version","source_dir","previous_install_kind","previous_source_dir"):
        if document.get(optional) is not None and not isinstance(document[optional],str):
            raise RuntimeError("installed runtime record is invalid; reinstall devc2")
    roles=[document["current"],document["manager"]]
    if previous is not None: roles.append(previous)
    for value in roles:
        runtime=Path(value)
        try: resolved=runtime.resolve(strict=True)
        except OSError as error: raise RuntimeError("installed runtime record is invalid; reinstall devc2") from error
        if resolved!=runtime or not runtime.is_dir() or runtime.is_symlink():
            raise RuntimeError("installed runtime record is invalid; reinstall devc2")
        launcher=runtime/"launcher"/"devc2.py"
        if not launcher.is_file() or launcher.is_symlink():
            raise RuntimeError("installed runtime record is invalid; reinstall devc2")
    if document["manager"] not in {document["current"],previous}:
        raise RuntimeError("installed runtime record is invalid; reinstall devc2")
    key="manager" if management else "current"
    value=document[key]
    runtime=Path(value)
    try: resolved=runtime.resolve(strict=True)
    except OSError as error: raise RuntimeError("installed runtime record is invalid; reinstall devc2") from error
    if resolved!=runtime or not runtime.is_dir() or runtime.is_symlink():
        raise RuntimeError("installed runtime record is invalid; reinstall devc2")
    return runtime

def main():
    data=Path(os.environ.get("DEVC2_DATA_DIR",Path.home()/".local/share/devc2")).expanduser().absolute()
    state=xdg("XDG_STATE_HOME",".local/state")/"devc2"
    state.mkdir(parents=True,exist_ok=True,mode=0o700)
    lock_path=state/"install.lock"
    descriptor=os.open(lock_path,os.O_RDWR|os.O_CREAT,0o600)
    command=sys.argv[1] if len(sys.argv)>1 else ""
    exclusive=command in {"rollback","_install-assets"}
    management=command in {"update","rollback"}
    mode=fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try: fcntl.flock(descriptor,mode|fcntl.LOCK_NB)
    except BlockingIOError:
        action="change the devc2 installation while a session is active" if exclusive else "start or update devc2 while installation management is active"
        print(f"devc2: error: cannot {action}",file=sys.stderr)
        return 1
    try: runtime=record_runtime(data,management)
    except RuntimeError as error:
        print(f"devc2: error: {error}",file=sys.stderr)
        return 1
    launcher=runtime/"launcher"/"devc2.py"
    if not launcher.is_file() or launcher.is_symlink():
        print("devc2: error: installed runtime is invalid; reinstall devc2",file=sys.stderr)
        return 1
    os.set_inheritable(descriptor,True)
    environment=dict(os.environ)
    environment["DEVC2_RUNTIME_ROOT"]=str(runtime)
    environment["DEVC2_CONTROL_LOCK_FD"]=str(descriptor)
    environment["DEVC2_CONTROL_LOCK_MODE"]="exclusive" if exclusive else "shared"
    os.execve(sys.executable,[sys.executable,str(launcher),*sys.argv[1:]],environment)

if __name__=="__main__": raise SystemExit(main())
