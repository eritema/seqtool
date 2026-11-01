"""Matrice di tracciabilità: una riga per elemento del tipo scelto (predefinito SRS)."""

from __future__ import annotations

import csv
import io

from .evidence import TestResult
from .items import Item

VERIFYING_RELATIONS = ("verifies", "validates")

# Stato di una riga, dal peggiore al migliore.
UNCOVERED = "uncovered"   # nessun elemento la verifica
FAILING = "failing"       # almeno un esito fail/error
NOT_RUN = "not-run"       # almeno un TEST senza esito
OUTDATED = "outdated"     # esiti presenti ma non tutti sul commit di riferimento
PASSING = "passing"       # tutti gli esiti pass sul commit di riferimento


def build_matrix(items: dict[str, Item], results: dict[str, TestResult], row_type: str) -> list[dict]:
    incoming: dict[str, list[tuple[str, Item]]] = {}
    for item in items.values():
        if item.status == "obsolete":
            continue
        for link in item.links:
            incoming.setdefault(link.to, []).append((link.rel, item))

    rows = []
    for item in sorted(items.values(), key=lambda i: i.id):
        if item.type != row_type or item.status == "obsolete":
            continue
        verifiers = sorted((source for rel, source in incoming.get(item.id, []) if rel in VERIFYING_RELATIONS),
                           key=lambda i: i.id)
        tests = []
        for verifier in verifiers:
            result = results.get(verifier.id)
            tests.append({
                "id": verifier.id, "title": verifier.title, "type": verifier.type,
                "method": verifier.fields.get("method"),
                "result": result.result if result else None,
                "evidence": result.as_dict() if result else None,
                "suspect": any(link.to == item.id and link.fingerprint != item.fingerprint()
                               for link in verifier.links),
            })
        rows.append({
            "id": item.id, "title": item.title, "status": item.status,
            "derives_from": sorted(link.to for link in item.links if link.rel == "derives_from"),
            "risks": sorted(link.to for link in item.links if link.rel == "mitigates"),
            "tests": tests,
            "suspect_links": sorted(link.to for link in item.links
                                    if link.to in items and link.fingerprint != items[link.to].fingerprint()),
            "coverage": _row_status(tests),
        })
    return rows


def _row_status(tests: list[dict]) -> str:
    if not tests:
        return UNCOVERED
    if any(t["result"] in ("fail", "error") for t in tests):
        return FAILING
    if any(t["result"] is None or t["result"] == "skip" for t in tests):
        return NOT_RUN
    if not all(t["evidence"]["current"] for t in tests):
        return OUTDATED
    return PASSING


def summarize(rows: list[dict]) -> dict:
    summary = {state: 0 for state in (PASSING, OUTDATED, NOT_RUN, FAILING, UNCOVERED)}
    for row in rows:
        summary[row["coverage"]] += 1
    summary["total"] = len(rows)
    return summary


def _test_label(test: dict) -> str:
    if test["result"] is None:
        outcome = "nessun esito"
    else:
        evidence = test["evidence"]
        commit = (evidence["commit"] or "")[:7] or "?"
        outcome = f"{test['result']} ({evidence['origin']}, {commit}{'' if evidence['current'] else ', non corrente'})"
    suspect = " [sospetto]" if test["suspect"] else ""
    return f"{test['id']} {outcome}{suspect}"


def row_cells(row: dict) -> list[str]:
    return [
        row["id"], row["title"], ", ".join(row["derives_from"]), ", ".join(row["risks"]),
        "; ".join(_test_label(t) for t in row["tests"]),
        row["coverage"] + (" [link sospetti]" if row["suspect_links"] else ""),
    ]


HEADERS = ["Elemento", "Titolo", "Deriva da", "Rischi", "Verificato da (esito)", "Stato"]


def to_markdown(rows: list[dict], header: dict) -> str:
    out = [f"# Matrice di tracciabilità — {header['type']}", ""]
    out.append(f"Progetto: {header['project']}  ")
    out.append(f"Commit di riferimento: {header['commit'] or 'non disponibile'}  ")
    out.append(f"Evidenze: {'solo CI e verifiche manuali dei test manuali' if header['ci_only'] else 'tutte'}  ")
    out.append(f"Generata da hammer {header['hammer_version']}")
    out.append("")
    out.append("| " + " | ".join(HEADERS) + " |")
    out.append("|" + "---|" * len(HEADERS))
    for row in rows:
        out.append("| " + " | ".join(cell.replace("|", "\\|") for cell in row_cells(row)) + " |")
    summary = summarize(rows)
    out.append("")
    out.append(", ".join(f"{state}: {summary[state]}" for state in (PASSING, OUTDATED, NOT_RUN, FAILING, UNCOVERED))
               + f" (totale {summary['total']})")
    return "\n".join(out) + "\n"


def to_csv(rows: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(HEADERS)
    for row in rows:
        writer.writerow(row_cells(row))
    return buffer.getvalue()
