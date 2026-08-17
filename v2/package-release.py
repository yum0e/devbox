#!/usr/bin/env python3
"""Build deterministic devc2 GitHub Release assets."""
import gzip
import hashlib
import io
import os
import shutil
import tarfile
from pathlib import Path

ROOT=Path(__file__).resolve().parent
REPO=ROOT.parent
DIST=REPO/"dist"
INCLUDE=("Dockerfile","compose.yaml","README.md","install.sh","devbox","credential_proxy","launcher","spans")
EXECUTABLE={"install.sh","devbox/entrypoint.sh","devbox/configure-pi.sh","launcher/devc2.py","launcher/dispatcher.py","launcher/command_projection.py","spans/http-credential-world","spans/ssh-agent/world","spans/diagnostics/world"}

def files():
    result=[]
    for name in INCLUDE:
        path=ROOT/name
        if path.is_symlink(): raise RuntimeError(f"refusing to package symlink: {path}")
        if path.is_dir():
            for candidate in path.rglob("*"):
                if candidate.is_symlink(): raise RuntimeError(f"refusing to package symlink: {candidate}")
                if candidate.is_file() and "__pycache__" not in candidate.parts and "tests" not in candidate.parts: result.append(candidate)
        elif path.is_file(): result.append(path)
    return sorted(result,key=lambda p:str(p.relative_to(ROOT)))

def main():
    DIST.mkdir(exist_ok=True)
    archive=DIST/"devc2.tar.gz"
    with archive.open("wb") as raw:
        with gzip.GzipFile(filename="",mode="wb",fileobj=raw,mtime=0,compresslevel=9) as compressed:
            with tarfile.open(fileobj=compressed,mode="w",format=tarfile.USTAR_FORMAT) as package:
                directories={Path("devc2")}
                for path in files():
                    relative=path.relative_to(ROOT)
                    for parent in relative.parents:
                        if str(parent)!=".": directories.add(Path("devc2")/parent)
                for directory in sorted(directories,key=str):
                    info=tarfile.TarInfo(str(directory)+"/"); info.type=tarfile.DIRTYPE; info.mode=0o755; info.uid=info.gid=0; info.uname=info.gname=""; info.mtime=0
                    package.addfile(info)
                for path in files():
                    relative=path.relative_to(ROOT); data=path.read_bytes(); info=tarfile.TarInfo(str(Path("devc2")/relative)); info.size=len(data); info.mode=0o755 if str(relative) in EXECUTABLE else 0o644; info.uid=info.gid=0; info.uname=info.gname=""; info.mtime=0
                    package.addfile(info,io.BytesIO(data))
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    (DIST/"devc2.tar.gz.sha256").write_text(f"{digest}  devc2.tar.gz\n",encoding="ascii")
    shutil.copyfile(ROOT/"install-release.sh",DIST/"install-devc2.sh"); (DIST/"install-devc2.sh").chmod(0o755)
    print(archive); print(digest)

if __name__=="__main__": main()
