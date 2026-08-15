#!/usr/bin/env bash
set -euo pipefail

root="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
bin_dir="${DEVC2_BIN_DIR:-$HOME/.local/bin}"
mkdir -p "$bin_dir"
install -m 0755 "$root/probe-span" "$bin_dir/probe-span"
install -m 0755 "$root/probe-span-client" "$bin_dir/probe-span-client"

printf 'installed probe-span provider: %s\n' "$bin_dir/probe-span"
printf 'installed probe Span client source: %s\n' "$bin_dir/probe-span-client"
if [[ ":$PATH:" != *":$bin_dir:"* ]]; then
  printf 'note: add %s to the host PATH before running devc2\n' "$bin_dir"
fi
