#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/devc2"
span_root="${DEVC2_SPAN_ROOT:-$HOME/.local/lib/devc2-spans/herdr}"
mkdir -p -m 700 "$config_dir"
python3 "$root/register.py" "$config_dir/spans.json" "$span_root" "$root/herdr-span"
