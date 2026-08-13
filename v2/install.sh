#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./v2/install.sh

Installs devc2 to ~/.local/bin/devc2 and its v2 runtime assets to
~/.local/share/devc2. It does not modify devc v1 or any repository.
EOF
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  "") ;;
  *) echo "install.sh: unknown argument: $1" >&2; usage >&2; exit 2 ;;
esac

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
command -v python3 >/dev/null 2>&1 || { echo "install.sh: Python 3 is required on the host" >&2; exit 1; }
bin_dir="${DEVC2_BIN_DIR:-$HOME/.local/bin}"
share_dir="${DEVC2_SHARE_DIR:-$HOME/.local/share/devc2}"

for required in Dockerfile compose.yaml devbox/entrypoint.sh launcher/devc2.py credential_proxy/ssh_agent_proxy.py; do
  if [[ ! -f "$script_dir/$required" ]]; then
    echo "install.sh: missing v2 asset: $script_dir/$required" >&2
    exit 1
  fi
done

mkdir -p "$bin_dir" "$(dirname -- "$share_dir")"
staging="$(mktemp -d "${share_dir}.install.XXXXXX")"
cleanup() { rm -rf -- "$staging"; }
trap cleanup EXIT HUP INT TERM
chmod 0700 "$staging"

# Copy only v2 assets. In particular, never inspect or modify a target checkout's
# .devcontainer and never replace the v1 ~/.local/bin/devc installation.
cp -R "$script_dir/Dockerfile" "$script_dir/compose.yaml" "$script_dir/devbox" \
  "$script_dir/credential_proxy" "$script_dir/launcher" "$staging/"
if [[ -f "$script_dir/README.md" ]]; then cp "$script_dir/README.md" "$staging/"; fi
find "$staging" -type d -name __pycache__ -prune -exec rm -rf {} +
rm -rf -- "$staging/credential_proxy/tests"
find "$staging" -type d -exec chmod 0755 {} +
find "$staging" -type f -exec chmod 0644 {} +
chmod 0755 "$staging/launcher/devc2.py" "$staging/devbox/entrypoint.sh" \
  "$staging/credential_proxy/ssh_agent_proxy.py"

backup="${share_dir}.old.$$"
rm -rf -- "$backup"
if [[ -e "$share_dir" ]]; then mv -- "$share_dir" "$backup"; fi
if ! mv -- "$staging" "$share_dir"; then
  [[ ! -e "$backup" ]] || mv -- "$backup" "$share_dir"
  exit 1
fi
rm -rf -- "$backup"

launcher_tmp="$(mktemp "$bin_dir/.devc2.XXXXXX")"
printf '%s
' '#!/usr/bin/env bash' \
  "export DEVC2_SHARE_DIR=$(printf '%q' "$share_dir")" \
  'exec python3 "$DEVC2_SHARE_DIR/launcher/devc2.py" "$@"' >"$launcher_tmp"
chmod 0755 "$launcher_tmp"
mv -f -- "$launcher_tmp" "$bin_dir/devc2"

trap - EXIT HUP INT TERM
printf 'installed devc2: %s\n' "$bin_dir/devc2"
printf 'installed v2 assets: %s\n' "$share_dir"
case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *) printf 'note: add %s to PATH\n' "$bin_dir" ;;
esac
