# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
ARG TACT_VERSION=0.6.1
FROM ghcr.io/clabby/tact:${TACT_VERSION}@sha256:f832481864ecf41574333dc848f1ad65ac8ab98d4b5a832cbeb254ecd2cb5428 AS tact
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

# Match v1's managed Python and Ethereum development toolchain without placing
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

ARG TARGETARCH
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
COPY devbox/github-known-hosts /etc/ssh/ssh_known_hosts
RUN chmod 0755 /usr/local/bin/devc2-entrypoint /usr/local/bin/tact \
      /usr/local/libexec/devc2/configure-pi \
      /usr/local/share/pnpm/bin/pi \
 && groupadd --gid 1000 devbox \
 && useradd --uid 1000 --gid devbox --create-home --shell /bin/zsh devbox \
 && mkdir -p /workspace /home/devbox/.cache /home/devbox/.local/share/tact \
      /run/devc2-public /run/devc2/spans \
 && chown -R devbox:devbox /workspace /home/devbox \
 && chmod 0777 /home/devbox

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
