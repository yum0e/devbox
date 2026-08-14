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
ambient 1Password agent and relays requests over an ephemeral, mutually authenticated
TLS channel to a second container-side filter. The raw 1Password socket never enters
Docker. The per-launch client certificate and key are mounted only into the SSH
sidecar, never the devbox. This prevents trivial raw
credential extraction, but a compromised agent can exercise the full authority of
those credentials and can export data fetched with them.

## Interface

```sh
./v2/install.sh

devc2 auth        # one-time ChatGPT + GitHub browser/device login
devc2 doctor             # fast host prerequisites
devc2 doctor --runtime   # disposable live Docker/credential/signing checks
devc2 update             # verify and atomically activate the latest stable release
devc2 rollback           # atomically return to the retained previous runtime
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


## Safe installation and updates

A checkout install remains available:

```sh
./v2/install.sh
```

After the first `devc2-v*` GitHub Release is published, installation does not
require a clone. Download the installer as a file, inspect it, and then run it;
do not pipe remote code directly into a shell:

```sh
curl --proto '=https' --tlsv1.2 -fLO \
  https://github.com/yum0e/devbox/releases/latest/download/install-devc2.sh
less install-devc2.sh
bash install-devc2.sh
rm install-devc2.sh
```

Installation and updates use only `~/.local/bin`, `~/.local/share/devc2`, and
`~/.local/state/devc2` by default. They never invoke `sudo` or edit shell startup
files. A Docker Hub account or `docker login` is never required: devc2 only reads
public pinned base images anonymously, builds its runtime image locally, and uses
`--pull=never` whenever that local image is run. If `~/.local/bin` is absent from
`PATH`, the installer prints a note and leaves the decision to the user.

Program assets are installed into immutable, versioned directories. A stable
dispatcher takes a shared lock before selecting one runtime, and that process
keeps the resolved directory for its full lifetime. Install, update, and rollback
need the exclusive form of the same permanent lock and immediately refuse while
any devc2 command or session is active. There is no force override. Repository
locks are also permanent, so `reset` cannot tear down a running checkout.

The new release is fully downloaded, bounded, hash-verified, safely extracted,
and structurally validated before activation. A same-filesystem `current` symlink
replacement is the single atomic activation point. The old runtime remains at
`previous`; `devc2 rollback` switches back without network access. Configuration,
OAuth data, Tact preferences, checkout contents, and Docker state volumes live
outside versioned program assets and are never migrated or rewritten by these
operations.

`devc2 update` trusts GitHub's TLS-verified Releases API, the repository's GitHub
control plane, and the SHA-256 asset digest recorded by GitHub. This detects
corruption and substitution outside that trust boundary; it does not protect
against compromise of the GitHub repository, release workflow, or GitHub itself.
Release tags and immutable Releases must therefore be protected in repository
settings. The `devc2-release` environment must require approval and define its
`DEVC2_RELEASE_APPROVED=true` environment variable. The workflow also requires a
protected tag whose commit is on `main`; it is tag/version-gated, uses a
commit-pinned checkout action, builds deterministic archives, tests the archive,
and refuses to replace an existing release.

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

- macOS with Docker Desktop, Docker Compose v2, Python 3, and `/usr/bin/openssl`
- ChatGPT subscription and GitHub account (configured by `devc2 auth`)
- 1Password SSH agent enabled, with `SSH_AUTH_SOCK` available to the launcher

Herdr is pinned to `v0.8.0`; Tact remains the only bundled coding agent.

The devbox includes the v1 general developer toolset: build tools, Git and
GitHub CLI, Jujutsu, zsh, ripgrep, fd, zoxide, PostgreSQL 17 server and
client programs, uv-managed Python 3.14, Foundry, and the standard
network/process utilities.
The latest standalone pnpm installs and manages the latest Node runtime through
`pnpm runtime`; Corepack, fnm, and nvm are not used. Legacy v1 coding agents are
not installed because v2 remains Tact-only.

Herdr is the only bundled terminal multiplexer; tmux is intentionally absent.

This is an experimental v2. Pi, Prime Agent, Claude, Git LFS, and cross-repository
private submodules are outside the initial scope.
