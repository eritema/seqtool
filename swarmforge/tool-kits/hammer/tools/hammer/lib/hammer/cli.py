"""Interfaccia a riga di comando di hammer.

Codici di uscita: 0 nessun problema, 1 problemi trovati, 2 errore d'uso o di dati.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from . import __version__
from . import baseline as baselines
from .checks import ERROR, WARNING, check_project, check_release
from .ci import detect as detect_ci, download_github_artifacts
from .config import CONFIG_FILE, CQ_DIR, default_config_text, parse_config
from .errors import HammerError
from .evidence import (PARSERS, Case, build_evidence, ci_accept, evidence_filename, latest_results,
                       load_evidence, report_info, results_by_test, write_evidence)
from .index import build_index, query_index
from .matrix import build_matrix, summarize, to_csv, to_markdown
from .items import ITEMS_DIR, Item, Link, format_id, item_path, next_id, split_id, write_item
from .project import INDEX_FILE, LOCAL_EVIDENCE_DIR, Project, ensure_gitignored, find_project
from .risk import evaluate
from .soup import FORMATS as LOCK_FORMATS, ecosystem, read_lock
from . import audit, export, kit

OK, PROBLEMS, FAILURE = 0, 1, 2
EVIDENCE_TYPES = ("TEST", "VAL")


def _print_json(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _print_table(columns: list[str], rows: list) -> None:
    cells = [[("" if value is None else str(value)) for value in row] for row in rows]
    widths = [max([len(c)] + [len(row[i]) for row in cells]) for i, c in enumerate(columns)]
    print("  ".join(c.ljust(w) for c, w in zip(columns, widths)).rstrip())
    for row in cells:
        print("  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip())


def _project(args) -> Project:
    return find_project(Path(args.root) if args.root else Path.cwd())


def _item_summary(item: Item) -> dict:
    return {"id": item.id, "type": item.type, "title": item.title, "status": item.status}


def _risk_evaluation(project: Project, item: Item) -> dict | None:
    policy = project.config.risk
    if item.type != "RISK" or policy is None:
        return None
    evaluation = evaluate(policy, item.fields)
    return evaluation.as_dict(policy) if evaluation else None


def _require(items: dict[str, Item], item_id: str) -> Item:
    if item_id not in items:
        raise HammerError(f"elemento inesistente: {item_id}")
    return items[item_id]


# ------------------------------------------------------------------ comandi

def cmd_init(args) -> int:
    root = Path(args.root or Path.cwd()).resolve()
    cq_dir = root / CQ_DIR
    config_path = cq_dir / CONFIG_FILE
    if config_path.exists():
        raise HammerError(f"progetto già inizializzato: {config_path}")
    text = default_config_text(args.name or root.name, args.safety_class)
    parse_config(text)  # la configurazione generata deve essere valida
    (cq_dir / ITEMS_DIR).mkdir(parents=True, exist_ok=True)
    config_path.write_text(text, "utf-8")

    ensure_gitignored(root, [INDEX_FILE, f"{LOCAL_EVIDENCE_DIR}/"])

    if args.json:
        _print_json({"root": str(root), "config": str(config_path)})
    else:
        print(f"Progetto inizializzato: {config_path}")
    return OK


def cmd_new(args) -> int:
    project = _project(args)
    config = project.config
    prefix = args.type.upper()
    spec = config.types.get(prefix)
    if spec is None:
        raise HammerError(f"tipo sconosciuto: {args.type} (definiti: {', '.join(config.types)})")

    fields: dict = {}
    for assignment in args.field or []:
        name, sep, value = assignment.partition("=")
        if not sep or not name:
            raise HammerError(f"--field atteso nel formato nome=valore: {assignment!r}")
        field_spec = spec.fields.get(name)
        if field_spec is None:
            raise HammerError(f"campo {name!r} non definito per {prefix} ({', '.join(spec.fields) or 'nessuno'})")
        converted = field_spec.convert(value)
        problem = field_spec.problem(converted)
        if problem:
            raise HammerError(problem)
        if field_spec.is_list:
            fields.setdefault(name, []).append(converted)
        else:
            fields[name] = converted
    missing = [name for name, f in spec.fields.items() if f.required and name not in fields]
    if missing:
        raise HammerError(f"campi obbligatori per {prefix}: {', '.join(missing)} (usare --field nome=valore)")
    if args.status not in config.statuses:
        raise HammerError(f"stato {args.status!r} non ammesso ({', '.join(config.statuses)})")

    body = ""
    if args.body_file:
        body = Path(args.body_file).read_text("utf-8")
    elif args.body is not None:
        body = args.body

    item_id = next_id(project.cq_dir, prefix)
    item = Item(id=item_id, type=prefix, title=args.title, status=args.status,
                fields=fields, body=body, path=item_path(project.cq_dir, item_id))
    write_item(item)
    if args.json:
        _print_json({**_item_summary(item), "path": str(item.path.relative_to(project.root))})
    else:
        print(f"{item_id} creato: {item.path.relative_to(project.root)}")
    return OK


def cmd_show(args) -> int:
    project = _project(args)
    items = project.load().by_id()
    item = _require(items, args.id)
    outgoing = []
    for link in item.links:
        target = items.get(link.to)
        outgoing.append({
            "rel": link.rel, "to": link.to,
            "title": target.title if target else None,
            "exists": target is not None,
            "suspect": target is not None and link.fingerprint != target.fingerprint(),
        })
    incoming = []
    for other in items.values():
        for link in other.links:
            if link.to == item.id:
                incoming.append({"from": other.id, "rel": link.rel, "title": other.title,
                                 "suspect": link.fingerprint != item.fingerprint()})
    incoming.sort(key=lambda entry: (entry["rel"], entry["from"]))
    risk = _risk_evaluation(project, item)

    if args.json:
        _print_json({**_item_summary(item), "fields": item.fields, "fingerprint": item.fingerprint(),
                     "path": str(item.path.relative_to(project.root)), "body": item.body,
                     "links": outgoing, "linked_from": incoming, "risk_evaluation": risk})
        return OK

    print(f"{item.id}  {item.title}")
    print(f"tipo: {item.type}   stato: {item.status}   impronta: {item.fingerprint()}")
    for name, value in item.fields.items():
        print(f"{name}: {value}")
    if risk:
        for label, entry in (("rischio iniziale", risk["initial"]), ("rischio residuo", risk["residual"])):
            if entry:
                verdict = "accettabile" if entry["acceptable"] else "NON accettabile"
                print(f"{label}: Pt={entry['total_probability']:.3g}  {entry['risk_class']}  "
                      f"({entry['probability']}; {entry['severity']})  {verdict}")
    if item.body.strip():
        print()
        print(item.body.rstrip())
    if outgoing:
        print("\nCollegamenti:")
        for entry in outgoing:
            note = " [SOSPETTO]" if entry["suspect"] else ("" if entry["exists"] else " [INESISTENTE]")
            print(f"  {entry['rel']} {entry['to']}  {entry['title'] or ''}{note}")
    if incoming:
        print("\nCollegato da:")
        for entry in incoming:
            note = " [SOSPETTO]" if entry["suspect"] else ""
            print(f"  {entry['from']} {entry['rel']}  {entry['title']}{note}")
    return OK


def cmd_link(args) -> int:
    project = _project(args)
    config = project.config
    items = project.load().by_id()
    source, target = _require(items, args.source), _require(items, args.target)
    if source.id == target.id:
        raise HammerError("un elemento non può essere collegato a se stesso")
    if args.rel not in config.relations:
        raise HammerError(f"relazione sconosciuta: {args.rel} (definite: {', '.join(config.relations)})")
    if not config.relation_allowed(args.rel, source.type, target.type):
        raise HammerError(f"{source.type} {args.rel} {target.type} non ammesso dalla configurazione")
    if any(link.rel == args.rel and link.to == target.id for link in source.links):
        raise HammerError(f"collegamento già presente: {source.id} {args.rel} {target.id}")
    source.links.append(Link(rel=args.rel, to=target.id, fingerprint=target.fingerprint()))
    write_item(source)
    if args.json:
        _print_json({"source": source.id, "rel": args.rel, "target": target.id})
    else:
        print(f"{source.id} {args.rel} {target.id}")
    return OK


def cmd_unlink(args) -> int:
    project = _project(args)
    source = _require(project.load().by_id(), args.source)
    kept = [link for link in source.links if not (link.rel == args.rel and link.to == args.target)]
    if len(kept) == len(source.links):
        raise HammerError(f"collegamento non trovato: {args.source} {args.rel} {args.target}")
    source.links = kept
    write_item(source)
    if not args.json:
        print(f"rimosso: {args.source} {args.rel} {args.target}")
    else:
        _print_json({"source": args.source, "rel": args.rel, "target": args.target})
    return OK


def cmd_review(args) -> int:
    project = _project(args)
    items = project.load().by_id()
    target = _require(items, args.id)
    current = target.fingerprint()
    confirmed = []
    for other in items.values():
        changed = False
        for link in other.links:
            if link.to == target.id and link.fingerprint != current:
                link.fingerprint = current
                confirmed.append({"from": other.id, "rel": link.rel})
                changed = True
        if changed:
            write_item(other)
    if args.json:
        _print_json({"target": target.id, "confirmed": confirmed})
    elif confirmed:
        for entry in confirmed:
            print(f"confermato: {entry['from']} {entry['rel']} {target.id}")
    else:
        print(f"nessun collegamento sospetto verso {target.id}")
    return OK


def cmd_list(args) -> int:
    project = _project(args)
    items = project.load().items
    if args.type:
        items = [item for item in items if item.type == args.type.upper()]
    if args.status:
        items = [item for item in items if item.status == args.status]
    items.sort(key=lambda item: item.id)
    if args.json:
        _print_json([_item_summary(item) for item in items])
    elif items:
        _print_table(["ID", "STATO", "TITOLO"], [(i.id, i.status, i.title) for i in items])
    return OK


def _trace(items: dict[str, Item], start: str, direction: str, depth: int | None) -> dict:
    incoming: dict[str, list[tuple[str, str]]] = {}
    for item in items.values():
        for link in item.links:
            incoming.setdefault(link.to, []).append((link.rel, item.id))

    def neighbours(item_id: str) -> list[tuple[str, str]]:
        if direction == "up":
            item = items.get(item_id)
            return sorted((link.rel, link.to) for link in item.links) if item else []
        return sorted(incoming.get(item_id, []))

    def node(item_id: str, rel: str | None, level: int, path: frozenset) -> dict:
        item = items.get(item_id)
        entry = {"id": item_id, "rel": rel, "title": item.title if item else None,
                 "status": item.status if item else None, "children": []}
        if item_id in path:
            entry["cycle"] = True
            return entry
        if depth is None or level < depth:
            entry["children"] = [node(child, child_rel, level + 1, path | {item_id})
                                 for child_rel, child in neighbours(item_id)]
        return entry

    return node(start, None, 0, frozenset())


def _print_tree(entry: dict, indent: str = "") -> None:
    label = f"{entry['rel']} " if entry["rel"] else ""
    title = entry["title"] if entry["title"] is not None else "[INESISTENTE]"
    cycle = " [CICLO]" if entry.get("cycle") else ""
    print(f"{indent}{label}{entry['id']}  {title}{cycle}")
    for child in entry["children"]:
        _print_tree(child, indent + "  ")


def cmd_trace(args) -> int:
    project = _project(args)
    items = project.load().by_id()
    _require(items, args.id)
    tree = _trace(items, args.id, "up" if args.up else "down", args.depth)
    if args.json:
        _print_json(tree)
    else:
        _print_tree(tree)
    return OK


def _build_index(project: Project, args) -> Path:
    evidence_list, errors = _load_project_evidence(project, args)
    _warn_evidence_errors(errors)
    return build_index(project.load(), project.root, project.index_path, project.config.risk,
                       evidence=evidence_list, head=project.git_head())


def cmd_index(args) -> int:
    project = _project(args)
    path = _build_index(project, args)
    if args.json:
        _print_json({"index": str(path)})
    else:
        print(f"Indice aggiornato: {path}")
    return OK


def cmd_query(args) -> int:
    project = _project(args)
    _build_index(project, args)
    try:
        columns, rows = query_index(project.index_path, args.sql)
    except sqlite3.Error as error:
        raise HammerError(f"SQL: {error}") from error
    if args.json:
        _print_json([dict(zip(columns, row)) for row in rows])
    elif columns:
        _print_table(columns, rows)
    return OK


def cmd_check(args) -> int:
    project = _project(args)
    loaded = project.load()
    if args.release:
        args.ci_only = True
    results, _, evidence_errors = _evidence_results(project, loaded.by_id(), args)
    _warn_evidence_errors(evidence_errors)
    findings = check_project(project.config, loaded, project.cq_dir, results)
    if args.release:
        findings += check_release(loaded.by_id(), results)
    findings.sort(key=lambda f: (f.severity != ERROR, f.item, f.code))
    errors = sum(f.severity == ERROR for f in findings)
    warnings = sum(f.severity == WARNING for f in findings)
    failed = errors > 0 or (args.strict and warnings > 0)
    if args.json:
        _print_json({"ok": not failed, "errors": errors, "warnings": warnings,
                     "findings": [f.as_dict() for f in findings]})
    else:
        for f in findings:
            print(f"{f.severity.upper():7} {f.item}  {f.code}: {f.message}")
        print(f"{errors} errori, {warnings} avvisi")
    return PROBLEMS if failed else OK


def _load_project_evidence(project: Project, args) -> tuple[list[dict], list]:
    """Evidenze del progetto più quelle indicate con --evidence."""
    sources = [project.committed_evidence_dir, project.local_evidence_dir]
    sources += [Path(path) for path in (getattr(args, "evidence", None) or [])]
    return load_evidence(sources)


def _warn_evidence_errors(errors: list) -> None:
    for path, message in errors:
        print(f"hammer: evidenza ignorata {path}: {message}", file=sys.stderr)


def _evidence_results(project: Project, items: dict[str, Item], args) -> tuple[dict, str | None, list]:
    """Esiti più recenti dei TEST/VAL dalle evidenze del progetto e da quelle indicate con --evidence."""
    evidence_list, errors = _load_project_evidence(project, args)
    target = getattr(args, "commit", None) or project.git_head()[0]
    accept = ci_accept(items) if getattr(args, "ci_only", False) else (lambda evidence, test: True)
    return latest_results(evidence_list, target, accept), target, errors


def _evidence_targets(project: Project) -> set[str]:
    return {item.id for item in project.load().items if item.type in EVIDENCE_TYPES}


def _default_evidence_path(project: Project, evidence: dict) -> Path:
    ensure_gitignored(project.root, [f"{LOCAL_EVIDENCE_DIR}/"])
    return project.local_evidence_dir / evidence_filename(evidence)


def cmd_evidence_import(args) -> int:
    project = _project(args)
    report = Path(args.report)
    if not report.is_file():
        raise HammerError(f"report non trovato: {report}")
    cases = PARSERS[args.format](report.read_text("utf-8", errors="replace"))

    if args.ci:
        ci = detect_ci(os.environ)
        if ci is None:
            raise HammerError("--ci: nessun sistema CI riconosciuto nell'ambiente (supportato: GitHub Actions)")
        if args.commit:
            raise HammerError("--commit non si usa con --ci: il commit è quello dell'esecuzione CI")
        origin, commit, dirty = "ci", ci["commit"], None
    else:
        ci = None
        origin = "local"
        commit, dirty = (args.commit, None) if args.commit else project.git_head()

    evidence = build_evidence(cases, _evidence_targets(project), origin=origin, commit=commit, dirty=dirty,
                              ci=ci, report=report_info(report, args.format))
    path = write_evidence(evidence, Path(args.out) if args.out else _default_evidence_path(project, evidence))
    summary = evidence["summary"]
    unlinked = [c["name"] for c in evidence["cases"] if not c["tests"]]

    if args.json:
        _print_json({"evidence": str(path), "origin": origin, "commit": commit, "summary": summary,
                     "tests": results_by_test(evidence), "unlinked_cases": unlinked,
                     "unknown_markers": evidence["unknown_markers"]})
    else:
        print(f"Evidenza {origin} scritta: {path}")
        print(f"commit: {commit or 'sconosciuto'}{' (con modifiche non committate)' if dirty else ''}")
        print("casi: " + ", ".join(f"{k} {summary[k]}" for k in ("total", "pass", "fail", "error", "skip")))
        for test, result in sorted(results_by_test(evidence).items()):
            print(f"  {test}: {result}")
        if unlinked:
            print(f"{len(unlinked)} casi senza marcatore [TEST-NNNN]:")
            for name in unlinked[:20]:
                print(f"  {name}")
            if len(unlinked) > 20:
                print(f"  … e altri {len(unlinked) - 20}")
    for marker in evidence["unknown_markers"]:
        print(f"hammer: marcatore [{marker}] non corrisponde a un TEST o VAL esistente", file=sys.stderr)
    return PROBLEMS if evidence["unknown_markers"] else OK


def cmd_evidence_record(args) -> int:
    project = _project(args)
    item = _require(project.load().by_id(), args.id)
    if item.type not in EVIDENCE_TYPES:
        raise HammerError(f"{item.id}: le evidenze si registrano solo per {', '.join(EVIDENCE_TYPES)}")
    if not args.by.strip():
        raise HammerError("--by: indicare chi ha eseguito la verifica")

    attachments = []
    store = project.local_evidence_dir / "attachments"
    for name in args.attach or []:
        source = Path(name)
        if not source.is_file():
            raise HammerError(f"allegato non trovato: {source}")
        data = source.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        stored = store / f"{digest[:16]}-{source.name}"
        store.mkdir(parents=True, exist_ok=True)
        stored.write_bytes(data)
        attachments.append({"file": source.name, "sha256": digest,
                            "stored": stored.relative_to(project.root).as_posix()})

    commit, dirty = (args.commit, None) if args.commit else project.git_head()
    case = Case(name=f"{item.title} [{item.id}]", result=args.result, message=args.note)
    evidence = build_evidence([case], {item.id}, origin="manual", commit=commit, dirty=dirty, ci=None,
                              report=None, recorded_by=args.by, note=args.note, attachments=attachments)
    path = write_evidence(evidence, Path(args.out) if args.out else _default_evidence_path(project, evidence))
    if args.json:
        _print_json({"evidence": str(path), "test": item.id, "result": args.result, "commit": commit,
                     "attachments": attachments})
    else:
        print(f"Verifica manuale registrata: {path}")
        print(f"{item.id}: {args.result}, eseguita da {args.by}, commit {commit or 'sconosciuto'}")
    return OK


def cmd_evidence_list(args) -> int:
    project = _project(args)
    evidence_list, errors = _load_project_evidence(project, args)
    evidence_list.sort(key=lambda e: e.get("created", ""))
    rows = [{"created": e.get("created"), "origin": e.get("origin"), "commit": (e.get("commit") or "")[:7],
             "total": e["summary"]["total"], "fail": e["summary"]["fail"] + e["summary"]["error"],
             "url": (e.get("ci") or {}).get("url"), "path": e["_path"]} for e in evidence_list]
    if args.json:
        _print_json({"evidence": rows, "errors": [{"path": str(p), "message": m} for p, m in errors]})
    else:
        if rows:
            _print_table(["CREATA", "ORIGINE", "COMMIT", "CASI", "FALLITI", "FILE"],
                         [(r["created"], r["origin"], r["commit"], r["total"], r["fail"], r["path"]) for r in rows])
        for path, message in errors:
            print(f"ignorata {path}: {message}")
    return OK


def cmd_export(args) -> int:
    if args.document is None:
        for document in export.DOCUMENTS.values():
            needs = "  (solo da una baseline)" if document.needs_baseline else ""
            print(f"{document.name:20} {document.title} ({document.johner}){needs}")
        return OK
    document = export.DOCUMENTS.get(args.document)
    if document is None:
        raise HammerError(f"documento sconosciuto: {args.document} (disponibili: {', '.join(export.DOCUMENTS)})")
    if document.needs_baseline and not args.baseline:
        raise HammerError(f"{document.name} si esporta solo da una baseline (--baseline NOME)")
    project = _project(args)
    if args.baseline:
        return _export_baseline(project, document, args)

    loaded = project.load()
    items = loaded.by_id()
    args.ci_only = not args.include_local
    results, target, evidence_errors = _evidence_results(project, items, args)
    _warn_evidence_errors(evidence_errors)
    findings = check_project(project.config, loaded, project.cq_dir, results)
    errors = sum(f.severity == ERROR for f in findings)
    if errors and not args.allow_errors:
        raise HammerError(f"'hammer check' segnala {errors} errori: correggerli prima dell'export "
                          "(oppure --allow-errors)")
    evidence_list, _ = _load_project_evidence(project, args)
    head, dirty = project.git_head()
    identification = {
        "Baseline": "nessuna (stato di lavoro)",
        "Commit": f"{head}{' con modifiche non committate' if dirty else ''}" if head else "fuori da Git",
        "Esiti riferiti al commit": target or "nessuno",
        "Evidenze": "tutte, comprese quelle locali" if args.include_local
                    else "solo CI, più le verifiche manuali dei test manuali e delle VAL",
        "Controlli": f"{errors} errori, {sum(f.severity == WARNING for f in findings)} avvisi",
    }
    model = export.Model(project.config, items, results, evidence_list)
    return _write_export(document, model, project.cq_dir, identification, None, args)


def _export_baseline(project: Project, document, args) -> int:
    """Export dallo stato della baseline: elementi dal suo commit, evidenze dalla sua cartella."""
    if args.include_local or args.commit or args.evidence:
        raise HammerError("--include-local, --commit ed --evidence non si usano con --baseline")
    manifest = baselines.load_manifest(project.cq_dir, args.baseline)
    commit = manifest["commit"]
    evidence_list, errors = load_evidence([project.committed_evidence_dir / manifest["name"]])
    recorded = {entry["file"]: entry["sha256"] for entry in manifest["evidence"]}
    found = {Path(e["_path"]).resolve().relative_to(project.root.resolve()).as_posix(): e for e in evidence_list}
    altered = sorted(f for f in recorded if f not in found or hashlib.sha256(
        (project.root / f).read_bytes()).hexdigest() != recorded[f])
    if errors or altered or set(found) != set(recorded):
        raise HammerError(f"evidenze della baseline {manifest['name']} mancanti, alterate o in più: "
                          f"{', '.join(altered or sorted(set(found) ^ set(recorded)) or [str(p) for p, _ in errors])}")
    with baselines.cq_at_commit(project.root, commit) as folder:
        snapshot = Project(folder)
        items = snapshot.load().by_id()
        problems = baselines.verify_items(manifest, items)
        if problems:
            raise HammerError(f"il commit {commit[:7]} non corrisponde al manifest: {'; '.join(problems[:5])}")
        results = baselines.release_results(evidence_list, commit, items)
        recorded_findings = manifest["findings"]
        identification = {
            "Baseline": f"{manifest['name']} (creata {manifest['created']})",
            "Commit": commit,
            "Esiti riferiti al commit": commit,
            "Evidenze": f"cq/evidence/{manifest['name']}/: CI e verifiche manuali dei test manuali e delle VAL",
            "Controlli": f"{sum(f['severity'] == ERROR for f in recorded_findings)} errori, "
                         f"{sum(f['severity'] == WARNING for f in recorded_findings)} avvisi alla creazione",
        }
        model = export.Model(snapshot.config, items, results, evidence_list)
        return _write_export(document, model, snapshot.cq_dir, identification, manifest, args)


def _write_export(document, model, cq_dir: Path, identification: dict, manifest: dict | None, args) -> int:
    markdown = export.render(document, model, cq_dir, identification, manifest)
    if args.format == "md" and not args.out:
        sys.stdout.write(markdown)
        return OK
    destination = Path(args.out or f"{document.name}.{args.format}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    export.convert(markdown, args.format, destination, document.title, args.pdf_engine, document.wide)
    if args.json:
        _print_json({"document": document.name, "format": args.format, "out": str(destination),
                     "content_hash": model.content_hash()})
    else:
        print(f"{document.title}: {destination}")
    return OK


def cmd_baseline_create(args) -> int:
    project = _project(args)
    path = baselines.manifest_path(project.cq_dir, args.name)
    if path.exists():
        raise HammerError(f"la baseline {args.name} esiste già: le baseline non si modificano")
    head, _ = project.git_head()
    if head is None:
        raise HammerError("una baseline richiede un repository Git con almeno un commit")
    changes = baselines.uncommitted_changes(project.root)
    if changes:
        raise HammerError("modifiche non committate: la baseline deve corrispondere a un commit\n  "
                          + "\n  ".join(changes[:10]))
    loaded = project.load()
    items = loaded.by_id()

    with TemporaryDirectory(prefix="hammer-download-") as downloads:
        # Le verifiche manuali si registrano in locale; release_evidence scarta il resto.
        sources = [project.committed_evidence_dir, project.local_evidence_dir] + [Path(p) for p in args.evidence or []]
        if args.download:
            sources += download_github_artifacts(project.root, head, args.artifact, Path(downloads))
        evidence_list, errors = load_evidence(sources)
        _warn_evidence_errors(errors)
        selected = baselines.release_evidence(evidence_list, head, items)
        results = baselines.release_results(selected, head, items)
        findings = check_project(project.config, loaded, project.cq_dir, results) + check_release(items, results)
        findings.sort(key=lambda f: (f.severity != ERROR, f.item, f.code))
        errors_found = [f for f in findings if f.severity == ERROR]
        if errors_found and not args.allow_deviations:
            for f in errors_found:
                print(f"ERROR   {f.item}  {f.code}: {f.message}", file=sys.stderr)
            raise HammerError(f"{len(errors_found)} errori: la baseline non è stata creata "
                              "(--allow-deviations la crea registrandoli come deviazioni)")
        copied = baselines.copy_evidence(selected, project.root, project.committed_evidence_dir / args.name)

    manifest = baselines.build_manifest(args.name, export.generated_at(), head, project.config.project_name,
                                        project.config.safety_class, items, results, copied, findings)
    baselines.write_manifest(path, manifest)
    if args.json:
        _print_json({"baseline": args.name, "commit": head, "manifest": str(path), "evidence": len(copied),
                     "errors": len(errors_found), "warnings": len(findings) - len(errors_found)})
    else:
        print(f"Baseline {args.name} sul commit {head[:7]}: {len(items)} elementi, {len(copied)} evidenze, "
              f"{len(errors_found)} deviazioni, {len(findings) - len(errors_found)} avvisi")
        print(f"Da committare: git add {path.relative_to(project.root)} "
              f"{(project.committed_evidence_dir / args.name).relative_to(project.root)}")
        print(f"Tag consigliato: git tag {args.name} {head[:12]}")
    return OK


def cmd_baseline_list(args) -> int:
    project = _project(args)
    rows = [{"name": m["name"], "commit": m["commit"][:7], "created": m["created"], "items": len(m["items"]),
             "evidence": len(m["evidence"]),
             "deviations": sum(f["severity"] == ERROR for f in m["findings"])}
            for m in baselines.list_manifests(project.cq_dir)]
    if args.json:
        _print_json({"baselines": rows})
    elif rows:
        _print_table(["NOME", "COMMIT", "CREATA", "ELEMENTI", "EVIDENZE", "DEVIAZIONI"],
                     [tuple(r.values()) for r in rows])
    return OK


def cmd_baseline_diff(args) -> int:
    """Differenze fra due baseline, o fra una baseline e lo stato di lavoro."""
    project = _project(args)
    old = baselines.load_manifest(project.cq_dir, args.old)
    if args.new:
        new = baselines.load_manifest(project.cq_dir, args.new)
    else:
        new = {"items": {i.id: {"status": i.status, "fingerprint": i.fingerprint()}
                         for i in project.load().items}}
    changes = baselines.diff(old, new)
    if args.json:
        _print_json(changes)
        return OK
    for label, key in (("aggiunti", "added"), ("rimossi", "removed"), ("modificati", "changed")):
        print(f"{label}: {', '.join(changes[key]) or '-'}")
    for item_id, (before, after) in changes["status"].items():
        print(f"stato {item_id}: {before} → {after}")
    for test, (before, after) in changes.get("results", {}).items():
        print(f"esito {test}: {before or '-'} → {after or '-'}")
    return OK


def cmd_audit(args) -> int:
    """Voci per l'esito dell'audit SwarmForge, per gli elementi modificati da --base."""
    project = _project(args)
    loaded = project.load()
    items = loaded.by_id()
    results, _, evidence_errors = _evidence_results(project, items, args)
    _warn_evidence_errors(evidence_errors)
    findings = check_project(project.config, loaded, project.cq_dir, results)
    fragment = audit.audit_fragment(audit.changed_ids(project.root, args.base), items, results, findings,
                                    args.strict)
    if args.update:
        path = Path(args.update)
        try:
            current = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HammerError(f"{path}: esito di audit non leggibile: {error}") from error
        path.write_text(json.dumps(audit.merge_into(current, fragment), ensure_ascii=False, indent=2) + "\n", "utf-8")
    if args.json or not args.update:
        _print_json(fragment)
    else:
        print(f"{path}: {len(fragment['requirements'])} requisiti, {len(fragment['findings'])} rilievi")
    return PROBLEMS if fragment["findings"] else OK


def cmd_kit_build(args) -> int:
    description = kit.build_kit(Path(args.destination))
    if args.json:
        _print_json(description)
    else:
        versions = ", ".join(f"{name} {c['version']}" for name, c in description["components"].items())
        print(f"Kit SwarmForge in {args.destination}: {versions}")
        print(f"Nel progetto: swarmforge/scripts/swarm_tool.sh import {args.destination}")
    return OK


def cmd_soup_import(args) -> int:
    """Crea un SOUP per ogni dipendenza nuova e aggiorna la versione di quelle cambiate."""
    project = _project(args)
    spec = project.config.types.get("SOUP")
    if spec is None or "package" not in spec.fields:
        raise HammerError("serve il campo 'package' in [types.SOUP] di hammer.toml")
    lock = Path(args.lock)
    if not lock.is_file():
        raise HammerError(f"file di lock non trovato: {lock}")
    deps, skipped = read_lock(lock, args.format, args.include_dev)

    existing = {item.fields["package"]: item for item in project.load().items
                if item.type == "SOUP" and item.fields.get("package")}
    created, updated = [], []
    number = split_id(next_id(project.cq_dir, "SOUP"))[1]
    for dep in deps:
        item = existing.get(dep.package)
        if item is None:
            item_id = format_id("SOUP", number)
            number += 1
            item = Item(id=item_id, type="SOUP", title=dep.name, status=project.config.statuses[0],
                        fields={"package": dep.package, "version": dep.version},
                        path=item_path(project.cq_dir, item_id))
            created.append({"id": item_id, "package": dep.package, "version": dep.version})
        elif str(item.fields.get("version", "")) != dep.version:
            change = {"id": item.id, "package": dep.package, "from": item.fields.get("version"), "to": dep.version}
            item.fields["version"] = dep.version
            if item.fields.get("anomalies_evaluated") == "yes":
                item.fields["anomalies_evaluated"] = "no"   # la valutazione valeva per la versione precedente
                change["anomalies_reset"] = True
            updated.append(change)
        else:
            continue
        if not args.dry_run:
            write_item(item)

    imported = {dep.package for dep in deps}
    ecosystems = {ecosystem(dep.package) for dep in deps}
    absent = sorted(item.id for package, item in existing.items()
                    if package not in imported and ecosystem(package) in ecosystems and item.status != "obsolete")
    unchanged = len(deps) - len(created) - len(updated)
    if args.json:
        _print_json({"lock": str(lock), "dry_run": args.dry_run, "created": created, "updated": updated,
                     "unchanged": unchanged, "absent": absent, "skipped": skipped})
        return OK
    verb = "da creare" if args.dry_run else "creati"
    for entry in created:
        print(f"{entry['id']}  {entry['package']} {entry['version']}  ({'da creare' if args.dry_run else 'creato'})")
    for entry in updated:
        reset = "; anomalie da rivalutare" if entry.get("anomalies_reset") else ""
        print(f"{entry['id']}  {entry['package']} {entry['from']} → {entry['to']}{reset}")
    for item_id in absent:
        print(f"{item_id}  non più nel file di lock: valutare se renderlo obsoleto")
    for line in skipped:
        print(f"ignorata: {line}")
    print(f"{len(created)} {verb}, {len(updated)} aggiornati, {unchanged} invariati, {len(absent)} assenti")
    if created and not args.dry_run:
        print("Completare produttore, scopo, requisiti e valutazione delle anomalie dei nuovi SOUP.")
    return OK


def cmd_matrix(args) -> int:
    project = _project(args)
    items = project.load().by_id()
    row_type = args.type.upper()
    if row_type not in project.config.types:
        raise HammerError(f"tipo sconosciuto: {args.type}")
    results, target, errors = _evidence_results(project, items, args)
    for path, message in errors:
        print(f"hammer: evidenza ignorata {path}: {message}", file=sys.stderr)
    rows = build_matrix(items, results, row_type)
    header = {"project": project.config.project_name, "type": row_type, "commit": target,
              "ci_only": args.ci_only, "hammer_version": __version__}

    if args.json or args.format == "json":
        text = json.dumps({**header, "summary": summarize(rows), "rows": rows}, ensure_ascii=False, indent=2) + "\n"
    elif args.format == "csv":
        text = to_csv(rows)
    else:
        text = to_markdown(rows, header)
    if args.out:
        Path(args.out).write_text(text, "utf-8")
        print(f"Matrice scritta: {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return OK


# ------------------------------------------------------------------ parser

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", help="cartella del progetto (predefinita: ricerca da quella corrente)")
    common.add_argument("--json", action="store_true", help="output JSON")

    parser = argparse.ArgumentParser(prog="hammer", description="Continuous Qualification")
    parser.add_argument("--version", action="version", version=f"hammer {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMANDO")

    p = sub.add_parser("init", parents=[common], help="crea cq/ e hammer.toml")
    p.add_argument("--name", help="nome del progetto (predefinito: nome della cartella)")
    p.add_argument("--safety-class", default="C", choices=["A", "B", "C"])
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("new", parents=[common], help="crea un elemento")
    p.add_argument("type")
    p.add_argument("--title", required=True)
    p.add_argument("--status", default="draft")
    p.add_argument("--field", action="append", metavar="NOME=VALORE")
    p.add_argument("--body")
    p.add_argument("--body-file")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("show", parents=[common], help="mostra un elemento e i suoi collegamenti")
    p.add_argument("id")
    p.set_defaults(func=cmd_show)

    for name, func, text in (("link", cmd_link, "collega due elementi"),
                             ("unlink", cmd_unlink, "rimuove un collegamento")):
        p = sub.add_parser(name, parents=[common], help=text)
        p.add_argument("source")
        p.add_argument("rel")
        p.add_argument("target")
        p.set_defaults(func=func)

    p = sub.add_parser("review", parents=[common], help="conferma i collegamenti sospetti verso un elemento")
    p.add_argument("id")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("list", parents=[common], help="elenca gli elementi")
    p.add_argument("--type")
    p.add_argument("--status")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("trace", parents=[common], help="grafo dei collegamenti da un elemento")
    p.add_argument("id")
    direction = p.add_mutually_exclusive_group()
    direction.add_argument("--up", action="store_true", help="verso ciò da cui deriva (predefinito: verso ciò che ne dipende)")
    direction.add_argument("--down", action="store_true")
    p.add_argument("--depth", type=int)
    p.set_defaults(func=cmd_trace)

    extra_evidence = argparse.ArgumentParser(add_help=False)
    extra_evidence.add_argument("--evidence", action="append", metavar="PERCORSO",
                                help="file o cartella di evidenze in più (es. artefatti CI scaricati)")

    p = sub.add_parser("index", parents=[common, extra_evidence], help="ricostruisce l'indice SQLite")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("query", parents=[common, extra_evidence], help="SQL in sola lettura sull'indice")
    p.add_argument("sql")
    p.set_defaults(func=cmd_query)

    evidence_sources = argparse.ArgumentParser(add_help=False, parents=[extra_evidence])
    evidence_sources.add_argument("--commit", help="commit di riferimento (predefinito: HEAD)")
    evidence_sources.add_argument("--ci-only", action="store_true",
                                  help="solo evidenze CI, più quelle manuali dei test manuali e delle VAL")

    p = sub.add_parser("check", parents=[common, evidence_sources],
                       help="controlla schema, integrità, link sospetti, rischio, copertura ed esiti")
    p.add_argument("--strict", action="store_true", help="anche gli avvisi fanno fallire il controllo")
    p.add_argument("--release", action="store_true",
                   help="anche le condizioni di rilascio: esiti CI sul commit, anomalie valutate (implica --ci-only)")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("matrix", parents=[common, evidence_sources], help="matrice di tracciabilità")
    p.add_argument("--type", default="SRS", help="tipo degli elementi per riga (predefinito: SRS)")
    p.add_argument("--format", choices=["md", "csv", "json"], default="md")
    p.add_argument("--out", help="file di destinazione (predefinito: output standard)")
    p.set_defaults(func=cmd_matrix)

    evidence = sub.add_parser("evidence", help="evidenze di test").add_subparsers(
        dest="evidence_command", required=True, metavar="AZIONE")

    p = evidence.add_parser("import", parents=[common], help="importa un report di test")
    p.add_argument("report")
    p.add_argument("--format", choices=sorted(PARSERS), required=True)
    p.add_argument("--ci", action="store_true", help="esecuzione CI: identità e commit dall'ambiente")
    p.add_argument("--commit", help="commit testato, solo senza --ci (predefinito: HEAD)")
    p.add_argument("--out", help=f"file di destinazione (predefinito: {LOCAL_EVIDENCE_DIR}/…)")
    p.set_defaults(func=cmd_evidence_import)

    p = evidence.add_parser("record", parents=[common], help="registra una verifica manuale")
    p.add_argument("id")
    p.add_argument("--result", choices=["pass", "fail"], required=True)
    p.add_argument("--by", required=True, help="chi ha eseguito la verifica")
    p.add_argument("--note")
    p.add_argument("--attach", action="append", metavar="FILE")
    p.add_argument("--commit", help="commit verificato (predefinito: HEAD)")
    p.add_argument("--out")
    p.set_defaults(func=cmd_evidence_record)

    p = evidence.add_parser("list", parents=[common, extra_evidence], help="elenca le evidenze disponibili")
    p.set_defaults(func=cmd_evidence_list)

    p = sub.add_parser("export", parents=[common, extra_evidence], help="esporta un documento (senza argomenti: elenco)")
    p.add_argument("document", nargs="?")
    p.add_argument("--format", choices=export.FORMATS, default="md")
    p.add_argument("--out", help="file di destinazione (predefinito: DOCUMENTO.FORMATO; md sull'output standard)")
    p.add_argument("--commit", help="commit a cui riferire gli esiti (predefinito: HEAD)")
    p.add_argument("--include-local", action="store_true", help="usa anche le evidenze locali (solo per bozze)")
    p.add_argument("--allow-errors", action="store_true", help="esporta anche se 'hammer check' segnala errori")
    p.add_argument("--baseline", metavar="NOME", help="esporta lo stato di una baseline")
    p.add_argument("--pdf-engine", choices=export.PDF_ENGINES,
                   help="motore per --format pdf (predefinito: il primo disponibile fra quelli elencati)")
    p.set_defaults(func=cmd_export)

    baseline = sub.add_parser("baseline", help="stati congelati per il rilascio").add_subparsers(
        dest="baseline_command", required=True, metavar="AZIONE")
    p = baseline.add_parser("create", parents=[common, extra_evidence], help="crea una baseline dal commit corrente")
    p.add_argument("name")
    p.add_argument("--download", action="store_true", help="scarica con gh gli artefatti CI del commit")
    p.add_argument("--artifact", default="cq-evidence", help="nome dell'artefatto CI (predefinito: cq-evidence)")
    p.add_argument("--allow-deviations", action="store_true", help="crea la baseline anche con errori, registrandoli")
    p.set_defaults(func=cmd_baseline_create)
    p = baseline.add_parser("list", parents=[common], help="elenca le baseline")
    p.set_defaults(func=cmd_baseline_list)
    p = baseline.add_parser("diff", parents=[common], help="differenze fra due baseline (o con lo stato di lavoro)")
    p.add_argument("old")
    p.add_argument("new", nargs="?")
    p.set_defaults(func=cmd_baseline_diff)

    p = sub.add_parser("audit", parents=[common, extra_evidence],
                       help="voci per l'esito dell'audit SwarmForge degli elementi modificati")
    p.add_argument("--base", required=True, help="commit di partenza del task (es. il commit base dell'handoff)")
    p.add_argument("--commit", help="commit a cui riferire gli esiti (predefinito: HEAD)")
    p.add_argument("--strict", action="store_true", help="anche gli avvisi di check diventano findings")
    p.add_argument("--update", metavar="ESITO.json", help="aggiorna questo esito di audit invece di stampare")
    p.set_defaults(func=cmd_audit)

    kits = sub.add_parser("kit", help="kit di strumenti per SwarmForge").add_subparsers(
        dest="kit_command", required=True, metavar="AZIONE")
    p = kits.add_parser("build", parents=[common], help="crea il kit da importare con swarm_tool.sh")
    p.add_argument("destination")
    p.set_defaults(func=cmd_kit_build)

    soup = sub.add_parser("soup", help="software di terze parti").add_subparsers(
        dest="soup_command", required=True, metavar="AZIONE")

    p = soup.add_parser("import", parents=[common], help="crea o aggiorna i SOUP da un file di lock")
    p.add_argument("lock", help="requirements*.txt, uv.lock, poetry.lock, package-lock.json, renv.lock")
    p.add_argument("--format", choices=sorted(LOCK_FORMATS), help="predefinito: dal nome del file")
    p.add_argument("--include-dev", action="store_true", help="package-lock.json: anche le dipendenze di sviluppo")
    p.add_argument("--dry-run", action="store_true", help="mostra cosa cambierebbe senza scrivere")
    p.set_defaults(func=cmd_soup_import)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except HammerError as error:
        print(f"hammer: {error}", file=sys.stderr)
        return FAILURE


if __name__ == "__main__":
    sys.exit(main())
