#!/usr/bin/env bash
set -euo pipefail
root="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
cd "$root/.."
PYTHONPATH=v2 uv run --no-project python -m unittest discover -s v2/tests -v
PYTHONPATH=v2 uv run --no-project python -m unittest discover -s v2/credential_proxy/tests -v
bash -n v2/install.sh v2/devbox/entrypoint.sh
bash tests/test_devc_multiplexer.sh
