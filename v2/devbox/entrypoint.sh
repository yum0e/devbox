#!/usr/bin/env bash
set -euo pipefail

readonly workspace="${TACT_WORKSPACE:-/workspace}"
mkdir -p "$TACT_HOME"

readonly shared_tact_config=/run/devc2-public/tact-config.toml
if [[ ! -r "$shared_tact_config" ]]; then
  echo "devc2: shared Tact config is unavailable" >&2
  exit 1
fi
rm -f -- "$TACT_CONFIG"
install -m 0600 "$shared_tact_config" "$TACT_CONFIG"

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
    echo "devc2: Span projection did not become ready" >&2
    exit 1
  fi
fi

# Clear grant-managed state from the persistent home before applying this
# launch's capabilities. A previous grant must never survive by configuration.
git config --global --unset-all core.sshCommand >/dev/null 2>&1 || true
git config --global --unset-all gpg.format >/dev/null 2>&1 || true
git config --global --unset-all gpg.ssh.allowedSignersFile >/dev/null 2>&1 || true
git config --global --unset-all user.signingkey >/dev/null 2>&1 || true
git config --global --unset-all commit.gpgsign >/dev/null 2>&1 || true
jj config unset --user signing.behavior >/dev/null 2>&1 || true
jj config unset --user signing.backend >/dev/null 2>&1 || true
jj config unset --user signing.key >/dev/null 2>&1 || true
rm -f -- "$HOME/.config/devc2/signing-key.pub" "$HOME/.config/devc2/allowed-signers"

# The SSH-agent Span is already a selected-key-only agent. No second relay,
# credential file, or adapter is needed inside the Island.
if [[ -S /run/devc2/spans/ssh-agent.sock ]]; then
  export SSH_AUTH_SOCK=/run/devc2/spans/ssh-agent.sock
  signing_dir="$HOME/.config/devc2"
  signing_key="$signing_dir/signing-key.pub"
  allowed_signers="$signing_dir/allowed-signers"
  mkdir -p -m 700 "$signing_dir"
  ssh-add -L | sed -n '1p' >"$signing_key"
  chmod 0600 "$signing_key"
  if [[ ! -s "$signing_key" ]]; then
    echo "devc2: SSH-agent Span exposed no selected identity" >&2
    exit 1
  fi
  read -r key_type key_blob _comment <"$signing_key"
  printf '* namespaces="git" %s %s\n' "$key_type" "$key_blob" >"$allowed_signers"
  chmod 0600 "$allowed_signers"
  signing_probe="$(mktemp /tmp/devc2-signing.XXXXXX)"
  printf 'devc2 signing readiness' >"$signing_probe"
  if ! ssh-keygen -Y sign -f "$signing_key" -n git "$signing_probe" >/dev/null 2>&1; then
    rm -f -- "$signing_probe" "$signing_probe.sig"
    echo "devc2: SSH-agent Span selected identity could not sign" >&2
    exit 1
  fi
  rm -f -- "$signing_probe" "$signing_probe.sig"
  git config --global core.sshCommand "ssh -i $signing_key -o IdentitiesOnly=yes -o IdentityAgent=$SSH_AUTH_SOCK"
  git config --global gpg.format ssh
  git config --global gpg.ssh.allowedSignersFile "$allowed_signers"
  git config --global user.signingkey "$signing_key"
  git config --global commit.gpgsign true
  jj config set --user signing.behavior own
  jj config set --user signing.backend ssh
  jj config set --user signing.key "$signing_key"
else
  unset SSH_AUTH_SOCK
fi

# GitHub identity is useful but not required to open an Island. It is learned
# only through the explicitly granted GitHub Span and never from a raw token.
if command -v github >/dev/null 2>&1; then
  if github_identity="$(github run -- gh api user 2>/dev/null)"; then
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
  echo "✓ PostgreSQL client: $(psql --version)"
  echo "✓ Node runtime: $(node --version)"
  echo "✓ pnpm: $(pnpm --version)"
  echo "✓ managed Python: $(python3.14 --version)"
  echo "✓ Foundry forge: $(forge --version | head -n 1)"
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
    git -C "$doctor_repo" -c user.name=devc2 -c user.email=devc2@invalid commit -q -S -m doctor
    git -C "$doctor_repo" verify-commit HEAD >/dev/null
    rm -rf -- "$doctor_repo"
    echo "✓ SSH-agent Span: signed commit verified"
  fi
  if command -v github >/dev/null 2>&1; then
    github run -- gh api user --jq .login >/dev/null
    echo "✓ GitHub Span: authenticated"
  fi
  if command -v openai >/dev/null 2>&1; then
    openai run -- /bin/sh -ceu '
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
      jq -e "type == \"object\"" "$response" >/dev/null
      rm -f -- "$response"
    '
    echo "✓ OpenAI Span: authenticated models request"
  fi
  echo "✓ runtime doctor passed"
  exit 0
fi

if (( $# )); then exec "$@"; fi
exec /bin/zsh
