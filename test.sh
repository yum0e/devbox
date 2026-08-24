#!/usr/bin/env bash
set -euo pipefail
root="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
cd "$root"
PYTHONPATH="$root" uv run --no-project python -m unittest discover -s tests -v
uv run --no-project python -m py_compile launcher/asset_contract.py launcher/dispatcher.py launcher/span_runtime.py launcher/span_supervisor.py launcher/command_projection.py launcher/world_attachment.py credential_proxy/config.py credential_proxy/span_bridge.py credential_proxy/stream_relay.py credential_proxy/http_projection.py spans/http-credential-world spans/ssh-agent/world spans/diagnostics/world package-release.py
bash -n install.sh install-release.sh devbox/entrypoint.sh devbox/configure-pi.sh devbox/configure-signing.sh devbox/configure-herdr.sh devbox/configure-gh-stack.sh devbox/herdr-git-metadata
