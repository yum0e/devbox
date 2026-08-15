#!/usr/bin/env bash
set -euo pipefail

root="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/devc2"
install -d -m 0700 "$config_dir"
span_root="${DEVC2_SPAN_ROOT:-$HOME/.local/lib/devc2-spans/probe}"
python3 "$root/register.py" "$config_dir/spans.json" "$span_root" "$root/probe-span" "$root/probe-span-client"
