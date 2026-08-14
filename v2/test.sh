#!/usr/bin/env bash
set -euo pipefail
root="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
cd "$root/.."
PYTHONPATH=v2 uv run --no-project python -m unittest discover -s v2/tests -v
PYTHONPATH=v2 uv run --no-project python -m unittest discover -s v2/credential_proxy/tests -v
uv run --no-project python -m py_compile v2/launcher/dispatcher.py v2/devbox/herdr-wrapper.py v2/package-release.py
bash -n v2/install.sh v2/install-release.sh v2/devbox/entrypoint.sh
bash tests/test_devc_multiplexer.sh
