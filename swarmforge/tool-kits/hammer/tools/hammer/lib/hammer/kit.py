"""Kit di strumenti per SwarmForge ('swarm_tool.sh import').

Il kit contiene hammer e le sue dipendenze in Python puro (Jinja2, MarkupSafe,
PyYAML senza le estensioni C), con le loro licenze. Nel container serve solo
python3 ≥ 3.11: il launcher lo avvia isolato dall'ambiente (-E -S), quindi usa
sempre il codice del kit.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import shutil
from pathlib import Path

from . import __version__
from .errors import HammerError

# modulo importabile → distribuzione che ne porta la licenza
VENDORED = {"hammer": "hammer-cq", "jinja2": "Jinja2", "markupsafe": "MarkupSafe", "yaml": "PyYAML"}
PACKAGE_FILES = ("*.py", "*.j2", "*.toml")

RUN = """#!/usr/bin/env bash
set -euo pipefail
tool_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
exec python3 -B -E -S "$tool_dir/main.py" "$@"
"""

MAIN = """import sys
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit(f"hammer richiede Python 3.11 o successivo, trovato {sys.version.split()[0]}")
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from hammer.cli import main

sys.exit(main())
"""


def _copy_package(module: str, destination: Path) -> None:
    source = Path(importlib.import_module(module).__file__).parent
    for pattern in PACKAGE_FILES:
        for path in source.rglob(pattern):
            if "__pycache__" in path.parts:
                continue
            target = destination / module / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)


def _copy_licenses(distribution: str, destination: Path) -> list[str]:
    dist = importlib.metadata.distribution(distribution)
    copied = []
    for file in dist.files or []:
        name = Path(str(file)).name.upper()
        if name.startswith(("LICENSE", "LICENCE", "COPYING")):
            target = destination / f"{distribution}-{Path(str(file)).name}"
            shutil.copyfile(dist.locate_file(file), target)
            copied.append(target.name)
    return copied


def build_kit(destination: Path) -> dict:
    """Crea il kit in 'destination' (che non deve esistere); restituisce la sua descrizione."""
    if destination.exists():
        raise HammerError(f"{destination} esiste già")
    tool = destination / "tools" / "hammer"
    lib, licenses = tool / "lib", tool / "licenses"
    lib.mkdir(parents=True)
    licenses.mkdir()
    components = {}
    for module, distribution in VENDORED.items():
        _copy_package(module, lib)
        found = _copy_licenses(distribution, licenses)
        if not found and module != "hammer":
            raise HammerError(f"licenza di {distribution} non trovata: il kit non può includerla")
        components[distribution] = {"version": importlib.metadata.version(distribution), "licenses": found}
    (tool / "run").write_text(RUN)
    (tool / "run").chmod(0o755)
    (tool / "main.py").write_text(MAIN)
    (destination / "swarmforge-tools-v1").write_text("1\n")

    files = {path.relative_to(destination).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(destination.rglob("*")) if path.is_file()}
    description = {"hammer_version": __version__, "requires": "python3 >= 3.11",
                   "components": components, "files": files}
    (tool / "kit.json").write_text(json.dumps(description, indent=2, sort_keys=True) + "\n")
    return description
