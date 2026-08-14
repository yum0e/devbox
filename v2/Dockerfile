# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
ARG TACT_VERSION=0.5.3
FROM ghcr.io/clabby/tact:${TACT_VERSION}@sha256:ef44abc4d69b27f3c13526717ac0258e2bb88497611449d5b02e935630d074c5 AS tact
FROM debian:bookworm-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818
ARG HERDR_VERSION=0.8.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential ca-certificates curl gh git jq less libnss-wrapper \
      netcat-openbsd nodejs npm openssh-client postgresql-client procps \
      python3 python3-venv ripgrep vim zsh \
 && rm -rf /var/lib/apt/lists/*

COPY --from=tact /tact /usr/local/libexec/devc2/tact-upstream
COPY devbox/tact-wrapper.sh /usr/local/bin/tact
COPY --from=ghcr.io/astral-sh/uv:0.9.26@sha256:9a23023be68b2ed09750ae636228e903a54a05ea56ed03a934d00fe9fbeded4b /uv /uvx /usr/local/bin/
RUN npm install --global @openai/codex@0.147.0 \
 && npm cache clean --force

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

RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) herdr_asset=herdr-linux-x86_64; herdr_sha256=b872ea7e40fa2cb17e857ac9b62b1bf26db7b403c622f5d2f3f5b35f6e9acd28 ;; \
      arm64) herdr_asset=herdr-linux-aarch64; herdr_sha256=f647ac66468d9efbc642fe534fb284468f0aea60641606fc008dfc0d82a3ca87 ;; \
      *) exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/herdrdev/herdr/releases/download/v${HERDR_VERSION#v}/${herdr_asset}" -o /tmp/herdr; \
    echo "$herdr_sha256  /tmp/herdr" | sha256sum -c -; \
    install -m 0755 /tmp/herdr /usr/local/bin/herdr; \
    rm /tmp/herdr; \
    herdr --version

COPY devbox/entrypoint.sh /usr/local/bin/devc2-entrypoint
RUN chmod 0755 /usr/local/bin/devc2-entrypoint /usr/local/bin/tact /usr/local/libexec/devc2/tact-upstream \
 && groupadd --gid 1000 devbox \
 && useradd --uid 1000 --gid devbox --create-home --shell /bin/zsh devbox \
 && mkdir -p /workspace /home/devbox/.cache /home/devbox/.local/share/tact \
      /run/devc2-public /run/devc2-ssh \
 && chown -R devbox:devbox /workspace /home/devbox \
 && chmod 0777 /home/devbox

ENV HOME=/home/devbox \
    TACT_HOME=/home/devbox/.local/share/tact \
    TACT_WORKSPACE=/workspace \
    TACT_AUTH=chatgpt \
    TACT_AUTH_FILE=/run/devc2-public/auth.json \
    CODEX_HOME=/run/devc2-public/codex \
    GH_TOKEN=devc2-github-placeholder-token \
    SSH_AUTH_SOCK=/run/devc2-ssh/agent.sock \
    TACT_PROXY_CA_FILE=/run/devc2-public/ca.crt \
    PATH=/usr/local/bin:/home/devbox/.local/bin:/usr/bin:/bin

USER devbox
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/devc2-entrypoint"]
