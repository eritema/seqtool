FROM node:22-bookworm-slim
ARG CODEX_VERSION=0.162.1
ARG BABASHKA_VERSION=1.13.225
ARG SWARM_UID=1000
ARG SWARM_GID=1000
ARG TARGETARCH
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git tmux zsh procps tini python3 \
 && rm -rf /var/lib/apt/lists/* \
 && case "$TARGETARCH" in arm64) bb_arch=aarch64 ;; amd64) bb_arch=amd64 ;; *) exit 1 ;; esac \
 && curl -fsSL "https://github.com/babashka/babashka/releases/download/v${BABASHKA_VERSION}/babashka-${BABASHKA_VERSION}-linux-${bb_arch}-static.tar.gz" -o /tmp/bb.tgz \
 && tar -xzf /tmp/bb.tgz -C /usr/local/bin bb \
 && rm /tmp/bb.tgz \
 && npm install -g "@openai/codex@${CODEX_VERSION}" \
 && npm cache clean --force \
 && (getent group "$SWARM_GID" >/dev/null || groupadd -g "$SWARM_GID" swarm) \
 && usermod -u "$SWARM_UID" -g "$SWARM_GID" node \
 && touch /etc/swarmforge-container
COPY entrypoint.sh /opt/swarmforge/entrypoint.sh
COPY isolation_probe.bb /opt/swarmforge/isolation_probe.bb
COPY git_trust.bb /opt/swarmforge/git_trust.bb
COPY codex_auth.bb /opt/swarmforge/codex_auth.bb
COPY lmstudio_gateway.cjs /opt/swarmforge/lmstudio_gateway.cjs
RUN chmod 755 /opt/swarmforge/entrypoint.sh
ENV HOME=/home/node CODEX_HOME=/home/node/.codex \
    SWARMFORGE_ISOLATION=container SWARMFORGE_TERMINAL=none SWARMFORGE_OPEN_BROWSER=0 \
    XDG_CACHE_HOME=/workspace/.swarmforge/cache \
    HTTP_PROXY=http://proxy:3128 HTTPS_PROXY=http://proxy:3128 \
    http_proxy=http://proxy:3128 https_proxy=http://proxy:3128 \
    NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost
WORKDIR /workspace
USER node
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/swarmforge/entrypoint.sh"]
