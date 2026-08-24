#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./install.sh

Installs devc2 to ~/.local/bin/devc2 and its runtime assets to
~/.local/share/devc2. It does not modify target repositories.
EOF
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  "") ;;
  *) echo "install.sh: unknown argument: $1" >&2; usage >&2; exit 2 ;;
esac

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
command -v python3 >/dev/null 2>&1 || { echo "install.sh: Python 3 is required on the host" >&2; exit 1; }
[[ -x /usr/bin/openssl ]] || { echo "install.sh: /usr/bin/openssl is required on the host" >&2; exit 1; }
bin_dir="${DEVC2_BIN_DIR:-$HOME/.local/bin}"
share_dir="${DEVC2_SHARE_DIR:-$HOME/.local/share/devc2}"
arguments=(
  _install-assets
  --source "$script_dir"
  --bin-dir "$bin_dir"
  --share-dir "$share_dir"
)
if [[ "${DEVC2_INSTALL_RELEASE:-0}" == 1 ]]; then arguments+=(--release); fi

# The Python installer holds the permanent exclusive control lock, validates and
# stages immutable assets, then atomically switches the installed runtime pointer.
# It never reads or writes shell startup files, configuration, auth, or repo state.
exec python3 "$script_dir/launcher/devc2.py" "${arguments[@]}"
