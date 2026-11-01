"""Export dei documenti: elementi → modello Jinja2 → Markdown → Pandoc.

I modelli sono in 'hammer/templates'; un progetto può sostituirne uno
mettendo un file con lo stesso nome in 'cq/templates/'. Ogni documento si
apre con un blocco di identificazione: progetto, commit, data, versione di
hammer e hash degli elementi e delle evidenze effettivamente inclusi.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import jinja2

from . import __version__
from .config import Config
from .errors import HammerError
from .evidence import TestResult
from .items import Item
from .matrix import HEADERS as MATRIX_HEADERS, build_matrix, row_cells, summarize
from .risk import evaluate

TEMPLATES_DIR = "templates"
FORMATS = ("md", "docx", "pdf", "html")
PDF_ENGINES = ("typst", "xelatex", "lualatex", "pdflatex")   # in ordine di preferenza
# MacTeX aggiunge questa cartella al PATH solo nelle shell di login.
ENGINE_DIRS = ("/Library/TeX/texbin",)


@dataclass(frozen=True)
class Document:
    name: str
    title: str
    johner: str
    needs_baseline: bool = False
    wide: bool = False      # tabelle larghe: pagina orizzontale nel PDF


DOCUMENTS = {d.name: d for d in (
    Document("urs", "Requisiti degli stakeholder", "DI-TPL-02"),
    Document("srs", "Specifica dei requisiti software", "SW-TPL-03"),
    Document("architecture", "Architettura e design di dettaglio del software", "SW-TPL-04"),
    Document("risk", "Tabella di gestione del rischio (FMEA)", "RM-TPL-05", wide=True),
    Document("test-spec", "Specifica dei test di sistema", "SW-TPL-06"),
    Document("verification-report", "Rapporto di verifica del software", "SW-TPL-12", wide=True),
    Document("validation-report", "Rapporto di validazione", "SW-TPL-14"),
    Document("traceability", "Matrice di tracciabilità", "sezioni Traceability", wide=True),
    Document("changes", "Richieste di modifica", "CC-FOR-01"),
    Document("release", "Rilascio del software", "SW-FOR-05", needs_baseline=True),
    Document("full-report", "Rapporto completo di verifica e validazione", "SW-TPL-10", needs_baseline=True),
)}


class Model:
    """Vista sugli elementi non obsoleti per i modelli; registra ciò che il documento include."""

    def __init__(self, config: Config, items: dict[str, Item], results: dict[str, TestResult],
                 evidence: list[dict] | None = None):
        self.config = config
        self._evidence = {e["_path"]: e for e in evidence or []}
        self._all = items
        self.items = {i: item for i, item in items.items() if item.status != "obsolete"}
        self._results = results
        self._incoming: dict[str, list[tuple[str, Item]]] = {}
        for item in self.items.values():
            for link in item.links:
                self._incoming.setdefault(link.to, []).append((link.rel, item))
        self.used_items: set[str] = set()
        self.used_results: set[str] = set()

    def _use(self, items: list[Item]) -> list[Item]:
        self.used_items.update(item.id for item in items)
        return items

    def of_type(self, type_: str, **fields) -> list[Item]:
        """Elementi di un tipo, eventualmente con certi valori dei campi, in ordine di ID."""
        found = [item for item in self.items.values() if item.type == type_
                 and all(item.fields.get(name) == value for name, value in fields.items())]
        return self._use(sorted(found, key=lambda i: i.id))

    def links_from(self, item: Item, rel: str) -> list[Item]:
        """Elementi verso cui 'item' ha la relazione 'rel'."""
        return self._use(sorted((self.items[link.to] for link in item.links
                                 if link.rel == rel and link.to in self.items), key=lambda i: i.id))

    def links_to(self, item: Item, rel: str, type_: str | None = None) -> list[Item]:
        """Elementi che hanno la relazione 'rel' verso 'item'."""
        return self._use(sorted((source for r, source in self._incoming.get(item.id, [])
                                 if r == rel and (type_ is None or source.type == type_)), key=lambda i: i.id))

    def risk(self, item: Item):
        return evaluate(self.config.risk, item.fields) if self.config.risk else None

    def result(self, test: Item) -> TestResult | None:
        result = self._results.get(test.id)
        if result is not None:
            self.used_results.add(result.source)
        return result

    def outcomes(self, tests: list[Item]) -> list[dict]:
        """Esito di ogni TEST/VAL, anche se assente."""
        rows = []
        for test in tests:
            result = self.result(test)
            rows.append({"item": test, "result": result.result if result else None,
                         "current": result.current if result else None,
                         "origin": result.origin if result else None,
                         "commit": (result.commit or "")[:7] if result else None,
                         "url": result.url if result else None})
        return rows

    @staticmethod
    def tally(outcomes: list[dict]) -> dict:
        counts = {"total": len(outcomes), "pass": 0, "failed": 0, "skip": 0, "missing": 0, "not_current": 0}
        for row in outcomes:
            if row["result"] is None:
                counts["missing"] += 1
                continue
            counts["failed" if row["result"] in ("fail", "error") else row["result"]] += 1
            if not row["current"]:
                counts["not_current"] += 1
        return counts

    def evidence_used(self) -> list[dict]:
        """File di evidenza da cui provengono gli esiti inclusi nel documento."""
        return [self._evidence[path] for path in sorted(self.used_results) if path in self._evidence]

    def passed(self, test: Item) -> bool:
        result = self.result(test)
        return result is not None and result.current and result.result == "pass"

    def release_checklist(self) -> list[dict]:
        """Criteri di SW-FOR-05 verificabili sugli elementi, con il loro numero nel modulo."""
        def not_approved(*types):
            return [i for t in types for i in self.of_type(t) if i.status != "approved"]

        def verified_by(item, level=None):
            tests = [t for t in self.links_to(item, "verifies", "TEST")
                     if level is None or t.fields.get("level") == level]
            return tests and all(self.passed(t) for t in tests)

        risks = [r for r in self.of_type("RISK") if (e := self.risk(r)) and not e.initial.acceptable]
        controls = {c.id: c for r in self.of_type("RISK") for c in self.links_to(r, "mitigates")}
        open_problems = [p for p in self.of_type("PRB") if p.fields.get("state") in ("open", "investigating")]
        rows = [
            (1, "Requisiti degli stakeholder approvati", "IEC 82304 4.2–4.4", not_approved("URS")),
            (3, "Specifica dei requisiti software approvata", "IEC 62304 5.8.6", not_approved("SRS")),
            (4, "Ogni rischio non accettabile ha una misura di controllo nei requisiti software", "IEC 62304 5.8.6",
             [r for r in risks if not self.links_to(r, "mitigates", "SRS")]),
            (5, "Architettura e design di dettaglio approvati", "IEC 62304 5.8.6", not_approved("ARCH", "UNIT")),
            (7, "Verifica delle unità: ogni unità ha test di unità superati", "IEC 62304 5.8.1",
             [u for u in self.of_type("UNIT") if not verified_by(u, "unit")]),
            (8, "Test di integrazione superati", "IEC 62304 5.8.6",
             [t for t in self.of_type("TEST", level="integration") if not self.passed(t)]),
            (9, "Ogni requisito software verificato da test di sistema superati", "IEC 62304 5.8.6",
             [r for r in self.of_type("SRS") if not verified_by(r, "system")]),
            (10, "Ogni misura di controllo verificata da test superati", "IEC 62304 5.8.6",
             [c for c in sorted(controls.values(), key=lambda i: i.id) if not verified_by(c)]),
            (12, "Anomalie note documentate e valutate", "IEC 62304 5.8.2, 5.8.3",
             [p for p in open_problems if not p.fields.get("criticality")]),
            (15, "Ogni requisito degli stakeholder validato con esito positivo", "IEC 82304 6.1–6.3",
             [u for u in self.of_type("URS")
              if not ((vals := self.links_to(u, "validates", "VAL")) and all(self.passed(v) for v in vals))]),
        ]
        return [{"number": n, "criterion": criterion, "reference": reference, "ok": not failing,
                 "failing": failing} for n, criterion, reference, failing in rows]

    def matrix(self, row_type: str) -> tuple[list[dict], dict]:
        rows = build_matrix(self.items, self._results, row_type)
        for row in rows:
            self.used_items.add(row["id"])
            for test in row["tests"]:
                self.used_items.add(test["id"])
                if test["evidence"]:
                    self.used_results.add(self._results[test["id"]].source)
        return rows, summarize(rows)

    def content_hash(self) -> str:
        lines = [f"{i} {self._all[i].fingerprint()}" for i in sorted(self.used_items)]
        lines += [f"evidence {Path(source).name}" for source in sorted(self.used_results)]
        return hashlib.sha256("\n".join(lines).encode()).hexdigest()


# ------------------------------------------------------------------ filtri

def cell(value) -> str:
    """Testo adatto a una cella di tabella Markdown."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v) for v in value)
    return re.sub(r"\s*\n\s*", " ", str(value)).replace("|", "\\|").strip()


def ids(items: list[Item]) -> str:
    return ", ".join(item.id for item in items)


def demote(text: str, level: int) -> str:
    """Porta i titoli del testo di un elemento sotto il titolo dell'elemento (di livello 'level')."""
    lines, fence, headings = (text or "").splitlines(), False, []
    for index, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        elif not fence and (match := re.match(r"(#{1,6})\s", line)):
            headings.append((index, len(match.group(1))))
    if headings:
        shift = level + 1 - min(depth for _, depth in headings)
        for index, depth in headings:
            lines[index] = "#" * min(6, depth + shift) + lines[index][depth:]
    return "\n".join(lines).strip()


def generated_at() -> str:
    """Data di generazione; SOURCE_DATE_EPOCH la fissa per export riproducibili."""
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    moment = datetime.fromtimestamp(int(epoch), timezone.utc) if epoch else datetime.now(timezone.utc)
    return moment.strftime("%Y-%m-%d %H:%M:%SZ")


# ---------------------------------------------------------------- rendering

class _Environment(jinja2.Environment):
    """Una chiave assente in un dizionario (un campo non compilato) vale None; il resto è rigoroso."""

    def getattr(self, obj, attribute):
        if isinstance(obj, dict) and not hasattr(obj, attribute):
            return obj.get(attribute)
        return super().getattr(obj, attribute)

    def getitem(self, obj, argument):
        if isinstance(obj, dict) and isinstance(argument, str) and argument not in obj:
            return None
        return super().getitem(obj, argument)


def _environment(cq_dir: Path) -> jinja2.Environment:
    loader = jinja2.ChoiceLoader([jinja2.FileSystemLoader(str(cq_dir / TEMPLATES_DIR)),
                                  jinja2.PackageLoader("hammer", TEMPLATES_DIR)])
    env = _Environment(loader=loader, undefined=jinja2.StrictUndefined, keep_trailing_newline=True,
                       trim_blocks=True, lstrip_blocks=True)
    env.filters.update(cell=cell, ids=ids, demote=demote)
    env.globals.update(matrix_headers=MATRIX_HEADERS, matrix_cells=row_cells)
    return env


def render(document: Document, model: Model, cq_dir: Path, identification: dict,
           baseline: dict | None = None) -> str:
    """Markdown del documento: prima il corpo, poi il blocco di identificazione con l'hash di ciò che include."""
    env = _environment(cq_dir)
    template_name = f"{document.name}.md.j2"
    try:
        body = env.get_template(template_name).render(m=model, doc=document, baseline=baseline)
    except jinja2.TemplateError as error:
        raise HammerError(f"modello {template_name}: {error}") from error
    custom = (cq_dir / TEMPLATES_DIR / template_name).is_file()
    rows = {
        "Progetto": model.config.project_name,
        "Classe di sicurezza": model.config.safety_class,
        "Documento": f"{document.title} ({document.johner})",
        **identification,
        "Elementi inclusi": str(len(model.used_items)),
        "Hash del contenuto": model.content_hash(),
        "Modello": f"cq/{TEMPLATES_DIR}/{template_name}" if custom else f"hammer {template_name}",
        "Generato": f"{generated_at()} con hammer {__version__}",
    }
    header = [f"# {document.title}", "", "| | |", "|----|----------|"]   # valori larghi: l'hash non va a capo
    header += [f"| {name} | {cell(value)} |" for name, value in rows.items()]
    body = re.sub(r"\n{3,}", "\n\n", body.strip())
    return "\n".join(header) + "\n\n" + body + "\n"


def pdf_engine(requested: str | None = None) -> str:
    """Percorso del motore PDF richiesto, oppure del primo disponibile nel PATH o in ENGINE_DIRS."""
    candidates = [requested] if requested else list(PDF_ENGINES)
    for engine in candidates:
        found = shutil.which(engine) or shutil.which(engine, path=os.pathsep.join(ENGINE_DIRS))
        if found:
            return found
    raise HammerError(f"{requested or 'nessun motore PDF'} non trovato nel PATH né in {', '.join(ENGINE_DIRS)} "
                      f"(serve uno fra {', '.join(PDF_ENGINES)})")


def convert(markdown: str, fmt: str, destination: Path, title: str, engine: str | None = None,
            wide: bool = False) -> None:
    """Converte con Pandoc; il PDF passa da Typst o da LaTeX."""
    if fmt == "md":
        destination.write_text(markdown, "utf-8")
        return
    pandoc = shutil.which("pandoc")
    if pandoc is None:
        raise HammerError("pandoc non trovato nel PATH: serve per l'export in docx, pdf e html")
    command = [pandoc, "--from", "markdown", "--output", str(destination)]
    if fmt == "pdf":
        engine = pdf_engine(engine)
        command += ["--to", "pdf", "--pdf-engine", engine]
        if Path(engine).name != "typst":
            command += ["--variable", "geometry:margin=2cm"]
            if wide:
                command += ["--variable", "geometry:landscape"]
    elif fmt == "html":
        command += ["--to", "html5", "--standalone", "--metadata", f"pagetitle={title}"]
    else:
        command += ["--to", fmt]
    process = subprocess.run(command, input=markdown, text=True, capture_output=True)
    if process.returncode != 0:
        raise HammerError(f"pandoc: {process.stderr.strip() or 'conversione fallita'}")
