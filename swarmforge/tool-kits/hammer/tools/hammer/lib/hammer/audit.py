"""Frammenti per l'esito dell'audit di SwarmForge ('swarm_audit.sh complete').

Per gli elementi modificati da un commit di base: una voce 'requirements' per
elemento, una voce 'checks' per 'hammer check' e i rilievi come 'findings'.
SwarmForge non conclude un audit con 'findings' non vuoti, quindi un errore di
'hammer check' blocca la consegna finché non è corretto.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .checks import ERROR, Finding
from .config import CQ_DIR
from .errors import HammerError
from .items import ID_PATTERN, ITEMS_DIR, Item

VERIFIERS = ("verifies", "validates")


def changed_ids(root: Path, base: str) -> list[str]:
    """ID degli elementi modificati rispetto a 'base', working tree compreso, file nuovi inclusi."""
    folder = f"{CQ_DIR}/{ITEMS_DIR}"
    commands = (["git", "-C", str(root), "diff", "--name-only", base, "--", folder],
                ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "--", folder])
    names = set()
    for command in commands:
        process = subprocess.run(command, capture_output=True, text=True)
        if process.returncode != 0:
            raise HammerError(f"git: {process.stderr.strip()}")
        names.update(process.stdout.split())
    return sorted({Path(name).stem for name in names if ID_PATTERN.match(Path(name).stem)})


def _verifiers(item: Item, items: dict[str, Item]) -> list[Item]:
    if item.type in ("TEST", "VAL"):
        return [item]
    return sorted((source for source in items.values() if source.status != "obsolete"
                   and any(link.rel in VERIFIERS and link.to == item.id for link in source.links)),
                  key=lambda i: i.id)


def requirement_entry(item_id: str, items: dict[str, Item], results: dict, findings: list[Finding]) -> dict:
    item = items.get(item_id)
    if item is None:
        return {"requirement": item_id, "status": "not_run", "evidence": "elemento rimosso: nessuna verifica da eseguire"}
    label = f"{item.id} {item.title}"
    own = [f for f in findings if f.item == item.id]
    if item.status == "obsolete":
        return {"requirement": label, "status": "verified", "evidence": "reso obsoleto"}
    if own:
        return {"requirement": label, "status": "not_run",
                "evidence": "rilievi di hammer check: " + "; ".join(f"{f.code} {f.message}" for f in own)}
    verifiers = _verifiers(item, items)
    if not verifiers:
        return {"requirement": label, "status": "not_run",
                "evidence": "nessun TEST o VAL lo verifica; hammer check senza rilievi"}
    outcomes = []
    for verifier in verifiers:
        result = results.get(verifier.id)
        if result is None:
            outcomes.append((False, f"{verifier.id} senza esito"))
        elif not result.current:
            outcomes.append((False, f"{verifier.id} {result.result} su un altro commit o con modifiche non committate"))
        else:
            outcomes.append((result.result == "pass",
                             f"{verifier.id} {result.result} ({result.origin}, {(result.commit or '')[:7]})"))
    status = "verified" if all(ok for ok, _ in outcomes) else "not_run"
    return {"requirement": label, "status": status, "evidence": "; ".join(text for _, text in outcomes)}


def audit_fragment(item_ids: list[str], items: dict[str, Item], results: dict, findings: list[Finding],
                   strict: bool) -> dict:
    blocking = [f for f in findings if f.severity == ERROR or strict]
    errors = sum(f.severity == ERROR for f in findings)
    command = "hammer check --strict" if strict else "hammer check"
    check = {"command": command, "status": "passed" if not blocking else "not_run",
             "evidence": f"{errors} errori, {len(findings) - errors} avvisi"
                         + ("" if not blocking else ": correggere i rilievi in findings")}
    return {
        "requirements": [requirement_entry(i, items, results, findings) for i in item_ids],
        "checks": [check],
        "findings": [f"hammer {f.severity} {f.item} {f.code}: {f.message}" for f in blocking],
    }


def merge_into(result: dict, fragment: dict) -> dict:
    """Aggiorna un esito di audit: sostituisce le voci di hammer, lascia le altre e la decisione."""
    ids = {entry["requirement"].split(" ", 1)[0] for entry in fragment["requirements"]}
    merged = dict(result)
    merged["requirements"] = [r for r in result.get("requirements", [])
                              if str(r.get("requirement", "")).split(" ", 1)[0] not in ids] + fragment["requirements"]
    merged["checks"] = [c for c in result.get("checks", [])
                        if not str(c.get("command", "")).startswith("hammer ")] + fragment["checks"]
    merged["findings"] = [f for f in result.get("findings", [])
                          if not str(f).startswith("hammer ")] + fragment["findings"]
    return merged
