"""Baseline: uno stato congelato per il rilascio.

Il manifest 'cq/baselines/NOME.json' registra commit, impronte degli elementi,
esiti validi e controlli. Le evidenze usate sono copiate in
'cq/evidence/NOME/'. Gli export di una baseline rileggono gli elementi dal
commit con 'git archive', non dal working tree.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from . import __version__
from .config import CQ_DIR
from .errors import HammerError
from .evidence import ci_accept, latest_results
from .items import Item

FORMAT_VERSION = 1
BASELINES_DIR = "baselines"
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def manifest_path(cq_dir: Path, name: str) -> Path:
    if not NAME_PATTERN.match(name):
        raise HammerError(f"nome di baseline non valido: {name!r} (lettere, cifre, '.', '_', '-')")
    return cq_dir / BASELINES_DIR / f"{name}.json"


def load_manifest(cq_dir: Path, name: str) -> dict:
    path = manifest_path(cq_dir, name)
    if not path.is_file():
        raise HammerError(f"baseline {name} non trovata ({path.relative_to(cq_dir.parent)})")
    try:
        data = json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError as error:
        raise HammerError(f"{path.name}: manifest non valido: {error}") from error
    if data.get("format_version") != FORMAT_VERSION:
        raise HammerError(f"{path.name}: versione del manifest non supportata")
    return data


def list_manifests(cq_dir: Path) -> list[dict]:
    folder = cq_dir / BASELINES_DIR
    return [load_manifest(cq_dir, path.stem) for path in sorted(folder.glob("*.json"))] if folder.is_dir() else []


# ------------------------------------------------------------------ git

def _git(root: Path, *args: str, binary: bool = False):
    process = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=not binary)
    if process.returncode != 0:
        error = process.stderr.decode() if binary else process.stderr
        raise HammerError(f"git {' '.join(args)}: {error.strip()}")
    return process.stdout


def uncommitted_changes(root: Path) -> list[str]:
    """Modifiche ai file tracciati, più i file non tracciati sotto cq/ (entrerebbero negli elementi)."""
    tracked = _git(root, "status", "--porcelain", "--untracked-files=no").splitlines()
    untracked = _git(root, "status", "--porcelain", "--untracked-files=all", "--", CQ_DIR).splitlines()
    return sorted(set(tracked) | {line for line in untracked if line.startswith("??")})


@contextmanager
def cq_at_commit(root: Path, commit: str):
    """Cartella temporanea con la cq/ del commit, estratta con 'git archive'."""
    data = _git(root, "archive", "--format=tar", commit, CQ_DIR, binary=True)
    with TemporaryDirectory(prefix="hammer-baseline-") as folder:
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            archive.extractall(folder, filter="data")
        yield Path(folder)


# ------------------------------------------------------------- evidenze

def release_evidence(evidence_list: list[dict], commit: str, items: dict[str, Item]) -> list[dict]:
    """Evidenze valide per il rilascio: prodotte sul commit, senza modifiche, da CI o manuali ammesse."""
    accept = ci_accept(items)
    selected = []
    for evidence in evidence_list:
        if evidence.get("commit") != commit or evidence.get("dirty"):
            continue
        tests = {test for case in evidence.get("cases", []) for test in case.get("tests", [])}
        if any(accept(evidence, test) for test in tests):
            selected.append(evidence)
    return selected


def release_results(evidence_list: list[dict], commit: str, items: dict[str, Item]) -> dict:
    return latest_results(evidence_list, commit, ci_accept(items))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_evidence(evidence_list: list[dict], root: Path, destination: Path) -> list[dict]:
    """Copia le evidenze e i loro allegati nella cartella della baseline; restituisce l'elenco per il manifest."""
    destination.mkdir(parents=True, exist_ok=True)
    copied = []
    for evidence in sorted(evidence_list, key=lambda e: e["_path"]):
        source = Path(evidence["_path"])
        target = destination / source.name
        if target.exists() and _sha256(target) != _sha256(source):
            raise HammerError(f"{target}: esiste già con un contenuto diverso")
        shutil.copyfile(source, target)
        entry = {"file": target.relative_to(root).as_posix(), "sha256": _sha256(target),
                 "origin": evidence.get("origin"), "created": evidence.get("created"),
                 "url": (evidence.get("ci") or {}).get("url"), "attachments": []}
        for attachment in evidence.get("attachments", []):
            stored = root / attachment["stored"]
            if not stored.is_file() or _sha256(stored) != attachment["sha256"]:
                raise HammerError(f"allegato mancante o alterato: {attachment['stored']}")
            copy = destination / "attachments" / stored.name
            copy.parent.mkdir(exist_ok=True)
            shutil.copyfile(stored, copy)
            entry["attachments"].append({"file": copy.relative_to(root).as_posix(), "sha256": attachment["sha256"]})
        copied.append(entry)
    return copied


# ------------------------------------------------------------- manifest

def build_manifest(name: str, created: str, commit: str, project_name: str, safety_class: str,
                   items: dict[str, Item], results: dict, evidence: list[dict], findings: list) -> dict:
    return {
        "format_version": FORMAT_VERSION,
        "name": name,
        "created": created,
        "commit": commit,
        "hammer_version": __version__,
        "project": project_name,
        "safety_class": safety_class,
        "items": {item.id: {"type": item.type, "status": item.status, "title": item.title,
                            "fingerprint": item.fingerprint()}
                  for item in sorted(items.values(), key=lambda i: i.id)},
        "results": {test: {"result": r.result, "origin": r.origin, "evidence": Path(r.source).name}
                    for test, r in sorted(results.items())},
        "evidence": evidence,
        "findings": [f.as_dict() for f in findings],
    }


def write_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", "utf-8")


def verify_items(manifest: dict, items: dict[str, Item]) -> list[str]:
    """Differenze fra gli elementi letti dal commit e quelli registrati nel manifest."""
    recorded = manifest["items"]
    problems = [f"{i}: assente nel commit" for i in sorted(set(recorded) - set(items))]
    problems += [f"{i}: non registrato nel manifest" for i in sorted(set(items) - set(recorded))]
    problems += [f"{i}: impronta diversa" for i in sorted(set(items) & set(recorded))
                 if items[i].fingerprint() != recorded[i]["fingerprint"]]
    return problems


def diff(old: dict, new: dict) -> dict:
    """Confronto di due stati {'items': {...}, 'results': {...}}."""
    a, b = old["items"], new["items"]
    changes = {
        "added": sorted(set(b) - set(a)),
        "removed": sorted(set(a) - set(b)),
        "changed": sorted(i for i in set(a) & set(b) if a[i]["fingerprint"] != b[i]["fingerprint"]),
        "status": {i: [a[i]["status"], b[i]["status"]] for i in sorted(set(a) & set(b))
                   if a[i]["status"] != b[i]["status"]},
    }
    if "results" in old and "results" in new:
        ra, rb = old["results"], new["results"]
        changes["results"] = {t: [(ra.get(t) or {}).get("result"), (rb.get(t) or {}).get("result")]
                              for t in sorted(set(ra) | set(rb))
                              if (ra.get(t) or {}).get("result") != (rb.get(t) or {}).get("result")}
    return changes
