#!/usr/bin/env sh
set -eu

# Restore the public capability path backed by the selected-key-only proxy,
# never a raw host agent, even when a launcher supplies a reduced environment.
export SSH_AUTH_SOCK=/run/devc2-ssh/agent.sock
exec /usr/local/libexec/devc2/tact-upstream "$@"
