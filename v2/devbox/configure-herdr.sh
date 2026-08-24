#!/usr/bin/env bash
set -euo pipefail

readonly managed_config="${DEVC2_HERDR_MANAGED_CONFIG:-/usr/local/share/devc2-herdr-config.toml}"
readonly config="${HERDR_CONFIG_PATH:-$HOME/.config/herdr/config.toml}"

if [[ ! -r "$managed_config" ]]; then
  echo "devc2: managed Herdr config is unavailable: $managed_config" >&2
  exit 1
fi

# Images built before the Herdr config directories were explicitly created
# seeded fresh home volumes with mode 0600 directories. Repair only that managed
# path and only real directories; never follow or modify user-managed symlinks.
if [[ "$config" == "$HOME/.config/herdr/config.toml" ]]; then
  for directory in "$HOME/.config" "$HOME/.config/herdr"; do
    if [[ -d "$directory" && ! -L "$directory" && -O "$directory" && ! -x "$directory" ]]; then
      chmod u+rwx -- "$directory"
    fi
  done
fi

mkdir -p "$(dirname "$config")"
if [[ ! -e "$config" && ! -L "$config" ]]; then
  install -m 0600 "$managed_config" "$config"
  exit 0
fi

# Symlinks and non-regular files are user-managed. Never replace their targets.
if [[ ! -f "$config" || -L "$config" ]]; then
  exit 0
fi

# An existing Agent layout is also user-managed, even when it omits $branch.
if grep -Eq '^\[ui\.sidebar\.agents(\]|\.)' "$config"; then
  exit 0
fi

temporary="$(mktemp "$(dirname "$config")/.config.toml.XXXXXX")"
cleanup() { rm -f -- "$temporary"; }
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
cp -- "$config" "$temporary"
printf '\n' >>"$temporary"
sed -n '/^\[ui\.sidebar\.agents\]$/,$p' "$managed_config" >>"$temporary"
chmod 0600 "$temporary"
mv -f -- "$temporary" "$config"
trap - EXIT HUP INT TERM
