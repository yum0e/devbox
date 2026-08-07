#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

mkdir -p "$TMP_DIR/bin" "$TMP_DIR/home"

cat >"$TMP_DIR/bin/docker" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
[[ "$*" == "buildx version" ]]
EOF

cat >"$TMP_DIR/bin/devcontainer" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$CALL_LOG"
EOF

chmod +x "$TMP_DIR/bin/docker" "$TMP_DIR/bin/devcontainer"

run_devc() {
  local case_name="$1"
  local multiplexer="$2"
  local command="$3"
  local repo="$TMP_DIR/$case_name/repo"
  local call_log="$TMP_DIR/$case_name/calls.log"

  mkdir -p "$repo"

  if [[ "$multiplexer" == "__unset__" ]]; then
    env -u DEVC_MULTIPLEXER \
      HOME="$TMP_DIR/home" \
      PATH="$TMP_DIR/bin:$PATH" \
      DEVC_TEMPLATE_DIR="$ROOT_DIR" \
      CALL_LOG="$call_log" \
      "$ROOT_DIR/install.sh" $command "$repo" >/dev/null 2>/dev/null
  else
    HOME="$TMP_DIR/home" \
      PATH="$TMP_DIR/bin:$PATH" \
      DEVC_TEMPLATE_DIR="$ROOT_DIR" \
      CALL_LOG="$call_log" \
      DEVC_MULTIPLEXER="$multiplexer" \
      "$ROOT_DIR/install.sh" $command "$repo" >/dev/null 2>/dev/null
  fi

  printf '%s\n' "$repo" "$call_log"
}

mapfile -t unset_result < <(run_devc unset-default __unset__ "")
unset_repo="${unset_result[0]}"
unset_log="${unset_result[1]}"
grep -Fqx -- "up --workspace-folder $unset_repo" "$unset_log"
grep -Fqx -- "exec --workspace-folder $unset_repo tmux new -As agent" "$unset_log"

mapfile -t empty_result < <(run_devc empty-default "" "")
empty_repo="${empty_result[0]}"
empty_log="${empty_result[1]}"
grep -Fqx -- "exec --workspace-folder $empty_repo tmux new -As agent" "$empty_log"

mapfile -t herdr_result < <(run_devc herdr-up herdr "")
herdr_repo="${herdr_result[0]}"
herdr_log="${herdr_result[1]}"
grep -Fqx -- "up --workspace-folder $herdr_repo" "$herdr_log"
grep -Fqx -- "exec --workspace-folder $herdr_repo herdr" "$herdr_log"
grep -Fq '"HERDR_VERSION": "0.8.0"' "$herdr_repo/.devcontainer/devcontainer.json"
grep -Fq '/usr/local/bin/herdr --version' "$herdr_repo/.devcontainer/Dockerfile"
! grep -Fq 'target=/home/node/.config/herdr' "$herdr_repo/.devcontainer/devcontainer.json"

mapfile -t rebuild_result < <(run_devc herdr-rebuild herdr rebuild)
rebuild_repo="${rebuild_result[0]}"
rebuild_log="${rebuild_result[1]}"
grep -Fqx -- "up --workspace-folder $rebuild_repo --remove-existing-container" "$rebuild_log"
grep -Fqx -- "exec --workspace-folder $rebuild_repo herdr" "$rebuild_log"

invalid_repo="$TMP_DIR/invalid/repo"
invalid_log="$TMP_DIR/invalid/calls.log"
invalid_error="$TMP_DIR/invalid/stderr.log"
mkdir -p "$invalid_repo"
if HOME="$TMP_DIR/home" \
  PATH="$TMP_DIR/bin:$PATH" \
  DEVC_TEMPLATE_DIR="$ROOT_DIR" \
  CALL_LOG="$invalid_log" \
  DEVC_MULTIPLEXER=screen \
  "$ROOT_DIR/install.sh" "$invalid_repo" >/dev/null 2>"$invalid_error"; then
  echo "invalid multiplexer unexpectedly succeeded" >&2
  exit 1
fi
grep -Fqx -- "error: unsupported multiplexer: screen (expected tmux or herdr)" "$invalid_error"
[[ ! -e "$invalid_log" ]]

echo "devc multiplexer tests passed"
