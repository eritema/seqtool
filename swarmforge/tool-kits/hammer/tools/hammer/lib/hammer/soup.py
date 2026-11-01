"""Dipendenze dai file di lock, per creare o aggiornare gli elementi SOUP.

L'import riempie solo identità e versione. Produttore, scopo, requisiti e
valutazione delle anomalie restano a una persona.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .errors import HammerError


@dataclass(frozen=True)
class Dependency:
    package: str    # identità stabile, senza versione: "pypi:pyyaml", "npm:@scope/x", "r:Biostrings"
    name: str
    version: str


def _pypi(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()   # nome normalizzato (PEP 503)


_REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*==\s*([^\s;#]+)")


def parse_requirements(text: str) -> tuple[list[Dependency], list[str]]:
    """Righe 'nome==versione' (pip freeze, pip-compile); le altre sono segnalate."""
    deps, skipped = [], []
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].strip().removesuffix("\\").strip()
        if not line or line.startswith(("#", "-")):
            continue   # commenti, opzioni (-r, -e, --hash …)
        match = _REQUIREMENT.match(line)
        if match:
            deps.append(Dependency(f"pypi:{_pypi(match.group(1))}", match.group(1), match.group(2)))
        else:
            skipped.append(line)
    return deps, skipped


def parse_package_lock(text: str, include_dev: bool = False) -> tuple[list[Dependency], list[str]]:
    data = json.loads(text)
    if data.get("lockfileVersion", 1) < 2 or "packages" not in data:
        raise HammerError("package-lock.json: serve lockfileVersion 2 o 3 (npm 7 o successivo)")
    deps, skipped = [], []
    for path, info in data["packages"].items():
        if not path or "node_modules/" not in path:
            continue  # il progetto stesso, o un workspace locale
        name = info.get("name") or path.rsplit("node_modules/", 1)[1]
        if info.get("link"):
            skipped.append(f"{name} (link locale)")
            continue
        if info.get("dev") and not include_dev:
            continue
        deps.append(Dependency(f"npm:{name}", name, info.get("version", "")))
    return deps, skipped


def parse_renv_lock(text: str) -> tuple[list[Dependency], list[str]]:
    packages = json.loads(text).get("Packages", {})
    return [Dependency(f"r:{info.get('Package', name)}", info.get("Package", name), info.get("Version", ""))
            for name, info in packages.items()], []


def parse_python_toml_lock(text: str) -> tuple[list[Dependency], list[str]]:
    """uv.lock e poetry.lock: tabelle [[package]] con name e version."""
    deps, skipped = [], []
    for package in tomllib.loads(text).get("package", []):
        source = package.get("source", {})
        if isinstance(source, dict) and ("editable" in source or "virtual" in source):
            skipped.append(f"{package['name']} (progetto locale)")
            continue
        deps.append(Dependency(f"pypi:{_pypi(package['name'])}", package["name"], str(package.get("version", ""))))
    return deps, skipped


FORMATS = {
    "requirements": parse_requirements,
    "package-lock": parse_package_lock,
    "renv": parse_renv_lock,
    "uv": parse_python_toml_lock,
    "poetry": parse_python_toml_lock,
}


def detect_format(path: Path) -> str:
    name = path.name
    if name == "package-lock.json":
        return "package-lock"
    if name == "renv.lock":
        return "renv"
    if name in ("uv.lock", "poetry.lock"):
        return name.split(".")[0]
    if name.startswith("requirements") and name.endswith(".txt"):
        return "requirements"
    raise HammerError(f"formato di {name} non riconosciuto: indicarlo con --format ({', '.join(FORMATS)})")


def read_lock(path: Path, fmt: str | None = None, include_dev: bool = False) -> tuple[list[Dependency], list[str]]:
    fmt = fmt or detect_format(path)
    text = path.read_text("utf-8")
    try:
        if fmt == "package-lock":
            deps, skipped = parse_package_lock(text, include_dev)
        else:
            deps, skipped = FORMATS[fmt](text)
    except (json.JSONDecodeError, tomllib.TOMLDecodeError, KeyError, AttributeError) as error:
        raise HammerError(f"{path.name}: file di lock non valido ({fmt}): {error}") from error
    return _merge_versions(deps), skipped


def _merge_versions(deps: list[Dependency]) -> list[Dependency]:
    """npm può installare più versioni dello stesso pacchetto: un solo SOUP con tutte le versioni."""
    versions: dict[str, set[str]] = {}
    names: dict[str, str] = {}
    for dep in deps:
        versions.setdefault(dep.package, set()).add(dep.version)
        names.setdefault(dep.package, dep.name)
    return [Dependency(package, names[package], ", ".join(sorted(v for v in versions[package] if v)))
            for package in sorted(versions)]


def ecosystem(package: str) -> str:
    return package.split(":", 1)[0]
