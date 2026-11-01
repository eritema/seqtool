"""Controlli su schema, integrità e link sospetti."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .risk import evaluate
from .items import ID_PATTERN, Item, LoadResult, item_path

ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    item: str
    message: str

    def as_dict(self) -> dict:
        return {"severity": self.severity, "code": self.code, "item": self.item, "message": self.message}


def check_project(config: Config, loaded: LoadResult, cq_dir, test_results: dict | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for path, message in loaded.errors:
        findings.append(Finding(ERROR, "parse", str(path.relative_to(cq_dir.parent)), message))

    seen: dict[str, Item] = {}
    for item in loaded.items:
        if item.id in seen:
            findings.append(Finding(ERROR, "duplicate-id", item.id,
                                    f"ID già usato da {seen[item.id].path.name}"))
        else:
            seen[item.id] = item
        findings.extend(_check_item(config, item, cq_dir))

    mitigated_by: dict[str, list[str]] = {}
    for item in loaded.items:
        findings.extend(_check_links(config, item, seen))
        for link in item.links:
            if link.rel == "mitigates":
                mitigated_by.setdefault(link.to, []).append(item.id)

    if config.risk is not None:
        for item in loaded.items:
            if item.type == "RISK" and item.status != "obsolete":
                findings.extend(_check_risk(config, item, mitigated_by.get(item.id, [])))

    findings.extend(_check_coverage(config, loaded.items, seen))
    findings.extend(_check_controls_verified(loaded.items, seen))
    if config.safety_class in ("B", "C"):
        findings.extend(_check_soup_anomalies(config, loaded.items))
    for test_id, result in sorted((test_results or {}).items()):
        item = seen.get(test_id)
        if item is not None and item.status != "obsolete" and result.result in ("fail", "error"):
            where = f"commit {result.commit[:7]}" if result.commit else "commit sconosciuto"
            stale = "" if result.current else ", non sul commit corrente"
            findings.append(Finding(WARNING, "test-failing", test_id,
                                    f"ultimo esito {result.result} ({result.origin}, {where}{stale})"))
    return findings


def _incoming(items: list[Item]) -> dict[str, list[tuple[str, Item]]]:
    """Collegamenti entranti da elementi non obsoleti: id → [(relazione, sorgente)]."""
    incoming: dict[str, list[tuple[str, Item]]] = {}
    for item in items:
        if item.status == "obsolete":
            continue
        for link in item.links:
            incoming.setdefault(link.to, []).append((link.rel, item))
    return incoming


def _check_coverage(config: Config, items: list[Item], by_id: dict[str, Item]) -> list[Finding]:
    """Ogni elemento dei tipi in [coverage] deve soddisfare tutte le regole indicate."""
    incoming = _incoming(items)
    found = []
    for item in by_id.values():
        if item.status == "obsolete":
            continue
        for rule in config.coverage.get(item.type, ()):
            if rule.applies_to(item) and not any(rule.accepts(rel, source) for rel, source in incoming.get(item.id, [])):
                found.append(Finding(WARNING, "not-covered", item.id, f"manca {rule.describe()}"))
    return found


def _check_controls_verified(items: list[Item], by_id: dict[str, Item]) -> list[Finding]:
    """ISO 14971 §7.2: l'attuazione di ogni misura di controllo va verificata."""
    incoming = _incoming(items)
    found = []
    for item in by_id.values():
        if item.status == "obsolete":
            continue
        risks = sorted(link.to for link in item.links if link.rel == "mitigates"
                       and link.to in by_id and by_id[link.to].status != "obsolete")
        verified = any(rel == "verifies" and source.type == "TEST" for rel, source in incoming.get(item.id, []))
        if risks and not verified:
            found.append(Finding(WARNING, "control-not-verified", item.id,
                                 f"misura di controllo di {', '.join(risks)} senza TEST che la verifichi"))
    return found


def _check_soup_anomalies(config: Config, items: list[Item]) -> list[Finding]:
    """IEC 62304 §7.1.3 (classi B e C): le anomalie note di ogni SOUP vanno valutate."""
    spec = config.types.get("SOUP")
    if spec is None or "anomalies_evaluated" not in spec.fields:
        return []
    return [Finding(WARNING, "soup-anomalies-not-evaluated", item.id,
                    "anomalie note non valutate (anomalies_evaluated diverso da yes)")
            for item in items
            if item.type == "SOUP" and item.status != "obsolete" and item.fields.get("anomalies_evaluated") != "yes"]


def _check_risk(config: Config, item: Item, controls: list[str]) -> list[Finding]:
    """Regole ISO 14971: un rischio inaccettabile va controllato e il residuo valutato."""
    evaluation = evaluate(config.risk, item.fields)
    if evaluation is None:
        return []  # P1, P2, P3 o gravità mancanti o non validi: già segnalati dallo schema
    found: list[Finding] = []
    initial = evaluation.initial
    if not initial.acceptable and not controls:
        found.append(Finding(ERROR, "risk-not-controlled", item.id,
                             f"rischio {initial.risk_class} non accettabile e senza misure di controllo (mitigates)"))
    if controls and evaluation.residual is None:
        found.append(Finding(WARNING, "residual-not-estimated", item.id,
                             f"ha misure di controllo ({', '.join(sorted(controls))}) ma manca reduction_factor"))
    residual = evaluation.residual
    if residual is not None and not residual.acceptable and item.fields.get("final_acceptance") != "benefit_risk":
        found.append(Finding(ERROR, "residual-not-acceptable", item.id,
                             f"rischio residuo {residual.risk_class} non accettabile: ridurlo o indicare "
                             "final_acceptance: benefit_risk dopo l'analisi beneficio/rischio"))
    return found


def _check_item(config: Config, item: Item, cq_dir) -> list[Finding]:
    found: list[Finding] = []
    match = ID_PATTERN.match(item.id)
    if not match:
        return [Finding(ERROR, "invalid-id", item.id, "ID non nel formato TIPO-NNNN")]
    if match.group(1) != item.type:
        found.append(Finding(ERROR, "id-type-mismatch", item.id, f"il prefisso non corrisponde al tipo {item.type}"))
    if item.path is not None and item.path.resolve() != item_path(cq_dir, item.id).resolve():
        found.append(Finding(ERROR, "wrong-path", item.id,
                             f"il file dovrebbe essere {item_path(cq_dir, item.id).relative_to(cq_dir.parent)}"))
    if item.status not in config.statuses:
        found.append(Finding(ERROR, "invalid-status", item.id,
                             f"stato {item.status!r} non ammesso ({', '.join(config.statuses)})"))
    if not item.title.strip():
        found.append(Finding(ERROR, "empty-title", item.id, "titolo vuoto"))

    spec = config.types.get(item.type)
    if spec is None:
        found.append(Finding(ERROR, "unknown-type", item.id, f"tipo {item.type!r} non definito in hammer.toml"))
        return found
    for name, field_spec in spec.fields.items():
        value = item.fields.get(name)
        if value in (None, "", []):
            if field_spec.required:
                found.append(Finding(ERROR, "missing-field", item.id, f"campo obbligatorio {name!r} mancante"))
            continue
        if isinstance(value, list) and not field_spec.is_list:
            found.append(Finding(ERROR, "invalid-field", item.id, f"{name}: atteso un solo valore, trovata una lista"))
            continue
        for single in value if isinstance(value, list) else [value]:
            problem = field_spec.problem(single)
            if problem:
                found.append(Finding(ERROR, "invalid-field", item.id, problem))
    for name in item.fields:
        if name not in spec.fields:
            found.append(Finding(WARNING, "unknown-field", item.id, f"campo {name!r} non definito per {item.type}"))
    return found


def _check_links(config: Config, item: Item, items: dict[str, Item]) -> list[Finding]:
    found: list[Finding] = []
    seen = set()
    for link in item.links:
        key = (link.rel, link.to)
        if key in seen:
            found.append(Finding(ERROR, "duplicate-link", item.id, f"{link.rel} {link.to} ripetuto"))
        seen.add(key)
        if link.to == item.id:
            found.append(Finding(ERROR, "self-link", item.id, f"{link.rel} verso se stesso"))
            continue
        target = items.get(link.to)
        if target is None:
            found.append(Finding(ERROR, "missing-target", item.id, f"{link.rel} {link.to}: elemento inesistente"))
            continue
        if link.rel not in config.relations:
            found.append(Finding(ERROR, "unknown-relation", item.id, f"relazione {link.rel!r} non definita"))
        elif not config.relation_allowed(link.rel, item.type, target.type):
            found.append(Finding(ERROR, "relation-not-allowed", item.id,
                                 f"{item.type} {link.rel} {target.type} non ammesso"))
        if target.status == "obsolete" and item.status != "obsolete":
            found.append(Finding(WARNING, "obsolete-target", item.id, f"{link.rel} {link.to}: elemento obsoleto"))
        if link.fingerprint != target.fingerprint():
            found.append(Finding(WARNING, "suspect-link", item.id,
                                 f"{link.rel} {link.to}: il bersaglio è cambiato dopo il collegamento "
                                 f"(verificare e confermare con 'hammer review {link.to}')"))
    return found


OPEN_PROBLEM_STATES = ("open", "investigating")


def check_release(items: dict[str, Item], results: dict) -> list[Finding]:
    """Condizioni di rilascio (IEC 62304 §5.8): esiti sul commit, anomalie valutate, elementi approvati.

    'results' deve contenere solo esiti validi per il rilascio (CI e verifiche manuali dei test manuali).
    """
    found: list[Finding] = []
    for item in sorted(items.values(), key=lambda i: i.id):
        if item.status == "obsolete":
            continue
        if item.type in ("TEST", "VAL"):
            result = results.get(item.id)
            if result is None or not result.current or result.result == "skip":
                found.append(Finding(ERROR, "release-test-not-run", item.id, "nessun esito valido sul commit di rilascio"))
            elif result.result in ("fail", "error"):
                found.append(Finding(ERROR, "release-test-failing", item.id, f"esito {result.result} sul commit di rilascio"))
        if item.type == "PRB" and item.fields.get("state") in OPEN_PROBLEM_STATES and not item.fields.get("criticality"):
            found.append(Finding(ERROR, "release-prb-not-evaluated", item.id,
                                 "anomalia aperta senza valutazione (criticality) per il rilascio"))
        if item.status != "approved" and item.type != "PRB":
            found.append(Finding(WARNING, "release-not-approved", item.id, f"stato {item.status}, non approved"))
    return found
