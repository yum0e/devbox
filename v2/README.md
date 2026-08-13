# devbox v2 (experimental)

A hardened, Tact-only autonomous coding environment for macOS and Docker Desktop.
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
devc2 doctor
devc2 /path/to/repository
devc2 reset /path/to/repository
devc2 reset /path/to/repository --yes
```

`reset` removes only that checkout's v2 Compose resources and state. It never
removes the checkout or v1 resources.

## Tact preferences and state

Shared Tact preferences live at `~/.config/devc2/tact/config.toml` (or the
equivalent `XDG_CONFIG_HOME` path). The file is created on first launch with a
conservative subagent limit and mounted read-only into every devbox. Edit it on
the host; do not put credentials, tokens, repository paths, or other
workspace-specific values in it because every devbox can read it.

The mount target remains inside each checkout's `state-v2` volume. Tact
sessions, checkpoints, transcripts, and `memory/v1.sqlite3` therefore remain
per-checkout even when `[memory] enabled = true` is set in the shared file.

## Requirements

- macOS with Docker Desktop, Docker Compose v2, and Python 3
- ChatGPT subscription and GitHub account (configured by `devc2 auth`)
- 1Password SSH agent enabled, with `SSH_AUTH_SOCK` available to the launcher

This is an experimental v2. Pi, Prime Agent, Claude, Git LFS, and cross-repository
private submodules are outside the initial scope.
