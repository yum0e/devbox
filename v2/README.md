# devbox v2 (experimental)

A hardened development Island for macOS and Docker Desktop. The base Island is
deliberately unaware of host tools such as Herdr. Host capabilities can be
projected explicitly as experimental **Spans**.
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
devc2 update             # atomically update from the install source
devc2 rollback           # atomically return to the retained previous runtime
devc2 run /path/to/repository
devc2 run /path/to/repository --span herdr
devc2 /path/to/repository       # retained shorthand, with no Spans
devc2 reset /path/to/repository
devc2 reset /path/to/repository --yes
```

`devc2 run /path/to/repository` grants no Spans and opens a shell after
credential and signing readiness checks pass. `--span herdr` is an explicit
grant: it discovers `herdr-span` on the host `PATH`, projects only the client it
describes, and starts a provider scoped to that one Island. Granting a Span does
not automatically invoke it. Inside the Island, the projected command and its
private endpoint are `/run/devc2/bin/herdr` and
`/run/devc2/spans/herdr.sock`.

`reset` removes only that checkout's v2 Compose resources and state. It never
removes the checkout or v1 resources.

### Runtime diagnostics

`devc2 doctor --runtime` creates a disposable temporary checkout and validates
the real Docker Desktop path: both credential-proxy routes, the exact filtered
SSH identity, GitHub SSH, signed-commit verification, writable Tact config, and
the pinned Tact binary, plus the absence of ungranted Herdr. It may trigger a
1Password approval and
makes authenticated read-only requests to GitHub and the ChatGPT model catalog.
It never mounts a real checkout and removes its project volumes during teardown.

The image installs a trusted `tact` wrapper ahead of the persistent home PATH.
The wrapper restores the selected-key proxy socket before executing the pinned
upstream Tact binary, without modifying shell startup files.

## Experimental Span contract

Installing a provider is not a grant. `devc2` launches no provider or Span
transport unless its name appears in `--span`.
An Island may grant at most 16 Spans; the transport rejects excess concurrent
connections instead of creating unbounded threads.

A provider is a host executable named `<name>-span` with two operations:

```sh
<name>-span describe
<name>-span serve
```

`describe` writes exactly one JSON object to stdout:

```json
{"name":"herdr","version":"0.1.0","client":"/absolute/path/to/linux/herdr"}
```

The client must be one regular executable file of at most 64 MiB. `devc2`
snapshots it for that launch and mounts the snapshot read-only as
`/run/devc2/bin/<name>`; it never becomes a release asset or part of the base
image.

For `serve`, `DEVC2_SPAN_SOCKET_FD` names an inherited listening socket.
`DEVC2_ISLAND_ID` is unique to the launch and `DEVC2_WORKSPACE` is the canonical
host checkout path. The provider accepts one connection for each client stream.
The launcher and its small bridge copy bytes without interpreting or framing
them. Tool methods, schemas, authorization, PTYs, process execution, prompts,
and every other semantic decision belong to the provider.

On Docker Desktop, the private Unix endpoint cannot cross directly from macOS
to Linux. The transport therefore uses a per-launch mutually authenticated TLS
hop. Its keys enter only the protocol-blind bridge sidecar, never the Island.
The bridge owns the socket directory; Island processes may connect to granted
endpoints but cannot replace them. The provider process and transport stop when
the Island exits. Different Islands receive different provider processes,
instance IDs, endpoints, and transport credentials.


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

For a checkout installation made with `./v2/install.sh`, the initial install
records that checkout's `v2/` directory. Subsequent `devc2 update` commands
re-import its current assets directly, so rerunning `install.sh` is unnecessary.
The command does not fetch or modify the checkout; update it with your normal Git
workflow first. For a release installation, `devc2 update` retains the verified
latest-stable-release download path.

Program assets are installed into immutable, versioned directories. A stable
dispatcher takes a shared lock before selecting one runtime, and that process
keeps the resolved directory for its full lifetime. A forward `devc2 update` may
run while sessions are active: those sessions continue using their resolved
runtime and containers, while the new runtime becomes active only for subsequent
launches. Updates are serialized with a separate permanent lock. Install and
rollback retain the exclusive installation lock and refuse while any session is
active. Runtime pruning is not supported. There is no force override. Repository
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

Tact remains the only bundled coding agent. Herdr is intentionally not bundled.

The devbox includes the v1 general developer toolset: build tools, Git and
GitHub CLI, Jujutsu, zsh, ripgrep, fd, zoxide, PostgreSQL 17 server and
client programs, uv-managed Python 3.14, Foundry, and the standard
network/process utilities.
The latest standalone pnpm installs and manages the latest Node runtime through
`pnpm runtime`; Corepack, fnm, and nvm are not used. Legacy v1 coding agents are
not installed because v2 remains Tact-only.

No terminal multiplexer is bundled; tmux remains intentionally absent. Herdr
must arrive through an explicitly granted Span.

This is an experimental v2. Pi, Prime Agent, Claude, Git LFS, and cross-repository
private submodules are outside the initial scope.
