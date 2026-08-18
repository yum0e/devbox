#!/usr/bin/env bash
set -euo pipefail

if (( $# )); then
  echo "usage: configure-signing" >&2
  exit 2
fi

readonly ssh_socket=/run/devc2/spans/ssh-agent.sock
readonly signing_dir="$HOME/.config/devc2"
readonly signing_key="$signing_dir/signing-key.pub"
readonly allowed_signers="$signing_dir/allowed-signers"
readonly signing_program="$signing_dir/ssh-keygen-with-agent"

if [[ ! -S "$ssh_socket" ]]; then
  echo "devc2: SSH-agent Span socket is unavailable" >&2
  exit 1
fi
export SSH_AUTH_SOCK="$ssh_socket"
mkdir -p -m 700 "$signing_dir"
exec 9<"$signing_dir"
flock -x 9

staging="$(mktemp -d "$signing_dir/.signing.XXXXXX")"
trap 'rm -rf -- "$staging"' EXIT HUP INT TERM
mapfile -t identities < <(ssh-add -L)
if [[ "${#identities[@]}" -ne 1 || "${identities[0]}" != ssh-* ]]; then
  echo "devc2: SSH-agent Span did not expose exactly one identity" >&2
  exit 1
fi
printf '%s\n' "${identities[0]}" >"$staging/signing-key.pub"
read -r key_type key_blob _comment <"$staging/signing-key.pub"
printf '* namespaces="git" %s %s\n' "$key_type" "$key_blob" >"$staging/allowed-signers"
printf '%s\n' \
  '#!/bin/sh' \
  'set -eu' \
  'SSH_AUTH_SOCK=/run/devc2/spans/ssh-agent.sock' \
  'export SSH_AUTH_SOCK' \
  'exec /usr/bin/ssh-keygen "$@"' >"$staging/ssh-keygen-with-agent"
chmod 0600 "$staging/signing-key.pub" "$staging/allowed-signers"
chmod 0500 "$staging/ssh-keygen-with-agent"

probe="$(mktemp /tmp/devc2-signing.XXXXXX)"
trap 'rm -rf -- "$staging"; rm -f -- "$probe" "$probe.sig"' EXIT HUP INT TERM
printf 'devc2 signing readiness' >"$probe"
ssh-keygen -Y sign -f "$staging/signing-key.pub" -n git "$probe" >/dev/null 2>&1
rm -f -- "$probe" "$probe.sig"

mv -f -- "$staging/signing-key.pub" "$signing_key"
mv -f -- "$staging/allowed-signers" "$allowed_signers"
mv -f -- "$staging/ssh-keygen-with-agent" "$signing_program"
rmdir "$staging"
trap - EXIT HUP INT TERM

git config --global core.sshCommand "ssh -i $signing_key -o IdentitiesOnly=yes -o IdentityAgent=$ssh_socket"
git config --global gpg.format ssh
git config --global gpg.ssh.allowedSignersFile "$allowed_signers"
git config --global gpg.ssh.program "$signing_program"
git config --global user.signingkey "$signing_key"
git config --global commit.gpgsign true
jj config set --user signing.behavior own
jj config set --user signing.backend ssh
jj config set --user signing.key "$signing_key"
jj config set --user signing.backends.ssh.program "$signing_program"
jj config set --user signing.backends.ssh.allowed-signers "$allowed_signers"
