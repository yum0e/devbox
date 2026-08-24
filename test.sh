#!/usr/bin/env bash
set -euo pipefail
root="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
cd "$root"
PYTHONPATH="$root" uv run --no-project python -m unittest discover -s tests -v
uv run --no-project python -m py_compile launcher/asset_contract.py launcher/dispatcher.py launcher/span_runtime.py launcher/span_supervisor.py launcher/command_adapter.py launcher/http_attachment.py span_gateway/config.py span_gateway/gateway.py span_gateway/stream_relay.py span_gateway/http_connect_proxy.py spans/http-credential-world spans/ssh-agent/world spans/diagnostics/world package-release.py
bash -n install.sh install-release.sh box/entrypoint.sh box/configure-pi.sh box/configure-signing.sh box/configure-herdr.sh box/configure-gh-stack.sh box/herdr-git-metadata
