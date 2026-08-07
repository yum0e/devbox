# autonomous coding sandbox

a devcontainer for running Claude Code, Codex, Pi, and Prime Agent in yolo mode.

includes `uv`, Foundry (`forge`, `cast`, `anvil`, `chisel`), `fnm` (Fast Node Manager), and `hunk` preinstalled.

based on anthropic's claude code devcontainer and [banteg's setup](https://github.com/banteg/agents).

## requirements

- docker (or [orbstack](https://orbstack.dev/))
- Docker BuildKit/Buildx, required because the Dockerfile uses BuildKit-only syntax like `RUN --mount=type=cache,...`
- devcontainer cli (`npm install -g @devcontainers/cli`)

verify Buildx is available with:

```sh
docker buildx version
```

`devc` sets `DOCKER_BUILDKIT=1` by default for devcontainer builds and exits with a clear message if `docker` or `docker buildx` is unavailable.

## quickstart

install `./install.sh self-install`

run `devc <repo>` or `devc .` inside project folder.

you're now in tmux with Claude Code, Codex, Pi, and Prime Agent ready to go, with permissions preconfigured.

to use with vscode, run `devc install <repo>` and choose "reopen in container" in the editor.
the built in terminal would login inside the container.

## notes

- **overwrites `.devcontainer/`** on every run
- auth, history, Prime Agent sessions and runtime, and jj user config persist across rebuilds via Docker volumes
- `~/.agents` uses the shared `agents-repository` Docker volume, so a repository installed in one devbox is available in every devbox on the same Docker host
- Prime Agent is installed from its stable release channel by default; set `PRIME_AGENT_CHANNEL` to `beta` in `devcontainer.json` to use the beta channel
- PostgreSQL tools are preinstalled (start the service manually when needed)
- Node version switching is handled with `fnm` (`fnm install --lts`, `fnm use --lts`, `fnm default <version>`)
- installed `fnm` Node versions persist across rebuilds via the `fnm-data` volume
- host SSH agent forwarding is supported inside the container

