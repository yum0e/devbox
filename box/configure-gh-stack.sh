#!/bin/sh
set -eu

readonly managed_binary="${DEVC2_GH_STACK_BINARY:-/usr/local/libexec/devc2/gh-stack}"
readonly extensions_root="$HOME/.local/share/gh/extensions"
readonly extension_dir="$HOME/.local/share/gh/extensions/gh-stack"
readonly extension_binary="$extension_dir/gh-stack"

if [ ! -x "$managed_binary" ]; then
  echo "devc2: managed gh-stack binary is unavailable: $managed_binary" >&2
  exit 1
fi

# Never follow indirections in the managed extension path.
for directory in "$HOME/.local" "$HOME/.local/share" "$HOME/.local/share/gh" "$extensions_root"; do
  if [ -L "$directory" ] || { [ -e "$directory" ] && [ ! -d "$directory" ]; }; then
    exit 0
  fi
  mkdir -p -- "$directory"
done

# Never follow or replace a user-managed extension directory indirection.
if [ -L "$extension_dir" ] || { [ -e "$extension_dir" ] && [ ! -d "$extension_dir" ]; }; then
  exit 0
fi
mkdir -p -- "$extension_dir"

# A real file at the extension executable path belongs to the user. Symlinks
# are the image-managed representation and are safe to create or repair.
if [ -e "$extension_binary" ] && [ ! -L "$extension_binary" ]; then
  exit 0
fi
if [ -L "$extension_binary" ] && [ "$(readlink "$extension_binary")" = "$managed_binary" ]; then
  exit 0
fi

temporary="$(mktemp -d "$extension_dir/.gh-stack.XXXXXX")"
cleanup() { rm -rf -- "$temporary"; }
trap cleanup EXIT HUP INT TERM
ln -s -- "$managed_binary" "$temporary/gh-stack"
mv -f -- "$temporary/gh-stack" "$extension_binary"
rmdir -- "$temporary"
trap - EXIT HUP INT TERM
