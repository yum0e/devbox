#!/usr/bin/env bash
set -euo pipefail

readonly workspace="${TACT_WORKSPACE:-/workspace}"
readonly proxy_host="${TACT_PROXY_HOST:-127.0.0.1}"
readonly proxy_port="${TACT_PROXY_PORT:-8080}"
readonly ca_file="${TACT_PROXY_CA_FILE:-/run/tact-public/ca.crt}"
readonly wait_seconds="${TACT_PROXY_WAIT_SECONDS:-30}"

mkdir -p "$TACT_HOME"
if [[ ! -r "$TACT_CONFIG" ]]; then
  echo "tact-dev: shared Tact config is not readable: $TACT_CONFIG" >&2
  exit 1
fi

if ! user_name="$(id -un 2>/dev/null)"; then
  readonly nss_directory="$(mktemp -d /tmp/tact-nss.XXXXXX)"
  cp /etc/passwd "$nss_directory/passwd"
  cp /etc/group "$nss_directory/group"
  printf 'tact-host:x:%s:%s:Tact developer:%s:/bin/bash\n' \
    "$(id -u)" "$(id -g)" "$HOME" >>"$nss_directory/passwd"
  printf 'tact-host:x:%s:\n' "$(id -g)" >>"$nss_directory/group"
  export NSS_WRAPPER_PASSWD="$nss_directory/passwd"
  export NSS_WRAPPER_GROUP="$nss_directory/group"
  export LD_PRELOAD="/usr/local/lib/libnss_wrapper.so${LD_PRELOAD:+:$LD_PRELOAD}"
  user_name=tact-host
fi
export USER="$user_name"
export LOGNAME="$user_name"

if [[ ! -d "$workspace" ]]; then
  echo "tact-dev: workspace does not exist: $workspace" >&2
  exit 1
fi

if [[ ! -w "$workspace" ]]; then
  echo "tact-dev: workspace is not writable: $workspace" >&2
  exit 1
fi

workspace_probe="$(mktemp "$workspace/.tact-dev-write-test.XXXXXX")"
rm -f -- "$workspace_probe"

if [[ ! -r "$ca_file" ]]; then
  echo "tact-dev: proxy CA is not readable: $ca_file" >&2
  exit 1
fi

readonly ca_bundle="$(mktemp /tmp/tact-ca-bundle.XXXXXX)"
cat /etc/ssl/certs/ca-certificates.crt "$ca_file" >"$ca_bundle"
chmod 0444 "$ca_bundle"

if [[ ! "$wait_seconds" =~ ^[0-9]+$ ]] || (( wait_seconds == 0 )); then
  echo "tact-dev: TACT_PROXY_WAIT_SECONDS must be a positive integer" >&2
  exit 1
fi

proxy_ready=false
for ((attempt = 0; attempt < wait_seconds * 10; attempt++)); do
  if (exec 3<>"/dev/tcp/$proxy_host/$proxy_port") 2>/dev/null; then
    proxy_ready=true
    break
  fi
  sleep 0.1
done

if [[ "$proxy_ready" != true ]]; then
  echo "tact-dev: proxy did not become ready at $proxy_host:$proxy_port" >&2
  exit 1
fi

export SSL_CERT_FILE="$ca_bundle"
export REQUESTS_CA_BUNDLE="$ca_bundle"
export CURL_CA_BUNDLE="$ca_bundle"
export GIT_SSL_CAINFO="$ca_bundle"
export CARGO_HTTP_CAINFO="$ca_bundle"
export NODE_EXTRA_CA_CERTS="$ca_file"

# The filtered agent exposes only this public key. Configure Git and Jujutsu to
# use it for SSH commit signing without copying a private key into the devbox.
git config --global core.sshCommand "ssh -i /run/devc2-public/ssh-allowed.pub -o IdentitiesOnly=yes -o IdentityAgent=$SSH_AUTH_SOCK -o UserKnownHostsFile=/run/devc2-public/known_hosts -o StrictHostKeyChecking=yes"
git config --global gpg.format ssh
git config --global gpg.ssh.allowedSignersFile /run/devc2-public/allowed_signers
git config --global user.signingkey /run/devc2-public/ssh-allowed.pub
git config --global commit.gpgsign true
for _attempt in $(seq 1 100); do
  if [[ -S "$SSH_AUTH_SOCK" ]] && ssh-add -L >/dev/null 2>&1; then break; fi
  sleep 0.1
done
if [[ ! -S "$SSH_AUTH_SOCK" ]] || ! ssh-add -L >/dev/null 2>&1; then
  echo "tact-dev: filtered SSH agent did not become ready" >&2
  exit 1
fi
signature_probe="$(mktemp /tmp/devc2-signing-probe.XXXXXX)"
printf 'devc2 signing readiness probe' >"$signature_probe"
if ! ssh-keygen -Y sign -f /run/devc2-public/ssh-allowed.pub -n git "$signature_probe" >/dev/null 2>&1; then
  rm -f -- "$signature_probe" "$signature_probe.sig"
  echo "tact-dev: selected 1Password key could not sign; unlock 1Password and restart devc2; if no approval prompt appears, test the selected key directly on the host" >&2
  exit 1
fi
rm -f -- "$signature_probe" "$signature_probe.sig"
if ! github_user="$(gh api user 2>/dev/null)"; then
  echo "tact-dev: GitHub credential proxy authentication failed; run devc2 auth" >&2
  exit 1
fi
github_login="$(printf '%s' "$github_user" | jq -r '.login // empty')"
github_id="$(printf '%s' "$github_user" | jq -r '.id // empty')"
if [[ -z "$github_login" || -z "$github_id" ]]; then
  echo "tact-dev: GitHub API returned an incomplete user identity" >&2
  exit 1
fi
if ! git config --global user.name >/dev/null; then
  git config --global user.name "$github_login"
fi
if ! git config --global user.email >/dev/null; then
  git config --global user.email "${github_id}+${github_login}@users.noreply.github.com"
fi
if git_name="$(git config --global user.name 2>/dev/null)" && git_email="$(git config --global user.email 2>/dev/null)" && [[ -n "$git_name" && -n "$git_email" ]]; then
  jj config set --user user.name "$git_name"
  jj config set --user user.email "$git_email"
  jj config set --user signing.behavior own
  jj config set --user signing.backend ssh
  jj config set --user signing.key /run/devc2-public/ssh-allowed.pub
fi

# Some agent runtimes construct a reduced child environment. Persist the public
# filtered-socket path in user shell startup files as a compatibility fallback.
readonly ssh_agent_export='export SSH_AUTH_SOCK=/run/devc2-ssh/agent.sock'
for shell_startup in "$HOME/.profile" "$HOME/.bashrc" "$HOME/.zshenv"; do
  touch "$shell_startup"
  if ! grep -Fqx "$ssh_agent_export" "$shell_startup"; then
    printf '%s
' "$ssh_agent_export" >>"$shell_startup"
  fi
done

if [[ "${TACT_DEV_SHELL:-}" == "1" ]]; then
  exec /bin/bash
fi

exec tact "$@"
