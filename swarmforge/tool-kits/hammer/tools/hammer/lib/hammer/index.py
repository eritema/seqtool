"""Indice SQLite derivato dai file: si ricostruisce, non si modifica."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from . import __version__
from .items import LoadResult
from .risk import RiskPolicy, evaluate

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE items (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    path TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE fields (
    item_id TEXT NOT NULL REFERENCES items(id),
    name TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (item_id, name)
);
CREATE TABLE links (
    source TEXT NOT NULL REFERENCES items(id),
    rel TEXT NOT NULL,
    target TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    target_exists INTEGER NOT NULL,
    suspect INTEGER NOT NULL,
    PRIMARY KEY (source, rel, target)
);
CREATE INDEX links_target ON links(target);
CREATE TABLE risks (
    item_id TEXT PRIMARY KEY REFERENCES items(id),
    total_probability REAL NOT NULL,
    probability INTEGER NOT NULL,
    severity INTEGER NOT NULL,
    risk_class TEXT NOT NULL,
    acceptable INTEGER NOT NULL,
    residual_probability REAL,
    residual_category INTEGER,
    residual_severity INTEGER,
    residual_class TEXT,
    residual_acceptable INTEGER
);
CREATE TABLE evidence (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    origin TEXT NOT NULL,
    created TEXT NOT NULL,
    commit_sha TEXT,
    dirty INTEGER,
    ci_system TEXT,
    ci_run_id TEXT,
    ci_url TEXT,
    recorded_by TEXT,
    report_file TEXT,
    report_sha256 TEXT
);
CREATE TABLE results (
    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
    test TEXT,
    case_name TEXT NOT NULL,
    classname TEXT,
    result TEXT NOT NULL,
    duration REAL,
    message TEXT
);
CREATE INDEX results_test ON results(test);
"""


def _field_value(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _relative(path: str, root: Path) -> str:
    try:
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path


def _insert_evidence(db, evidence_list: list[dict], root: Path) -> None:
    """Esiti grezzi: una riga per caso e per TEST collegato (test NULL se il caso non è collegato).

    Quale esito vale per un commit lo decidono 'matrix' e 'check', non l'indice.
    """
    for number, evidence in enumerate(sorted(evidence_list, key=lambda e: (e.get("created", ""), e["_path"])), 1):
        ci, report = evidence.get("ci") or {}, evidence.get("report") or {}
        dirty = evidence.get("dirty")
        db.execute(
            "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (number, _relative(evidence["_path"], root), evidence.get("origin", ""), evidence.get("created", ""),
             evidence.get("commit"), None if dirty is None else int(dirty), ci.get("system"), ci.get("run_id"),
             ci.get("url"), evidence.get("recorded_by"), report.get("file"), report.get("sha256")),
        )
        for case in evidence.get("cases", []):
            row = (case.get("name", ""), case.get("classname"), case.get("result", "error"),
                   case.get("duration"), case.get("message"))
            db.executemany("INSERT INTO results VALUES (?, ?, ?, ?, ?, ?, ?)",
                           [(number, test, *row) for test in case.get("tests") or [None]])


def build_index(loaded: LoadResult, root: Path, destination: Path,
                risk_policy: RiskPolicy | None = None, evidence: list[dict] | None = None,
                head: tuple[str | None, bool] | None = None) -> Path:
    """Scrive l'indice in un file temporaneo e lo sostituisce atomicamente."""
    by_id = loaded.by_id()
    handle, temporary = tempfile.mkstemp(prefix=".cq-index-", suffix=".tmp", dir=destination.parent)
    os.close(handle)
    try:
        with closing(sqlite3.connect(temporary)) as db, db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO meta VALUES ('hammer_version', ?)", (__version__,))
            if head and head[0]:
                db.execute("INSERT INTO meta VALUES ('head_commit', ?)", (head[0],))
                db.execute("INSERT INTO meta VALUES ('head_dirty', ?)", (str(int(head[1])),))
            _insert_evidence(db, evidence or [], root)
            for item in loaded.items:
                if item.id in by_id and by_id[item.id] is not item:
                    continue  # ID duplicato: lo segnala 'hammer check'
                db.execute(
                    "INSERT INTO items VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (item.id, item.type, item.title, item.status,
                     item.path.relative_to(root).as_posix() if item.path else "",
                     item.fingerprint(), item.body),
                )
                db.executemany(
                    "INSERT INTO fields VALUES (?, ?, ?)",
                    [(item.id, name, _field_value(value)) for name, value in item.fields.items()],
                )
                rows = {}
                for link in item.links:
                    target = by_id.get(link.to)
                    suspect = target is not None and link.fingerprint != target.fingerprint()
                    rows[(link.rel, link.to)] = (item.id, link.rel, link.to, link.fingerprint,
                                                 int(target is not None), int(suspect))
                db.executemany("INSERT INTO links VALUES (?, ?, ?, ?, ?, ?)", rows.values())
                evaluation = evaluate(risk_policy, item.fields) if risk_policy and item.type == "RISK" else None
                if evaluation:
                    initial, residual = evaluation.initial, evaluation.residual
                    db.execute(
                        "INSERT INTO risks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (item.id, initial.total_probability, initial.category, initial.severity,
                         initial.risk_class, int(initial.acceptable),
                         *((residual.total_probability, residual.category, residual.severity,
                            residual.risk_class, int(residual.acceptable)) if residual else (None,) * 5)),
                    )
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination


def query_index(path: Path, sql: str) -> tuple[list[str], list[tuple]]:
    """Esegue SQL in sola lettura sull'indice."""
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only = ON")
        cursor = db.execute(sql)
        columns = [description[0] for description in cursor.description or []]
        return columns, cursor.fetchall()
