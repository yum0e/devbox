# devbox v2

A hardened development Island for macOS and Docker Desktop. The Island contains
development tools, not host authority. Credentials, signing, and other host
capabilities arrive only through explicitly granted **Spans**.
It is installed as `devc2` and deliberately coexists with v1: it does not modify
v1 files, resources, volumes, or a target repository's `.devcontainer/`.

## Security model

The selected host checkout is mounted read/write and outbound network access is
unrestricted. Treat everything readable in the checkout as visible to the agent.
For a linked Git worktree, devc2 additionally projects the repository's shared
Git metadata. Shared refs, objects, config, and hooks remain writable; all
sibling worktree administrative records and `.git` pointer paths are read-only,
while the selected worktree's complete administrative directory remains
writable for its index, `HEAD`, logs, locks, and in-progress operations. The
parent checkout's files are never mounted. Exotic external object stores are
rejected rather than broadening the mount. devc2 never creates, lists, prunes,
or removes worktrees; manage those from the host. As with any writable checkout,
Island code can still corrupt its own selected Git metadata.
The runtime is non-root, drops Linux capabilities, sets `no-new-privileges`, and
has limits of 16 GiB memory, 8 CPUs, and 4096 PIDs. The Docker socket, host home,
raw OAuth credentials, raw SSH-agent socket, and Span transport keys are never
mounted into the Island.

The first-party OpenAI and GitHub Worlds retain bearer credentials on the host
and inject them only into narrowly allowed HTTPS requests. At Island launch,
both export bounded public manifests to one generic HTTP Link. It attaches fake
authentication files, environment, public CAs, and exact routes for the lifetime
of that Island. The SSH-agent World accepts
only identity queries and signing requests for one host-selected public key;
the generic opaque-stream Link projects it directly as `SSH_AUTH_SOCK`. Every
other agent operation and key is rejected before reaching the host agent.
These boundaries prevent credential extraction, not
credential use: any process in an Island granted a Span can exercise that Span's
authority and export the resulting data. Grant only the capabilities a checkout
needs.

## Interface

```sh
./v2/install.sh

devc2 auth               # one-time OpenAI + GitHub login and SSH key selection
devc2 doctor             # fast host prerequisites
devc2 doctor --runtime   # disposable live Docker/credential/signing checks
devc2 repair /path/to/repository # reconnect one running Island's Spans
devc2 skills refresh /path/to/repository # refresh host skills in a running Island
devc2 update             # atomically update from the install source
devc2 rollback           # atomically return to the retained previous runtime
devc2 run /path/to/repository
devc2 run /path/to/repository --span openai --span github --span ssh-agent
devc2 run /path/to/repository --span diagnostics
devc2 /path/to/repository       # retained shorthand, with no Spans
devc2 reset /path/to/repository
devc2 reset /path/to/repository --yes
```

`devc2 run /path/to/repository` grants no Spans and opens a credential-free
shell. Herdr is a regular executable in that shell. Run `herdr` when you want
to orchestrate agents and other processes in the same Island. In a fresh box,
Herdr opens Tact in its initial pane and in new tabs and splits by default;
change `terminal.default_shell` in `~/.config/herdr/config.toml` to override
that behavior. devc2 never starts Herdr automatically and no Herdr host service
or Span is involved.

The matching Herdr agent skill is included in the immutable skill snapshot, so
Tact, Pi, and other compatible harnesses discover its focus-free pane and agent
APIs without a wrapper. Host skill refreshes cannot shadow this bundled skill.

`reset` removes only that checkout's v2 Compose resources and state. It never
removes the checkout or v1 resources.

The first-party `openai`, `github`, `ssh-agent`, and `diagnostics` Spans ship with devc2 but are
still inert until named. With `openai` granted, stock `tact` and Pi see their
normal configuration and environment; with `github` granted, stock `gh` sees a
normal authenticated environment. Pi
sees a normal `openai-codex` provider backed by a non-secret Span capability;
the Span owns the connection, account identity, and host ChatGPT credentials.
Pi retains control of its model choice, settings, extensions, and sessions as
normal persistent Island state. No capability-specific wrapper or command is installed.
GitHub repository transport is deliberately SSH-only in this iteration and
composes with `ssh-agent`; the GitHub Span does not grant a second Git HTTPS
credential path. When `ssh-agent` is present, the Island configures Git and
Jujutsu signing with its single exposed identity.

### Runtime diagnostics

Grant `--span diagnostics` to inspect one running Island from inside itself.
`diagnostics report` returns sanitized per-Link lifecycle stages, connection
counters, recovery state, and the last exception class. Its World can read only
a bounded launch-local snapshot: it has no Docker access, host command execution, host
paths, traffic contents, credentials, process environment, mutation commands,
or visibility into other Islands. Other Worlds are not told where its snapshot
lives. This makes transport failures self-observable without adding debugging
tools or host authority to the Island.

Span transport recovery is automatic. A resumed or replaced bridge retires old
streams, the host relay retries a World connection before forwarding bytes, and
the OpenAI/GitHub Worlds replace an exited or unreachable credential helper
with a bounded cooldown. Recovery never replays tunneled TLS or application
requests; an already-started request fails normally and the agent's next request
uses the repaired path. `devc2 repair` remains the manual fallback for a broader
session repair.

`devc2 doctor --runtime` creates a disposable temporary checkout and validates
an end-to-end Pi response through the OpenAI Span, along with
the real Docker Desktop path through all three first-party Spans: OpenAI
transport, GitHub API authentication, the exact filtered SSH identity, signing,
writable Tact config, and the pinned Tact binary. It may trigger a 1Password
approval and makes authenticated probes, including one minimal model response.
It never mounts a real checkout and removes its project volumes during teardown.

`devc2 repair /path/to/repository` keeps the running Island and its Herdr panes
alive. It restarts only that checkout's Span bridge (including a stopped one),
waits for fresh projected sockets, verifies GitHub and the selected SSH identity
when granted, checks every host World's process status when diagnostics is
granted, and refreshes Git signing configuration. Without diagnostics, a custom
World's application protocol cannot be checked generically. Existing shells
cannot have their process environment rewritten; after repairing a mixed-version session they may need
`export SSH_AUTH_SOCK=/run/devc2/spans/ssh-agent.sock`. New launches use that
path directly.

The image installs the stock Tact, Pi, and Herdr executables. Attachment
environment is configured on the container itself, so ordinary shells and
processes launched later from Herdr receive the same transparent connection
without modifying shell startup files.

## Span contract

Installing a World is not a grant. `devc2` launches no World or Span
transport unless its name appears in `--span`.
An Island may grant at most 16 Spans; excess grants are rejected. The HTTP
projection, Span relay, and HTTP World handler layers each admit up to 256
concurrent connections, with a 1,024-connection listen backlog for bursts. A
saturated worker layer stops accepting until capacity returns, so queued clients
receive backpressure instead of an immediate connection reset.

First-party Spans are immutable release assets outside the Island
image and cannot be shadowed by configuration. Additional Spans are made
available in the host-owned catalog at
`~/.config/devc2/spans.json` (or `$XDG_CONFIG_HOME/devc2/spans.json`):

```json
{
  "spans": {
    "example": "/absolute/host/path/to/example-world"
  }
}
```

The catalog must be a regular file owned by the current host user and must not
be group- or world-writable. A World entry is only its absolute executable path;
there are no projection declarations, shell strings, schemas, or World-reported
fields. The World must live outside the mounted workspace and be
a root/current-user-owned executable that is not group/world-writable or
hard-linked. `devc2` snapshots its exact bytes without running it and projects
its own generic shim read-only as `/run/devc2/bin/<name>`. The World supplies no
client artifact. The host resolves the granted name to this World and supplies
the generic Link shim. A catalog value has no second shape: every Span is one
World path. First-party OpenAI and GitHub use a second host-owned generic Link
shape: a launch-lifetime HTTP attachment with bounded public bootstrap material
and declared CONNECT routes. These Worlds supply no Island executable. The Link
coalesces compatible environment, rejects route or environment conflicts, and
projects one standard CONNECT proxy. Undeclared HTTPS remains ordinary outbound
network traffic. Any process in a granted Island can use an attachment; the
capability boundary is the World grant, not an individual child process.

The World is executed directly with no devc2-defined arguments.
`DEVC2_SPAN_SOCKET_FD` names an inherited listening socket.
`DEVC2_ISLAND_ID` is unique to the launch and `DEVC2_WORKSPACE` is the canonical
host checkout path. It accepts one connection for each projected stream.
The launcher and its small bridge copy bytes without interpreting or framing
them. Tool methods, schemas, authorization, PTYs, process execution, prompts,
and every other semantic decision belong to the World.

World startup is structural, not a semantic health check: devc2 verifies that
the configured executable starts and does not exit immediately, but it does not
ask whether a World-specific upstream service is ready. Links
surface those failures normally and Worlds may retry or recover without a new
Island launch.

On Docker Desktop, the private Unix endpoint cannot cross directly from macOS
to Linux. The transport therefore uses a per-launch mutually authenticated TLS
hop. Its keys enter only the protocol-blind bridge sidecar, never the Island.
The bridge owns the socket directory; Island processes may connect to granted
endpoints but cannot replace them. The World process and transport stop when
the Island exits. The bridge compares VM boot time with wall time and retires
existing streams after a suspend-sized gap. Relays check that discontinuity
before forwarding the first post-resume bytes, so a resumed agent opens a fresh
connection instead of reusing a half-open tunnel; TCP keepalive provides an
additional failure detector.
Different Islands receive different World processes,
instance IDs, endpoints, and transport credentials.

Other command Worlds should install one immutable executable outside the
workspace and register its absolute path in `spans.json`. There is no automatic
PATH migration because availability is intentionally an explicit host
configuration decision.


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

Checkout builds display their full content identity, for example
`0.2.0+local.0123456789ab`, during installation, update, `--version`, and
rollback. Tagged releases retain their semantic version such as `0.2.0`.

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
and structurally validated before activation. A same-filesystem replacement of
`$DEVC2_DATA_DIR/installation.json` is the single atomic activation point. It
selects the current, previous, and management runtimes; matching symlinks remain
only as compatibility mirrors. `devc2 rollback` switches back without network access. Configuration,
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

## Host skills

Put portable Agent Skills in `~/.config/agent-skills/<name>/SKILL.md` (or the
equivalent `XDG_CONFIG_HOME` path). On each launch, devc2 validates that directory
and creates an immutable generation before publishing it into a stable read-only
projection. The standard `~/.agents/skills` path selects that view. Pi discovers that path
directly; devc2 enables Tact's native skill discovery in its per-launch
configuration snapshot. Both harnesses therefore see the same skills without
wrappers or access to the rest of the host configuration.

After editing the host directory, run
`devc2 skills refresh /path/to/repository` on the host to validate a replacement
snapshot and update the stable view only after validation completes. The command does not
restart the Island or any agent; reload or restart Tact, Pi, or Herdr when you
want it to discover the refreshed skills. Staged generations are removed with
the launch's temporary state when the Island exits.

```text
~/.config/agent-skills/
└── release-review/
    ├── SKILL.md
    ├── references/
    └── scripts/
```

The source may itself be a symlink into a dotfiles checkout, but its contents
must contain only user-owned, non-writable-by-others directories and regular
files. Nested symlinks, hard links, special files, oversized trees, invalid
skill names, and skill directories without `SKILL.md` are rejected. Never put
credentials in a skill; use a separately granted Span for authority.

## Requirements

The normal test suite builds and validates the real release archive. On a host
with Docker, also build and smoke-test every image from that packaged archive:

```bash
DEVC2_TEST_PACKAGED_IMAGES=1 ./v2/test.sh
```

The opt-in test uses unique temporary image tags and removes only those tags.

- macOS with Docker Desktop, Docker Compose v2, Python 3, and `/usr/bin/openssl`
- ChatGPT subscription and GitHub account for their respective optional Spans
- An SSH agent, such as 1Password, for the optional SSH-agent Span

Tact and Pi are bundled coding agents. Herdr v0.8.0 is bundled as their
in-Island orchestrator.

The devbox includes the v1 general developer toolset: build tools, Git and
GitHub CLI, Jujutsu, zsh, ripgrep, fd, zoxide, PostgreSQL 17 server and
client programs, uv-managed Python 3.14, Foundry, and the standard
network/process utilities.
The latest standalone pnpm installs and manages the latest Node runtime through
`pnpm runtime`; Corepack, fnm, and nvm are not used. Pi defaults its
`npmCommand` setting to pnpm so git and npm package dependencies can be
installed without npm; an existing user-selected command is preserved. Legacy
v1 coding agents are not installed.

Herdr is available on `PATH` but remains dormant until someone runs it. It then
owns only panes and processes in that same Island. tmux remains intentionally
absent.

Prime Agent, Claude, Git LFS, and cross-repository private submodules are
outside the initial scope.
