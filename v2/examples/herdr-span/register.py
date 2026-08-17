#!/usr/bin/env python3
"""Install one immutable Herdr World generation and register it atomically."""
from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import zipfile
from pathlib import Path


NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
MAX_BYTES = 64 * 1024 * 1024
MAX_CATALOG_BYTES = 64 * 1024
BUNDLE_FILES = {
    "__main__.py": "bundle.py",
    "herdr_world.py": "herdr-span",
    "docker_agent.py": "docker_agent.py",
    "island_supervisor.py": "island_supervisor.py",
    "worker_link.py": "worker_link.py",
}


def read_file(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_BYTES:
            raise RuntimeError(f"invalid executable source: {path}")
        payload = source.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise RuntimeError(f"executable source exceeds 64 MiB: {path}")
    return payload


def write_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o500)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    path.chmod(0o555)


def build_bundle(root: Path) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for destination, source in BUNDLE_FILES.items():
            info = zipfile.ZipInfo(destination, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            archive.writestr(info, read_file(root / source))
    payload = b"#!/usr/bin/env python3\n" + output.getvalue()
    if len(payload) > MAX_BYTES:
        raise RuntimeError("Herdr bundle exceeds 64 MiB")
    return payload


def checked_directory(path: Path, label: str) -> None:
    metadata = path.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077):
        raise RuntimeError(f"{label} must be a current-user-owned private directory: {path}")


def checked_generation(generation: Path, world: bytes) -> None:
    checked_directory(generation, "Herdr generation")
    if {item.name for item in generation.iterdir()} != {"world"}:
        raise RuntimeError(f"existing Herdr generation is inconsistent: {generation}")
    path = generation / "world"
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o555
                or source.read(MAX_BYTES + 1) != world):
            raise RuntimeError(f"existing Herdr generation is inconsistent: {generation}")


def install_generation(root: Path, world: bytes) -> Path:
    digest = hashlib.sha256(b"world\0" + world).hexdigest()[:16]
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = root.resolve(strict=True)
    checked_directory(root, "Herdr install root")
    generation = root / digest
    if generation.exists():
        checked_generation(generation, world)
        return generation
    staging = Path(tempfile.mkdtemp(prefix=".herdr.", dir=root))
    try:
        write_file(staging / "world", world)
        directory = os.open(staging, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        try:
            os.replace(staging, generation)
        except FileExistsError:
            pass
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    checked_generation(generation, world)
    return generation


def read_catalog(path: Path) -> dict:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return {"spans": {}}
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
            raise RuntimeError("Span catalog must be current-user-owned and not group/world-writable")
        payload = source.read(MAX_CATALOG_BYTES + 1)
    if len(payload) > MAX_CATALOG_BYTES:
        raise RuntimeError("Span catalog exceeds 64 KiB")
    document = json.loads(payload)
    if not isinstance(document, dict) or set(document) != {"spans"} or not isinstance(document["spans"], dict):
        raise RuntimeError("Span catalog must contain only a spans object")
    for name in document["spans"]:
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise RuntimeError("Span catalog contains an invalid name")
    return document


def write_catalog(path: Path, document: dict) -> None:
    payload = (json.dumps(document, sort_keys=True, indent=2) + "\n").encode()
    if len(payload) > MAX_CATALOG_BYTES:
        raise RuntimeError("updated Span catalog would exceed 64 KiB")
    descriptor, raw = tempfile.mkstemp(prefix=".spans.", dir=path.parent)
    temporary = Path(raw)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: register.py CATALOG INSTALL_ROOT SOURCE_ROOT", file=sys.stderr)
        return 2
    catalog, install_root, source_root = map(Path, sys.argv[1:])
    generation = install_generation(install_root, build_bundle(source_root))
    lock_path = catalog.with_suffix(".lock")
    lock = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(lock, "a+") as locked:
        os.fchmod(locked.fileno(), 0o600)
        fcntl.flock(locked, fcntl.LOCK_EX)
        document = read_catalog(catalog)
        document["spans"]["herdr"] = str(generation / "world")
        if len(document["spans"]) > 64:
            raise RuntimeError("Span catalog contains too many entries")
        write_catalog(catalog, document)
    print(f"installed immutable Herdr generation: {generation}")
    print(f"registered Herdr command Link: {catalog}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"herdr-span installer: {error}", file=sys.stderr)
        raise SystemExit(1)
