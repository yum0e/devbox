#!/usr/bin/env bash
set -euo pipefail
root="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
cd "$root/.."
PYTHONPATH=v2 uv run --no-project python -m unittest discover -s v2/tests -v
uv run --no-project python -m py_compile v2/launcher/dispatcher.py v2/launcher/span_runtime.py v2/launcher/span_supervisor.py v2/launcher/command_projection.py v2/credential_proxy/span_bridge.py v2/spans/openai/provider v2/spans/openai/client v2/spans/github/provider v2/spans/github/client v2/spans/ssh-agent/provider v2/spans/ssh-agent/client v2/examples/probe-span/probe-span v2/examples/probe-span/probe-span-client v2/examples/probe-span/register.py v2/examples/herdr-span/herdr-span v2/examples/herdr-span/register.py v2/package-release.py
bash -n v2/install.sh v2/install-release.sh v2/devbox/entrypoint.sh v2/examples/probe-span/install.sh v2/examples/herdr-span/install.sh
bash tests/test_devc_multiplexer.sh
