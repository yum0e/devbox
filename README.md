# devbox

`devc2` runs coding agents in a non-root Docker Desktop Island. Host credentials
managed by `devc2` stay outside it and are delegated through explicitly granted
capabilities called **Spans**. The checkout remains read/write.

## Start

Requirements: macOS, Docker Desktop with Compose v2, Python 3, and
`/usr/bin/openssl`.

```sh
./install.sh
devc2 auth
devc2 doctor --runtime
devc2 run /path/to/repo --span openai --span github --span ssh-agent
```

Without `--span`, `devc2` provides no host credentials:

```sh
devc2 /path/to/repo
```

Inside the Island, run `tact`, `pi`, `gh`, `gh stack`, or `herdr`. GitHub
currently labels stacked PRs a private preview; enabled repositories are
required. Herdr opens Tact in new panes by default, but is never automatic.

## Setup

```text
macOS host
+------------------------------------------------------------------+
| checkout ------------------------ read/write ------------------+  |
|                                                              |  |
| auth files --> OpenAI/GitHub World --> exact HTTPS routes     |  |
| SSH agent  --> SSH World -----------> one selected identity   |  |
|                         |                                    |  |
|                         | per-launch Link + mTLS              |  |
+-------------------------|------------------------------------|--+
                          v
Docker Desktop
+------------------------------------------------------------------+
| opaque Span tunnels + HTTP CONNECT router                       |
|                         |                                        |
|                         v                                        |
| Island                                                           |
|   /workspace       selected checkout, read/write                 |
|   /home/devbox     persistent, per checkout path                 |
|   stock tools      no raw credentials mounted/provided by devc2  |
|   no Docker socket, host home, raw SSH socket, or transport keys |
+------------------------------------------------------------------+
```

HTTP tools see normal-looking configuration. Their requests pass through a
host-controlled World, which adds the real credential only to declared routes.
The SSH World exposes identity queries and signing for one selected key; other
agent operations and keys are rejected.

Installing a World does not grant it. A World and its transport exist only for
the lifetime of an Island launched with the matching `--span`.

## What this protects

Common setups place authority inside the development environment:

```text
setup                         raw secret visible?   delegated authority
----------------------------  --------------------  --------------------------
environment variable         yes                   token scope
Compose secret/file mount    yes                   token scope
Git credential helper        returned to Git       token scope
raw SSH-agent forwarding     private key: no       every loaded identity
egress allowlist/firewall     separate concern      whatever credentials exist
devc2 HTTP Span              no                    declared service routes
devc2 SSH Span               private key: no       one selected identity
```

Docker notes that environment variables can leak through process inheritance
and logs; its [Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/)
improve delivery and service scoping but still mount the secret as a readable
file. VS Code [Dev Containers](https://code.visualstudio.com/remote/advancedcontainers/sharing-git-credentials)
reuse host Git helpers and forward the host SSH agent. Anthropic's reference
[devcontainer firewall](https://github.com/anthropics/claude-code/blob/main/.devcontainer/init-firewall.sh)
instead limits network destinations.

The closest current design is
[Docker Sandboxes credential injection](https://docs.docker.com/ai/sandboxes/configuration/credentials/),
which also keeps credentials behind a host proxy. Docker Sandboxes additionally
[deny outbound TCP by default](https://docs.docker.com/ai/sandboxes/security/defaults/).
`devc2` makes a different tradeoff: arbitrary development traffic is allowed,
while `devc2`-managed host authority is absent unless named at launch. This
avoids maintaining package-manager and service allowlists, but it does not
prevent source-code or command-output exfiltration.

## Honest boundaries

- The agent can read, modify, or delete the selected checkout and its Git
  metadata. Review and back up work as you would with any autonomous tool.
- Outbound networking is unrestricted. Spans isolate credentials, not data.
- Secrets already in the checkout or persistent Island home remain visible, and
  tools can acquire new credentials through interactive network login.
- A granted Span prevents extraction of the raw secret, not use of its
  authority. An agent can call the allowed API and export the response.
- The runtime is non-root, drops Linux capabilities, uses
  `no-new-privileges`, and has CPU, memory, and PID limits. This reduces impact;
  it is not a VM-grade defense against a Docker/kernel escape.
- First-party HTTP Spans currently cover OpenAI and GitHub. Git repository
  transport is SSH-only. Other registries, clouds, and APIs need a custom World.
- The host-side Worlds, bridge, mTLS, fake config, and recovery logic are more
  complex than mounting a token. `diagnostics` exists because that complexity
  can fail.
- This implementation supports macOS and Docker Desktop only.

## State and lifecycle

```text
canonical checkout path
          |
          v
devc2-<path-hash>_state-v2  -->  /home/devbox
```

The volume holds Tact sessions, tool settings, and the Island home. Normal exit,
host restart, reinstall, update, and rollback preserve it. Moving a checkout to
a new canonical path creates a new identity and leaves the old volume orphaned.

`devc2 reset /path/to/repo` permanently deletes that checkout's volume. It is
not a repair command.

## Commands

```text
devc2 auth                         host login and SSH-key selection
devc2 doctor                       host prerequisite checks
devc2 doctor --runtime             disposable end-to-end credential checks
devc2 run REPO [--span NAME ...]   launch an Island
devc2 repair REPO                  reconnect a running Island's Spans
devc2 skills refresh REPO          refresh host Agent Skills
devc2 update                       atomically import the next runtime
devc2 rollback                     activate the retained previous runtime
devc2 reset REPO                   destroy that checkout's devc2 state
```

First-party Spans are `openai`, `github`, `ssh-agent`, and `diagnostics`.
`diagnostics report` exposes bounded transport state, never credentials or
traffic contents.

## Installation and development

The installer writes only beneath `~/.local/bin`, `~/.local/share/devc2`, and
`~/.local/state/devc2`; it does not use `sudo`, edit shell startup files, or
modify target repositories. A checkout install updates from the checkout;
release installs update from GitHub's public release path. Activation
is atomic and one previous runtime is retained for rollback.

Upgrading a checkout installation from the former `v2/` layout requires one
root-level `./install.sh`; this changes installer metadata, not Island volumes.

```sh
./test.sh
DEVC2_TEST_PACKAGED_IMAGES=1 ./test.sh   # also build and smoke-test images
```

Custom command Worlds are registered by absolute executable path in
`~/.config/devc2/spans.json`. See the small first-party examples under
[`spans/`](spans/) and the bridge notes in
[`credential_proxy/README.md`](credential_proxy/README.md).
