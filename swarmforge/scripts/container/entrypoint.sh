#!/bin/sh
set -eu
umask 077
mkdir -p "$CODEX_HOME" "$XDG_CACHE_HOME" /workspace/tmp
if [ "${1:-}" = "--probe" ]; then
  shift
  exec bb /opt/swarmforge/isolation_probe.bb "$@"
fi
if [ "${SWARMFORGE_OPENAI_ENABLED:-1}" = "1" ]; then
  if [ ! -r /run/secrets/openai_api_key ]; then
    echo "Missing dedicated OpenAI key file; no agents started." >&2
    exit 1
  fi
  SWARMFORGE_OPENAI_API_KEY="$(cat /run/secrets/openai_api_key)"
  export SWARMFORGE_OPENAI_API_KEY
fi
bb /opt/swarmforge/codex_auth.bb
export GIT_CONFIG_GLOBAL="$HOME/.gitconfig"
git config --global user.name "SwarmForge"
git config --global user.email "swarmforge@localhost"
bb /opt/swarmforge/git_trust.bb
if [ -f /opt/swarmforge/runtime/runtime.edn ]; then
  /opt/swarmforge/runtime/swarm
else
  ./swarm
fi
# Keep the container alive only while its dashboard remains alive. A teardown
# or dashboard crash must not leave an apparently healthy worker container.
dashboard_pid="$(cat .swarmforge/pack_web.pid)"
while kill -0 "$dashboard_pid" 2>/dev/null; do sleep 2; done
echo "Dashboard stopped; worker container exiting." >&2
exit 1
