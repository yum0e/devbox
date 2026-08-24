#!/usr/bin/env bash
set -euo pipefail

readonly workspace="${TACT_WORKSPACE:-/workspace}"
mkdir -p "$TACT_HOME"
/usr/local/libexec/devc2/configure-pi
/usr/local/libexec/devc2/configure-herdr
/usr/local/libexec/devc2/configure-gh-stack

report_span_diagnostics() {
  if command -v diagnostics >/dev/null 2>&1; then
    echo "devc2: launch-scoped diagnostics:" >&2
    diagnostics report >&2 || true
  fi
}

readonly shared_tact_config=/run/devc2-public/tact-config.toml
if [[ ! -r "$shared_tact_config" ]]; then
  echo "devc2: shared Tact config is unavailable" >&2
  exit 1
fi
rm -f -- "$TACT_CONFIG"
install -m 0600 "$shared_tact_config" "$TACT_CONFIG"
skills_config="$(mktemp /tmp/devc2-tact-skills.XXXXXX)"
if ! tact config show | awk '
  $0 == "[skills]" { in_skills=1; print; next }
  in_skills && /^enabled = / { print "enabled = true"; configured=1; next }
  in_skills && /^\[/ { in_skills=0 }
  { print }
  END { if (!configured) exit 1 }
' >"$skills_config"; then
  rm -f -- "$skills_config"
  echo "devc2: could not enable Tact skill discovery" >&2
  exit 1
fi
install -m 0600 "$skills_config" "$TACT_CONFIG"
rm -f -- "$skills_config"

if ! user_name="$(id -un 2>/dev/null)"; then
  readonly nss_directory="$(mktemp -d /tmp/devc2-nss.XXXXXX)"
  cp /etc/passwd "$nss_directory/passwd"
  cp /etc/group "$nss_directory/group"
  printf 'devc2-host:x:%s:%s:devc2 user:%s:/bin/zsh\n' \
    "$(id -u)" "$(id -g)" "$HOME" >>"$nss_directory/passwd"
  printf 'devc2-host:x:%s:\n' "$(id -g)" >>"$nss_directory/group"
  export NSS_WRAPPER_PASSWD="$nss_directory/passwd"
  export NSS_WRAPPER_GROUP="$nss_directory/group"
  export LD_PRELOAD="/usr/local/lib/libnss_wrapper.so${LD_PRELOAD:+:$LD_PRELOAD}"
  user_name=devc2-host
fi
export USER="$user_name" LOGNAME="$user_name"

if [[ ! -d "$workspace" || ! -w "$workspace" ]]; then
  echo "devc2: workspace must exist and be writable: $workspace" >&2
  exit 1
fi
workspace_probe="$(mktemp "$workspace/.devc2-write-test.XXXXXX")"
rm -f -- "$workspace_probe"

if [[ -e /run/devc2-public/spans.json ]]; then
  for _attempt in $(seq 1 100); do
    cmp -s /run/devc2-public/span-ready /run/devc2/spans/.ready && break
    sleep 0.1
  done
  if ! cmp -s /run/devc2-public/span-ready /run/devc2/spans/.ready; then
    echo "devc2: Span gateway did not become ready" >&2
    exit 1
  fi
fi

# HTTP Worlds attach only public/fake application state. Copy it into a fresh
# launch-local writable tree so stock tools can use their normal configuration
# paths, while the real credentials and routing policy remain in host Worlds.
readonly attachment_root=/tmp/devc2-http-attachments
readonly attachment_ca=/tmp/devc2-http-ca.pem
rm -rf -- "$attachment_root"
rm -f -- "$attachment_ca"
if [[ -d /run/devc2-public/http-attachments ]]; then
  cp -R /run/devc2-public/http-attachments "$attachment_root"
  find "$attachment_root" -type d -exec chmod 0700 {} +
  find "$attachment_root" -type f -exec chmod 0600 {} +
fi
if [[ -s /run/devc2-public/http-ca.pem ]]; then
  (
    umask 077
    cat /etc/ssl/certs/ca-certificates.crt /run/devc2-public/http-ca.pem >"$attachment_ca"
  )
fi

# Clear grant-managed state from the persistent home before applying this
# launch's capabilities. A previous grant must never survive by configuration.
git config --global --unset-all core.sshCommand >/dev/null 2>&1 || true
git config --global --unset-all gpg.format >/dev/null 2>&1 || true
git config --global --unset-all gpg.ssh.allowedSignersFile >/dev/null 2>&1 || true
git config --global --unset-all gpg.ssh.program >/dev/null 2>&1 || true
git config --global --unset-all user.signingkey >/dev/null 2>&1 || true
git config --global --unset-all commit.gpgsign >/dev/null 2>&1 || true
jj config unset --user signing.behavior >/dev/null 2>&1 || true
jj config unset --user signing.backend >/dev/null 2>&1 || true
jj config unset --user signing.key >/dev/null 2>&1 || true
jj config unset --user signing.backends.ssh.program >/dev/null 2>&1 || true
jj config unset --user signing.backends.ssh.allowed-signers >/dev/null 2>&1 || true
rm -f -- \
  "$HOME/.config/devc2/signing-key.pub" \
  "$HOME/.config/devc2/allowed-signers" \
  "$HOME/.config/devc2/ssh-keygen-with-agent"

# The SSH World is already a selected-key-only agent. The Span gateway
# exposes it directly. Git and Jujutsu need one narrow ssh-keygen launcher
# because agent runtimes may intentionally remove SSH_AUTH_SOCK from children.
if [[ -S /run/devc2/spans/ssh-agent.sock ]]; then
  export SSH_AUTH_SOCK=/run/devc2/spans/ssh-agent.sock
  if ! /usr/local/libexec/devc2/configure-signing; then
    report_span_diagnostics
    exit 1
  fi
else
  unset SSH_AUTH_SOCK
fi

# GitHub identity is useful but not required to open a Box. It is learned
# only through the explicitly granted GitHub Span and never from a raw token.
if [[ -n "${GH_TOKEN:-}" ]]; then
  if github_identity="$(gh api user 2>/dev/null)"; then
    github_login="$(printf '%s' "$github_identity" | jq -r '.login // empty')"
    github_id="$(printf '%s' "$github_identity" | jq -r '.id // empty')"
    if [[ -n "$github_login" && -n "$github_id" ]]; then
      git config --global user.name "$(git config --global user.name 2>/dev/null || printf '%s' "$github_login")"
      git config --global user.email "$(git config --global user.email 2>/dev/null || printf '%s+%s@users.noreply.github.com' "$github_id" "$github_login")"
      jj config set --user user.name "$(git config --global user.name)"
      jj config set --user user.email "$(git config --global user.email)"
    fi
  fi
fi

if [[ "${DEVC2_RUNTIME_DOCTOR:-}" == "1" ]]; then
  tact config show >/dev/null
  echo "✓ Tact configuration: valid"
  echo "✓ Tact binary: $(tact --version 2>&1 | head -n 1)"
  echo "✓ Pi binary: $(pi --version 2>&1 | head -n 1)"
  echo "✓ PostgreSQL client: $(psql --version)"
  echo "✓ Node runtime: $(node --version)"
  echo "✓ pnpm: $(pnpm --version)"
  echo "✓ managed Python: $(python3.14 --version)"
  echo "✓ Foundry forge: $(forge --version | head -n 1)"
  echo "✓ Helm: $(helm version --short)"
  if [[ -n "${SSH_AUTH_SOCK:-}" ]]; then
    mapfile -t doctor_identities < <(ssh-add -L)
    test "${#doctor_identities[@]}" -eq 1
    echo "✓ SSH-agent Span: exactly one identity"
    git ls-remote git@github.com:yum0e/devbox.git HEAD >/dev/null
    echo "✓ SSH-agent Span: GitHub Git transport authenticated"
    doctor_repo="$(mktemp -d /tmp/devc2-doctor-git.XXXXXX)"
    git -C "$doctor_repo" init -q
    printf 'devc2 runtime doctor\n' >"$doctor_repo/README.md"
    git -C "$doctor_repo" add README.md
    env -u SSH_AUTH_SOCK git -C "$doctor_repo" -c user.name=devc2 -c user.email=devc2@invalid commit -q -S -m doctor
    git -C "$doctor_repo" verify-commit HEAD >/dev/null
    rm -rf -- "$doctor_repo"
    doctor_jj="$(mktemp -d /tmp/devc2-doctor-jj.XXXXXX)"
    jj git init --colocate "$doctor_jj" >/dev/null
    printf 'devc2 runtime doctor\n' >"$doctor_jj/README.md"
    env -u SSH_AUTH_SOCK jj -R "$doctor_jj" sign -r @ >/dev/null
    rm -rf -- "$doctor_jj"
    echo "✓ SSH-agent Span: Git and Jujutsu signing verified without inherited agent state"
  fi
  if [[ -n "${GH_TOKEN:-}" ]]; then
    gh api user --jq .login >/dev/null
    echo "✓ GitHub Span: authenticated"
  fi
  if [[ -n "${TACT_AUTH_FILE:-}" ]]; then
    access="$(jq -er .tokens.access_token "$TACT_AUTH_FILE")"
    account="$(jq -er .tokens.account_id "$TACT_AUTH_FILE")"
    response="$(mktemp /tmp/devc2-openai-doctor.XXXXXX)"
    status="$(curl -sS --max-time 30 -w "%{http_code}" \
      -H "Authorization: Bearer $access" \
      -H "chatgpt-account-id: $account" \
      -H "originator: codex_cli_rs" \
      -H "User-Agent: codex_cli_rs/0.147.0" \
      "https://chatgpt.com/backend-api/codex/models?client_version=0.147.0" \
      -o "$response")"
    test "$status" = 200
    jq -e 'type == "object"' "$response" >/dev/null
    rm -f -- "$response"
    echo "✓ OpenAI Span: authenticated models request"
    pi_probe="$(timeout 90 pi --print --no-session --no-tools \
      --no-extensions --no-skills --no-prompt-templates --no-context-files \
      --thinking minimal 'Reply with exactly: devc2-pi-ok')"
    test "$(printf '%s' "$pi_probe" | tr -d '\r\n')" = devc2-pi-ok
    echo "✓ Pi OpenAI Span: completed response"
  fi
  echo "✓ runtime doctor passed"
  exit 0
fi

if (( $# )); then exec "$@"; fi
exec /bin/zsh
