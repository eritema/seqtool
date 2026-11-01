set -euo pipefail
export SWARMFORGE_OFFLINE=1
export npm_config_offline=true
export PATH="$tool_dir/support:$PATH"
analysis_cp="src:spec:test:swarmforge/scripts:$tool_dir/src"
for dependency in "$tool_dir"/lib/*.jar; do
  analysis_cp="$analysis_cp:$dependency"
done
if [ -n "${SWARMFORGE_ANALYSIS_CLASSPATH:-}" ]; then
  analysis_cp="$analysis_cp:$SWARMFORGE_ANALYSIS_CLASSPATH"
fi
