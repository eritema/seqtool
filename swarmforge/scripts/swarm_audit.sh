#!/usr/bin/env sh
set -eu
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec bb "$SCRIPT_DIR/swarm_audit.bb" "$@"
