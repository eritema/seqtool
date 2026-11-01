#!/usr/bin/env python3
"""Explicit, connected preparation step; the generated kit needs only Babashka."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile

SOURCES = {
    "crap4clj": ("https://github.com/unclebob/crap4clj.git", "e068673a852a8142323ac680fa3366de65bc2227", "src"),
    "clj-mutate": ("https://github.com/unclebob/clj-mutate.git", "cea397da5b10b27372e36b6764728c952058e802", "src"),
    "cloverage": ("https://github.com/cloverage/cloverage.git", "61e3cac426e9907a9dd01c37597f85c71a57ff90", "cloverage/src"),
}
JARS = {
    "riddley-0.2.2.jar": ("riddley/riddley/0.2.2", "c38dbfa82e0decfd406185db0f881c383705f1fc1ed6b011a61c78c3d016b679"),
    "slingshot-0.12.2.jar": ("slingshot/slingshot/0.12.2", "a68ac22bf2ea3cd54ce34db70fd69844d631ef549f6450c278c30e6f79df63f3"),
    "speclj-3.13.0.jar": ("speclj/speclj/3.13.0", "d66528eba8e8dc5925befa32f8999be45fde116bd8960c1bcc9676c2bb95427d"),
    "versioneer-0.2.0.jar": ("trptcolin/versioneer/0.2.0", "d0bdc80617439d92afd76e6fbd9284ac6edbf74d78c38f9b18f183c57f378e09"),
    "tools.namespace-1.5.0.jar": ("org/clojure/tools.namespace/1.5.0", "1eebf07aeb241aee9a0c197bf723205b0bade132d96069f6600bda4ae2e1d094"),
}
NOTICES = {
    'riddley-README.md': ('https://raw.githubusercontent.com/clj-commons/riddley/cc126c205ad456c8e4463533f53d83956faef3bc/README.md',
                          '98a9dddf8fb54649277b6237c33ad15f7c20bd5acb72f6578ff5d38ca7f57b99'),
}

RUNTIME = '''set -euo pipefail
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
'''

COVERAGE = '''if [ "${1:-}" = --speclj ]; then
  exec bb --config "$tool_dir/empty.edn" --classpath "$analysis_cp" -m offline.coverage "$@"
fi
exec bb --config "$tool_dir/empty.edn" --classpath "$analysis_cp" -m offline.coverage --lcov "$@"
'''

COVERAGE_ADAPTER = '''(ns offline.coverage
  (:require [clojure.java.io :as io]
            [cloverage.instrument :as instrument]
            [crap4clj.bb-coverage :as bb-coverage]
            [speclj.cloverage :as speclj-coverage]))

(defn source-filename [filename]
  (if-let [url (io/resource filename)]
    (if (= "file" (.getProtocol url))
      (.getCanonicalPath (io/file (.toURI url)))
      filename)
    filename))

(defn -main [& args]
  ;; Cloverage normally binds *file* to a resource-relative filename. Scripts
  ;; that load sibling helpers need the physical file path during evaluation.
  (alter-var-root #'instrument/eval-form
    (fn [original]
      (fn [filename form line-hint instrumented-form]
        (original (source-filename filename) form line-hint instrumented-form))))
  (bb-coverage/enable-bb-sources!)
  (if (= "--speclj" (first args))
    (apply speclj-coverage/-main (rest args))
    (apply bb-coverage/-main args)))
'''

SPEC = '''exec bb --config "$tool_dir/empty.edn" --classpath "$analysis_cp" -m speclj.main "$@"
'''

CRAP = '''reuse=; configured=
for argument in "$@"; do
  case "$argument" in
    --use-existing-coverage) reuse=1 ;;
    --coverage-command) configured=1 ;;
  esac
done
if [ -z "$reuse$configured" ] && [ "${1:-}" != --help ]; then
  echo 'LOCAL_COVERAGE_REQUIRED: pass --coverage-command with a local runner, or --use-existing-coverage.' >&2
  exit 2
fi
exec bb --config "$tool_dir/empty.edn" --classpath "$analysis_cp" -m crap4clj.core "$@"
'''

MUTATE = '''scan=; configured=
for argument in "$@"; do
  case "$argument" in
    --scan|--update-manifest|--help) scan=1 ;;
    --test-command) configured=1 ;;
  esac
done
if [ -z "$scan$configured" ]; then
  if [ ! -d spec ]; then
    echo 'LOCAL_TEST_COMMAND_REQUIRED: pass --test-command, --coverage-command and --test-roots for this project.' >&2
    exit 2
  fi
  set -- "$@" --test-command 'speclj -c spec --tag ~no-mutate' \
    --coverage-command 'cloverage --speclj -c spec --tag ~no-mutate -- -p src -s spec --lcov' --test-roots spec
fi
exec bb --config "$tool_dir/empty.edn" --classpath "$analysis_cp" -m clj-mutate.core "$@"
'''


def executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/usr/bin/env bash\n' + body)
    path.chmod(0o755)


def build(output, cache):
    if output.exists():
        raise SystemExit(f"Destination exists: {output}")
    cache.mkdir(parents=True, exist_ok=True)
    repos = {}
    for name, (url, commit, source_path) in SOURCES.items():
        repo = cache / name
        if not repo.exists():
            subprocess.run(['git', 'clone', url, str(repo)], check=True)
        actual = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
        if actual != commit:
            subprocess.run(['git', '-C', str(repo), 'checkout', '--detach', commit], check=True)
        # Reusing a cache must not silently package local edits.
        if subprocess.check_output(['git', '-C', str(repo), 'status', '--porcelain'], text=True).strip():
            raise SystemExit(f"Source checkout has local changes: {repo}")
        repos[name] = repo
    jars = cache / 'jars'
    jars.mkdir(exist_ok=True)
    for name, (coordinate, expected) in JARS.items():
        path = jars / name
        if not path.exists():
            host = 'https://repo.maven.apache.org/maven2' if coordinate.startswith('org/clojure/') else 'https://repo.clojars.org'
            with urllib.request.urlopen(f'{host}/{coordinate}/{name}', timeout=60) as response:
                path.write_bytes(response.read())
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise SystemExit(f"Dependency checksum mismatch: {path}")
    for name, (url, expected) in NOTICES.items():
        path = cache / name
        if not path.exists():
            with urllib.request.urlopen(url, timeout=60) as response:
                path.write_bytes(response.read())
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise SystemExit(f"Notice checksum mismatch: {path}")

    # Build in a sibling staging directory and publish only a complete kit.
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='clojure-kit-', dir=output.parent) as temp:
        stage = Path(temp) / 'kit'
        stage.mkdir()
        for tool, body in {'crap4clj': CRAP, 'cloverage': COVERAGE,
                           'clj-mutate': MUTATE, 'speclj': SPEC}.items():
            dest = stage / 'tools' / tool
            dest.mkdir(parents=True)
            for name, (_, _, src) in SOURCES.items():
                shutil.copytree(repos[name] / src, dest / 'src', dirs_exist_ok=True)
                notices = dest / 'upstream' / name
                notices.mkdir(parents=True)
                for document in ('LICENSE', 'COPYING', 'NOTICE', 'README.md', 'epl-v10.html'):
                    if (repos[name] / document).is_file():
                        shutil.copy2(repos[name] / document, notices / document)
            (dest / 'lib').mkdir()
            for jar in JARS:
                shutil.copy2(jars / jar, dest / 'lib' / jar)
                with zipfile.ZipFile(jars / jar) as archive:
                    for name in archive.namelist():
                        if name.endswith('pom.xml') or Path(name).name.upper() in ('LICENSE', 'NOTICE', 'COPYING'):
                            notice = dest / 'upstream/dependencies' / jar / name
                            notice.parent.mkdir(parents=True, exist_ok=True)
                            notice.write_bytes(archive.read(name))
            for notice in NOTICES:
                shutil.copy2(cache / notice, dest / 'upstream' / notice)
            (dest / 'provenance.json').write_text(json.dumps({
                'sources': {name: {'url': url, 'commit': commit} for name, (url, commit, _) in SOURCES.items()},
                'dependencies': {name: {'coordinate': coord, 'sha256': digest} for name, (coord, digest) in JARS.items()},
                'runtime': 'Babashka 1.13.225 or newer; built-in Clojure, rewrite-clj, tools.reader, tools.cli, tools.logging, java.classpath, data.xml and data.json',
            }, indent=2) + '\n')
            (dest / 'empty.edn').write_text('{:paths []}\n')
            (dest / 'src/offline').mkdir(exist_ok=True)
            (dest / 'src/offline/coverage.clj').write_text(COVERAGE_ADAPTER)
            (dest / 'runtime.sh').write_text(RUNTIME)
            executable(dest / 'run', 'tool_dir="$(cd -- "$(dirname -- "$0")" && pwd)"\nsource "$tool_dir/runtime.sh"\n' + body)
            for support, support_body in {'cloverage': COVERAGE, 'speclj': SPEC}.items():
                executable(dest / 'support' / support,
                           'tool_dir="$(cd -- "$(dirname -- "$0")/.." && pwd)"\nsource "$tool_dir/runtime.sh"\n' + support_body)
        (stage / 'swarmforge-tools-v1').write_text('1\n')
        inventory = {'sources': {name: {'url': url, 'commit': commit} for name, (url, commit, _) in SOURCES.items()},
                     'dependencies': {name: {'coordinate': coord, 'sha256': digest} for name, (coord, digest) in JARS.items()},
                     'runtime': 'Babashka 1.13.225 or newer; Bash; no JVM required'}
        inventory['files'] = {str(p.relative_to(stage)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in sorted(stage.rglob('*')) if p.is_file()}
        (stage / 'inventory.json').write_text(json.dumps(inventory, indent=2) + '\n')
        stage.rename(output)
    print(f'Prepared offline kit: {output}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--cache', type=Path, required=True)
    args = parser.parse_args()
    build(args.output.resolve(), args.cache.resolve())
