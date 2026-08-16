#!/usr/bin/env bash
set -euo pipefail
root="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
cd "$root/.."
PYTHONPATH=v2 uv run --no-project python -m unittest discover -s v2/tests -v
uv run --no-project python -m py_compile v2/launcher/dispatcher.py v2/launcher/span_runtime.py v2/launcher/span_supervisor.py v2/launcher/command_projection.py v2/launcher/scoped_exec_projection.py v2/credential_proxy/span_bridge.py v2/credential_proxy/stream_relay.py v2/spans/openai/world v2/spans/github/world v2/spans/ssh-agent/world v2/spans/diagnostics/world v2/examples/herdr-span/herdr-span v2/examples/herdr-span/register.py v2/package-release.py
bash -n v2/install.sh v2/install-release.sh v2/devbox/entrypoint.sh v2/examples/herdr-span/install.sh
bash tests/test_devc_multiplexer.sh
