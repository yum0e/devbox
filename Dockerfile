# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
ARG TACT_VERSION=0.6.3
FROM ghcr.io/clabby/tact:${TACT_VERSION}@sha256:ac02066f6c839c0741d894e02417609ac72f27b9729f1d3de736dfb4ed54fcf7 AS tact
FROM debian:bookworm-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818 AS island

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bubblewrap build-essential ca-certificates curl fd-find gh git jq less \
      libatomic1 libnss-wrapper lsof ncurses-term netcat-openbsd \
      openssh-client passwd procps python3 python3-venv ripgrep \
      unzip vim zoxide zsh \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl --fail --show-error --silent \
      https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
 && printf '%s\n' \
      'Types: deb' \
      'URIs: https://apt.postgresql.org/pub/repos/apt' \
      'Suites: bookworm-pgdg' \
      'Components: main' \
      'Signed-By: /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc' \
      >/etc/apt/sources.list.d/pgdg.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      libpq-dev postgresql-17 postgresql-client-17 \
 && ln -s /usr/bin/fdfind /usr/local/bin/fd \
 && rm -rf /var/lib/apt/lists/*

COPY --from=tact /tact /usr/local/bin/tact
COPY --from=ghcr.io/astral-sh/uv:0.9.26@sha256:9a23023be68b2ed09750ae636228e903a54a05ea56ed03a934d00fe9fbeded4b /uv /uvx /usr/local/bin/

# Bootstrap the latest standalone pnpm without Node, then let pnpm install and
# manage the latest Node runtime. Corepack, fnm, and nvm are not used.
ENV PNPM_HOME=/usr/local/share/pnpm
ENV FOUNDRY_DIR=/usr/local/share/foundry
ENV UV_PYTHON_INSTALL_DIR=/usr/local/share/uv/python
ENV UV_PYTHON_BIN_DIR=/usr/local/bin
ENV PATH=${FOUNDRY_DIR}/bin:${PNPM_HOME}/bin:/usr/lib/postgresql/17/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUN install -d -m 0755 "$PNPM_HOME" \
 && curl --fail --show-error --silent https://get.pnpm.io/install.sh \
      -o /tmp/install-pnpm.sh \
 && ENV=/tmp/pnpm-env SHELL=/bin/sh sh /tmp/install-pnpm.sh \
 && rm -f /tmp/install-pnpm.sh /tmp/pnpm-env \
 && pnpm --version \
 && pnpm runtime set node latest --global \
 && node --version \
 && pnpm store prune

# Pi keeps its settings and sessions in the Island. When the OpenAI World is
# granted, its standard provider environment is attached for the whole launch.
RUN pnpm add --global --ignore-scripts --no-optional \
      @earendil-works/pi-coding-agent@0.84.2 \
 && pnpm store prune

# Herdr is an ordinary Island tool. Nothing starts it until a user invokes it.
COPY devbox/agent-skills/herdr/SKILL.md /tmp/herdr-skill.expected
ARG TARGETARCH
ARG HERDR_VERSION=0.8.2
RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) \
        herdr_asset="herdr-linux-x86_64"; \
        herdr_sha256="976150a14d490c94b243ea2e1a7eb2dfb67f12e36b182db90936f6728e6aecf4" \
        ;; \
      arm64) \
        herdr_asset="herdr-linux-aarch64"; \
        herdr_sha256="f55610658e1c2e0d2aaef730b4b2ab885f7f8ba00285ab372bfb14f2e3d5b40d" \
        ;; \
      *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    herdr_tag="v${HERDR_VERSION#v}"; \
    curl -fsSL -o /tmp/herdr \
      "https://github.com/herdrdev/herdr/releases/download/${herdr_tag}/${herdr_asset}"; \
    echo "${herdr_sha256}  /tmp/herdr" | sha256sum -c -; \
    install -m 0755 /tmp/herdr /usr/local/bin/herdr; \
    rm -f /tmp/herdr; \
    herdr --version; \
    herdr --skill >/tmp/herdr-skill.actual; \
    cmp /tmp/herdr-skill.expected /tmp/herdr-skill.actual; \
    rm -f /tmp/herdr-skill.expected /tmp/herdr-skill.actual

# Install gh-stack as an immutable system binary. The GitHub CLI discovers it
# through the per-home extension symlink seeded below and repaired at startup.
ARG GH_STACK_VERSION=0.1.0
RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) gh_stack_sha256="358552dd7dce0a46ce153fe196270cec482b84f080947890aad4061a8d44bc0b" ;; \
      arm64) gh_stack_sha256="a79649e121845b7404109de21d65601c09c8c6d021d93738a3428d23986a8841" ;; \
      *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    gh_stack_tag="v${GH_STACK_VERSION#v}"; \
    curl -fsSL -o /tmp/gh-stack \
      "https://github.com/github/gh-stack/releases/download/${gh_stack_tag}/linux-${TARGETARCH}"; \
    echo "${gh_stack_sha256}  /tmp/gh-stack" | sha256sum -c -; \
    install -d -m 0755 /usr/local/libexec/devc2; \
    install -m 0755 /tmp/gh-stack /usr/local/libexec/devc2/gh-stack; \
    rm -f /tmp/gh-stack; \
    /usr/local/libexec/devc2/gh-stack --help >/dev/null

# Install the official Helm client with architecture-specific release checksums.
ARG HELM_VERSION=4.2.4
RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) helm_sha256="c306b46f719b0a4da32d0f78ee21bf90ce8d602f15b22ab753f0674d1670a7f3" ;; \
      arm64) helm_sha256="564de2191b881e9f71b5606b25345821ea1682f06ab90499d3ab22b530176da1" ;; \
      *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    helm_tag="v${HELM_VERSION#v}"; \
    helm_archive="/tmp/helm-${helm_tag}-linux-${TARGETARCH}.tar.gz"; \
    curl -fsSL -o "$helm_archive" \
      "https://get.helm.sh/helm-${helm_tag}-linux-${TARGETARCH}.tar.gz"; \
    echo "${helm_sha256}  ${helm_archive}" | sha256sum -c -; \
    tar -xzf "$helm_archive" -C /tmp "linux-${TARGETARCH}/helm"; \
    install -m 0755 "/tmp/linux-${TARGETARCH}/helm" /usr/local/bin/helm; \
    rm -rf "$helm_archive" "/tmp/linux-${TARGETARCH}"; \
    helm version --short

# Install Python and the Ethereum development toolchain without placing
# either installation in the persistent devbox home volume.
RUN uv python install 3.14 \
 && python3.14 --version \
 && curl --fail --location --show-error --silent https://getfoundry.sh/install \
      -o /tmp/foundry-install.sh \
 && head -n 1 /tmp/foundry-install.sh | grep -q '^#!' \
 && bash /tmp/foundry-install.sh \
 && rm -f /tmp/foundry-install.sh \
 && "$FOUNDRY_DIR/bin/foundryup" \
 && forge --version \
 && cast --version \
 && anvil --version \
 && chisel --version

RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) jj_target=x86_64; jj_sha=0b2d7ca5f77ec6b109fc5b6f9328c8d764825d3976f874c99236481f5461a8a3 ;; \
      arm64) jj_target=aarch64; jj_sha=4974ea98fcaaf9a546560b38c4cfbc6f04c6ac660c09d4f878e28d64131eebcf ;; \
      *) exit 1 ;; \
    esac; \
    url="https://github.com/jj-vcs/jj/releases/download/v0.29.0/jj-v0.29.0-${jj_target}-unknown-linux-musl.tar.gz"; \
    curl -fsSL "$url" -o /tmp/jj.tar.gz; \
    echo "$jj_sha  /tmp/jj.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/jj.tar.gz -C /tmp ./jj; \
    install -m 0755 /tmp/jj /usr/local/bin/jj; \
    rm /tmp/jj /tmp/jj.tar.gz; \
    jj --version

COPY devbox/entrypoint.sh /usr/local/bin/devc2-entrypoint
COPY devbox/configure-pi.sh /usr/local/libexec/devc2/configure-pi
COPY devbox/configure-signing.sh /usr/local/libexec/devc2/configure-signing
COPY devbox/configure-herdr.sh /usr/local/libexec/devc2/configure-herdr
COPY devbox/configure-gh-stack.sh /usr/local/libexec/devc2/configure-gh-stack
COPY devbox/herdr-git-metadata /usr/local/libexec/devc2/herdr-git-metadata
COPY devbox/herdr-zsh-integration.zsh /etc/zsh/zshrc.d/devc2-herdr-metadata.zsh
COPY devbox/github-known-hosts /etc/ssh/ssh_known_hosts
RUN chmod 0755 /usr/local/bin/devc2-entrypoint /usr/local/bin/tact \
      /usr/local/libexec/devc2/configure-pi \
      /usr/local/libexec/devc2/configure-signing \
      /usr/local/libexec/devc2/configure-herdr \
      /usr/local/libexec/devc2/configure-gh-stack \
      /usr/local/libexec/devc2/herdr-git-metadata \
      /usr/local/share/pnpm/bin/pi \
 && printf '\nsource /etc/zsh/zshrc.d/devc2-herdr-metadata.zsh\n' >>/etc/zsh/zshrc \
 && groupadd --gid 1000 devbox \
 && useradd --uid 1000 --gid devbox --create-home --shell /bin/zsh devbox \
 && mkdir -p /workspace /home/devbox/.cache /home/devbox/.local/share/tact \
      /home/devbox/.local/share/gh/extensions/gh-stack \
      /run/devc2-public /run/devc2/spans \
 && ln -s /usr/local/libexec/devc2/gh-stack \
      /home/devbox/.local/share/gh/extensions/gh-stack/gh-stack \
 && install -d -m 0700 -o devbox -g devbox \
      /home/devbox/.config /home/devbox/.config/herdr \
 && chown -R devbox:devbox /workspace /home/devbox \
 && chmod 0777 /home/devbox

# A fresh persistent home volume is initialized from the image. Seed Herdr so
# its initial pane, new tabs, and splits open zsh by default. The entrypoint
# upgrades the prior untouched default while preserving user-managed configs.
COPY --chmod=0644 devbox/herdr-config.toml /usr/local/share/devc2-herdr-config.toml
COPY --chown=devbox:devbox --chmod=0600 devbox/herdr-config.toml /home/devbox/.config/herdr/config.toml

ENV HOME=/home/devbox \
    TACT_HOME=/home/devbox/.local/share/tact \
    TACT_WORKSPACE=/workspace \
    FOUNDRY_DIR=/usr/local/share/foundry \
    UV_PYTHON_INSTALL_DIR=/usr/local/share/uv/python \
    UV_PYTHON_BIN_DIR=/usr/local/bin \
    PATH=/usr/local/share/foundry/bin:/usr/local/share/pnpm/bin:/usr/lib/postgresql/17/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/run/devc2/bin:/home/devbox/.local/bin

USER devbox
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/devc2-entrypoint"]

# Authentication is deliberately outside the Island image. Codex currently
# supplies the headless device flow; this target is built only by `devc2 auth`.
FROM island AS auth
USER root
RUN pnpm add --global @openai/codex@0.147.0 \
 && pnpm store prune
USER devbox

# Keep the lean Island as the default build result.
FROM island AS runtime
