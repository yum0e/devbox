"""Validate and compose launch-lifetime HTTP World attachment manifests."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import ssl
import stat
import struct


HEADER = struct.Struct("!I")
MAX_MANIFEST = 2 * 1024 * 1024
MAX_CA = 1024 * 1024
MAX_FILES = 16
MAX_FILE_BYTES = 1024 * 1024
MAX_ENVIRONMENT = 32
MAX_ENV_NAME = 255
MAX_ENV_VALUE = 64 * 1024
MAX_ROUTES = 32
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ENV_VALUE = re.compile(r"^[A-Za-z0-9_./,:@+\[\]-]*$")
DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
PLACEHOLDER = re.compile(r"\$\{[^}]*\}")
BOX_ROOT = "/tmp/devc2-http-attachments"
BOX_CA = "/tmp/devc2-http-ca.pem"
BOX_PROXY = "http://span-proxy:3128"


class AttachmentError(RuntimeError):
    pass


def receive(connection: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        block = connection.recv(size - len(result))
        if not block:
            raise AttachmentError("World manifest ended early")
        result.extend(block)
    return bytes(result)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AttachmentError("duplicate JSON object field")
        result[key] = value
    return result


def decode_b64(value: object, label: str) -> bytes:
    if not isinstance(value, str):
        raise AttachmentError(f"invalid {label}")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as error:
        raise AttachmentError(f"invalid {label}") from error


def valid_dns(host: str) -> bool:
    return bool(host) and len(host) <= 253 and all(
        DNS_LABEL.fullmatch(label) for label in host.split(".")
    )


def validate_file_name(value: object) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise AttachmentError("invalid manifest file name")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise AttachmentError("invalid manifest file name")
    return value


def validate_manifest(raw: bytes):
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AttachmentError("World returned invalid manifest JSON") from error
    required = {"v", "ca_b64", "files", "environment", "routes"}
    if (not isinstance(document, dict) or set(document) != required
            or type(document["v"]) is not int or document["v"] != 1):
        raise AttachmentError("invalid World manifest schema")

    certificate = decode_b64(document["ca_b64"], "World CA")
    if not certificate or len(certificate) > MAX_CA or b"PRIVATE KEY" in certificate.upper():
        raise AttachmentError("invalid World CA")
    try:
        pem = certificate.decode("ascii")
        if "-----BEGIN CERTIFICATE-----" not in pem or "-----END CERTIFICATE-----" not in pem:
            raise ValueError
        ssl.create_default_context(cadata=pem)
    except (UnicodeError, ValueError, ssl.SSLError) as error:
        raise AttachmentError("World CA is not a valid public PEM certificate") from error

    files = []
    names = set()
    total = 0
    raw_files = document["files"]
    if not isinstance(raw_files, list) or len(raw_files) > MAX_FILES:
        raise AttachmentError("invalid manifest files")
    for entry in raw_files:
        if not isinstance(entry, dict) or set(entry) != {"name", "contents_b64"}:
            raise AttachmentError("invalid manifest file")
        name = validate_file_name(entry["name"])
        if name in names or any(name.startswith(old + "/") or old.startswith(name + "/") for old in names):
            raise AttachmentError("duplicate or conflicting manifest file name")
        contents = decode_b64(entry["contents_b64"], "manifest file contents")
        total += len(contents)
        if total > MAX_FILE_BYTES:
            raise AttachmentError("manifest files exceed 1 MiB")
        names.add(name)
        files.append((name, contents))

    environment = document["environment"]
    if not isinstance(environment, dict) or len(environment) > MAX_ENVIRONMENT:
        raise AttachmentError("invalid manifest environment")
    for key, value in environment.items():
        literal_value = value if isinstance(value, str) else ""
        for placeholder in ("${ROOT}", "${CA}", "${PROXY}"):
            literal_value = literal_value.replace(placeholder, "")
        if (not isinstance(key, str) or not ENV_NAME.fullmatch(key)
                or len(key) > MAX_ENV_NAME or not isinstance(value, str)
                or "\0" in value or len(value.encode("utf-8")) > MAX_ENV_VALUE
                or not ENV_VALUE.fullmatch(literal_value)):
            raise AttachmentError("invalid manifest environment entry")

    routes = document["routes"]
    if not isinstance(routes, list) or len(routes) > MAX_ROUTES:
        raise AttachmentError("invalid manifest routes")
    seen_routes = set()
    for route in routes:
        if not isinstance(route, str) or not route.endswith(":443"):
            raise AttachmentError("invalid manifest route")
        host = route[:-4]
        if not valid_dns(host) or route != route.lower() or route in seen_routes:
            raise AttachmentError("invalid manifest route")
        seen_routes.add(route)
    return certificate, files, environment, routes


def bootstrap(address: tuple[str, int]):
    with socket.create_connection(address, timeout=30) as connection:
        connection.sendall(b"B")
        size = HEADER.unpack(receive(connection, HEADER.size))[0]
        if not size or size > MAX_MANIFEST:
            raise AttachmentError("World manifest exceeds 2 MiB")
        return validate_manifest(receive(connection, size))


def private_write(path: Path, contents: bytes, mode: int = 0o444) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        path.chmod(mode)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def expand_environment(raw: dict, name: str) -> dict[str, str]:
    replacements = {
        "${ROOT}": f"{BOX_ROOT}/{name}",
        "${CA}": BOX_CA,
        "${PROXY}": BOX_PROXY,
    }
    result = {}
    for key, raw_value in raw.items():
        value = raw_value
        for token, replacement in replacements.items():
            value = value.replace(token, replacement)
        if PLACEHOLDER.search(value):
            raise AttachmentError("unresolved manifest environment placeholder")
        result[key] = value
    return result


def materialize(public: Path, worlds: list[tuple[str, tuple[str, int]]]) -> dict[str, str]:
    template_root = public / "http-attachments"
    template_root.mkdir(mode=0o755)
    environment: dict[str, str] = {}
    routes: dict[str, str] = {}
    certificates = []
    for name, address in worlds:
        certificate, files, raw_environment, raw_routes = bootstrap(address)
        root = template_root / name
        root.mkdir(mode=0o755)
        for relative, contents in files:
            target = root.joinpath(*relative.split("/"))
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            private_write(target, contents)
        certificates.append(certificate.rstrip(b"\n"))
        for key, value in expand_environment(raw_environment, name).items():
            previous = environment.get(key)
            if previous is not None and previous != value:
                raise AttachmentError(f"conflicting attached environment variable: {key}")
            environment[key] = value
        for route in raw_routes:
            if route in routes:
                raise AttachmentError(f"conflicting attached HTTPS route: {route}")
            routes[route] = name

    private_write(
        public / "http-routes.json",
        (json.dumps({"v": 1, "routes": routes}, separators=(",", ":")) + "\n").encode(),
    )
    private_write(
        public / "world-environment.json",
        (json.dumps(environment, separators=(",", ":")) + "\n").encode(),
    )
    world_env = public / "world.env"
    world_env.unlink(missing_ok=True)
    private_write(
        world_env,
        "".join(f"{key}={value}\n" for key, value in sorted(environment.items())).encode(),
        0o600,
    )
    private_write(
        public / "http-ca.pem",
        (b"\n".join(certificates) + (b"\n" if certificates else b"")),
    )
    return environment
