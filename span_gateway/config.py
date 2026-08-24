"""Shared validation and bounded config loading for the Span gateway."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


MAX_CONFIG = 64 * 1024
NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


def valid_span_name(value: object) -> bool:
    return isinstance(value, str) and NAME.fullmatch(value) is not None


def read_json(path: Path, too_large_error: Exception) -> Any:
    """Read at most one byte beyond the config limit, then decode JSON."""
    with path.open("rb") as source:
        encoded = source.read(MAX_CONFIG + 1)
    if len(encoded) > MAX_CONFIG:
        raise too_large_error
    return json.loads(encoded.decode("utf-8"))
