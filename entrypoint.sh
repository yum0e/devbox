#!/usr/bin/env bash
set -euo pipefail

REAL_SSH_AUTH_SOCK="/run/host-services/ssh-auth.sock"
PROXY_SSH_AUTH_SOCK="/tmp/ssh-agent-node.sock"

# Recreate proxy socket on every container start.
rm -f "${PROXY_SSH_AUTH_SOCK}"

# Only start the proxy if Docker Desktop exposed the host SSH agent socket.
if [ -S "${REAL_SSH_AUTH_SOCK}" ]; then
  socat \
    UNIX-LISTEN:"${PROXY_SSH_AUTH_SOCK}",fork,user=node,group=node,mode=600 \
    UNIX-CONNECT:"${REAL_SSH_AUTH_SOCK}" &
fi

if [ "$#" -eq 0 ]; then
  exec sleep infinity
else
  exec "$@"
fi