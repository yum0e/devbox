#!/usr/bin/env sh
set -eu

# Herdr may start commands with a reduced environment. The socket is a public
# capability path backed by the selected-key-only proxy, never a raw host agent.
export SSH_AUTH_SOCK=/run/devc2-ssh/agent.sock
exec /usr/local/libexec/devc2/tact-upstream "$@"
