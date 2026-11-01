"""Evidenze: esiti dei test normalizzati e legati a un commit.

Un'evidenza è un file JSON prodotto da 'hammer evidence import' (da un report
JUnit XML o TAP) o da 'hammer evidence record' (verifica manuale).
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .errors import HammerError

FORMAT_VERSION = 1
MARKER = re.compile(r"\[([A-Z][A-Z0-9]*-\d{4,})\]")
PROPERTY_ID = re.compile(r"\b([A-Z][A-Z0-9]*-\d{4,})\b")
PROPERTY_NAME = "hammer"
RESULTS = ("pass", "fail", "error", "skip")
ORIGINS = ("ci", "local", "manual")


@dataclass
class Case:
    name: str
    result: str
    classname: str = ""
    duration: float | None = None
    message: str | None = None
    properties: tuple[str, ...] = ()   # ID dichiarati nella proprietà JUnit "hammer"

    def markers(self) -> list[str]:
        found = set(MARKER.findall(f"{self.classname} {self.name}"))
        found.update(PROPERTY_ID.findall(" ".join(self.properties)))
        return sorted(found)


# ----------------------------------------------------------------- parser

def parse_junit(text: str) -> list[Case]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise HammerError(f"JUnit XML non valido: {error}") from error
    cases = []
    for element in root.iter("testcase"):
        result, message = "pass", None
        for tag, outcome in (("failure", "fail"), ("error", "error"), ("skipped", "skip")):
            child = element.find(tag)
            if child is not None:
                result = outcome
                message = (child.get("message") or (child.text or "").strip() or None)
                break
        time = element.get("time")
        try:
            duration = float(time) if time not in (None, "") else None
        except ValueError:
            duration = None
        properties = tuple(prop.get("value", "") for prop in element.iter("property")
                           if prop.get("name") == PROPERTY_NAME)
        cases.append(Case(name=element.get("name", ""), classname=element.get("classname", ""),
                          result=result, duration=duration,
                          message=message[:500] if message else None, properties=properties))
    return cases


_TAP_LINE = re.compile(r"^\s*(not ok|ok)\b(?:\s+\d+)?(?:\s*-)?\s*(.*?)\s*$")


def parse_tap(text: str) -> list[Case]:
    """Righe 'ok' / 'not ok', anche annidate (sottotest di node --test)."""
    cases = []
    for line in text.splitlines():
        match = _TAP_LINE.match(line)
        if not match:
            continue
        status, description = match.groups()
        name, _, directive = description.partition(" # ")
        directive = directive.strip().upper()
        if directive.startswith(("SKIP", "TODO")):
            result = "skip"
        else:
            result = "pass" if status == "ok" else "fail"
        cases.append(Case(name=name.strip(), result=result))
    return cases


PARSERS = {"junit": parse_junit, "tap": parse_tap}


# ------------------------------------------------------------ costruzione

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def environment() -> dict:
    return {"os": platform.system(), "os_release": platform.release(), "machine": platform.machine()}


def build_evidence(cases: list[Case], known_tests: set[str], *, origin: str, commit: str | None,
                   dirty: bool | None, ci: dict | None, report: dict | None,
                   recorded_by: str | None = None, note: str | None = None,
                   attachments: list[dict] | None = None) -> dict:
    if origin not in ORIGINS:
        raise HammerError(f"origine non valida: {origin}")
    entries, unknown = [], set()
    summary = {result: 0 for result in RESULTS} | {"total": len(cases), "unlinked": 0}
    for case in cases:
        summary[case.result] += 1
        markers = case.markers()
        unknown.update(m for m in markers if m not in known_tests)
        linked = [m for m in markers if m in known_tests]
        if not linked:
            summary["unlinked"] += 1
        entries.append({"name": case.name, "classname": case.classname, "tests": linked,
                        "result": case.result, "duration": case.duration, "message": case.message})
    evidence = {
        "format_version": FORMAT_VERSION,
        "hammer_version": __version__,
        "origin": origin,
        "created": _now(),
        "commit": commit,
        "dirty": dirty,
        "ci": ci,
        "environment": ci.get("environment") if ci and ci.get("environment") else environment(),
        "report": report,
        "summary": summary,
        "unknown_markers": sorted(unknown),
        "cases": entries,
    }
    if recorded_by is not None:
        evidence["recorded_by"] = recorded_by
    if note:
        evidence["note"] = note
    if attachments:
        evidence["attachments"] = attachments
    return evidence


def report_info(path: Path, fmt: str) -> dict:
    data = path.read_bytes()
    return {"file": path.name, "format": fmt, "sha256": hashlib.sha256(data).hexdigest()}


def evidence_filename(evidence: dict) -> str:
    stamp = evidence["created"].replace("-", "").replace(":", "")
    commit = (evidence.get("commit") or "nocommit")[:7]
    run = (evidence.get("ci") or {}).get("run_id")
    suffix = f"-{run}" if run else ""
    return f"{stamp}-{commit}-{evidence['origin']}{suffix}.json"


def write_evidence(evidence: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n", "utf-8")
    return path


# ---------------------------------------------------------------- lettura

def load_evidence(sources: list[Path]) -> tuple[list[dict], list[tuple[Path, str]]]:
    """Legge le evidenze da file o cartelle (ricorsivamente). Restituisce evidenze ed errori."""
    loaded, errors, seen = [], [], set()
    files = []
    for source in sources:
        if source.is_dir():
            files.extend(sorted(source.rglob("*.json")))
        elif source.is_file():
            files.append(source)
    for path in files:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            errors.append((path, f"non leggibile: {error}"))
            continue
        if not isinstance(data, dict) or data.get("format_version") != FORMAT_VERSION or "cases" not in data:
            errors.append((path, "non è un'evidenza hammer"))
            continue
        data["_path"] = str(path)
        loaded.append(data)
    return loaded, errors


@dataclass(frozen=True)
class TestResult:
    test: str
    result: str          # pass | fail | error | skip
    commit: str | None
    created: str
    origin: str
    url: str | None
    source: str
    current: bool = True  # prodotto sul commit di riferimento

    def as_dict(self) -> dict:
        return {"result": self.result, "commit": self.commit, "created": self.created,
                "origin": self.origin, "url": self.url, "current": self.current}


def _aggregate(results: list[str]) -> str:
    """Esito di un TEST con più casi: fallisce se un caso fallisce, è saltato se lo sono tutti."""
    for outcome in ("error", "fail"):
        if outcome in results:
            return outcome
    if results and all(r == "skip" for r in results):
        return "skip"
    return "pass"


def results_by_test(evidence: dict) -> dict[str, str]:
    grouped: dict[str, list[str]] = {}
    for case in evidence.get("cases", []):
        for test in case.get("tests", []):
            grouped.setdefault(test, []).append(case.get("result", "error"))
    return {test: _aggregate(results) for test, results in grouped.items()}


def latest_results(evidence_list: list[dict], target_commit: str | None,
                   accept=lambda evidence, test: True) -> dict[str, TestResult]:
    """Esito più recente di ogni TEST.

    Si preferiscono le evidenze prodotte su target_commit; se non ce ne sono,
    si usa la più recente e la si marca come non corrente. Senza target_commit
    tutte le evidenze valgono come correnti.
    """
    best: dict[str, tuple[tuple, TestResult]] = {}
    for evidence in evidence_list:
        commit = evidence.get("commit")
        current = target_commit is None or (commit is not None and commit == target_commit
                                            and not evidence.get("dirty"))
        for test, result in results_by_test(evidence).items():
            if not accept(evidence, test):
                continue
            candidate = TestResult(test=test, result=result, commit=commit,
                                   created=evidence.get("created", ""), origin=evidence.get("origin", ""),
                                   url=(evidence.get("ci") or {}).get("url"),
                                   source=evidence.get("_path", ""), current=current)
            rank = (current, candidate.created)
            if test not in best or rank > best[test][0]:
                best[test] = (rank, candidate)
    return {test: entry[1] for test, entry in best.items()}


def ci_accept(items: dict):
    """Filtro per latest_results: evidenze CI, più quelle manuali dei TEST manuali e delle VAL."""
    def accept(evidence: dict, test_id: str) -> bool:
        if evidence.get("origin") == "ci":
            return True
        item = items.get(test_id)
        manual = item is not None and (item.type == "VAL" or item.fields.get("method") == "manual")
        return evidence.get("origin") == "manual" and manual
    return accept
