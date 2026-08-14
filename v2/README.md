# devbox v2 (experimental)

A hardened, Herdr-first Tact coding environment for macOS and Docker Desktop.
It is installed as `devc2` and deliberately coexists with v1: it does not modify
v1 files, resources, volumes, or a target repository's `.devcontainer/`.

## Security model

The selected host checkout is mounted read/write and outbound network access is
unrestricted. Treat everything readable in the checkout as visible to the agent.
The runtime is non-root, drops Linux capabilities, sets `no-new-privileges`, and
has limits of 16 GiB memory, 8 CPUs, and 4096 PIDs; credential sidecars have separate small limits. The Docker socket is never mounted.

ChatGPT/Codex and GitHub OAuth bearer credentials are held only by the sibling
credential proxy. The devbox sees placeholders; matching requests receive real
headers only at their exact upstream. A trusted host-side filter connects to the
ambient 1Password agent and relays requests over an authenticated, per-launch channel
to a second container-side filter; the raw 1Password socket never enters Docker and
the relay secret is not mounted in the devbox. This prevents trivial raw
credential extraction, but a compromised agent can exercise the full authority of
those credentials and can export data fetched with them.

## Interface

```sh
./v2/install.sh

devc2 auth        # one-time ChatGPT + GitHub browser/device login
devc2 doctor             # fast host prerequisites
devc2 doctor --runtime   # disposable live Docker/credential/signing checks
devc2 /path/to/repository
devc2 reset /path/to/repository
devc2 reset /path/to/repository --yes
```

`devc2 /path/to/repository` opens Herdr after credential and signing readiness
checks pass. Start `tact` yourself in a Herdr pane. Exiting or detaching Herdr
ends the ephemeral devbox session and tears down its containers.

`reset` removes only that checkout's v2 Compose resources and state. It never
removes the checkout or v1 resources.

### Runtime diagnostics

`devc2 doctor --runtime` creates a disposable temporary checkout and validates
the real Docker Desktop path: both credential-proxy routes, the exact filtered
SSH identity, GitHub SSH, signed-commit verification, writable Tact config, and
the pinned Tact and Herdr binaries. It may trigger a 1Password approval and
makes authenticated read-only requests to GitHub and the ChatGPT model catalog.
It never mounts a real checkout and removes its project volumes during teardown.

The image installs a trusted `tact` wrapper ahead of the persistent home PATH.
The wrapper restores the selected-key proxy socket before executing the pinned
upstream Tact binary, without modifying shell startup files.

## Tact preferences and state

Shared Tact preferences live at `~/.config/devc2/tact/config.toml` (or the
equivalent `XDG_CONFIG_HOME` path). The file is created on first launch with a
conservative subagent limit. Each launch projects a read-only snapshot and
refreshes a writable runtime copy inside that repository's state volume. Tact
may replace its runtime copy, but those changes never modify the shared host
file and are overwritten by the next launch. Edit shared preferences on the
host; do not put credentials, tokens, repository paths, or workspace-specific
values in them because every devbox can read them.

Tact sessions, checkpoints, transcripts, and `memory/v1.sqlite3` remain
per-checkout even when `[memory] enabled = true` is set in the shared file.

## Requirements

- macOS with Docker Desktop, Docker Compose v2, and Python 3
- ChatGPT subscription and GitHub account (configured by `devc2 auth`)
- 1Password SSH agent enabled, with `SSH_AUTH_SOCK` available to the launcher

Herdr is pinned to `v0.8.0`; Tact remains the only bundled coding agent.

This is an experimental v2. Pi, Prime Agent, Claude, Git LFS, and cross-repository
private submodules are outside the initial scope.
