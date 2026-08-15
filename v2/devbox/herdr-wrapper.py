#!/usr/bin/env python3
"""Legacy release marker; deliberately excluded from the Island image."""

import sys

print(
    "herdr is no longer bundled; install and explicitly grant a Herdr Span",
    file=sys.stderr,
)
raise SystemExit(127)
