"""Configurazione del progetto: tipi, campi, stati e relazioni ammesse."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from .errors import HammerError
from .risk import RiskPolicy, parse_risk_policy

CONFIG_FILE = "hammer.toml"
CQ_DIR = "cq"


FIELD_KINDS = ("text", "number", "integer")


@dataclass(frozen=True)
class FieldSpec:
    name: str
    required: bool = False
    values: tuple[str, ...] | None = None
    is_list: bool = False
    kind: str = "text"
    minimum: float | None = None
    maximum: float | None = None

    def convert(self, raw: str):
        """Converte un valore scritto da riga di comando nel tipo del campo."""
        if self.kind == "text":
            return raw
        try:
            return int(raw) if self.kind == "integer" else float(raw)
        except ValueError:
            raise HammerError(f"{self.name}: atteso un valore {self.kind}, trovato {raw!r}") from None

    def problem(self, value) -> str | None:
        """Descrive perché un singolo valore non è valido, oppure None."""
        if self.kind in ("number", "integer"):
            numeric = (int,) if self.kind == "integer" else (int, float)
            if isinstance(value, bool) or not isinstance(value, numeric):
                return f"{self.name}={value!r}: atteso un valore {self.kind}"
            if self.minimum is not None and value < self.minimum:
                return f"{self.name}={value}: minimo {self.minimum}"
            if self.maximum is not None and value > self.maximum:
                return f"{self.name}={value}: massimo {self.maximum}"
        if self.values is not None and str(value) not in self.values:
            return f"{self.name}={value!r} non ammesso ({', '.join(self.values)})"
        return None


@dataclass(frozen=True)
class TypeSpec:
    prefix: str
    name: str
    fields: dict[str, FieldSpec]


@dataclass(frozen=True)
class CoverageRule:
    """Un elemento con i campi 'when' deve ricevere 'rel' da un elemento non obsoleto
    dei tipi 'sources' con i campi 'where'."""
    rel: str
    sources: tuple[str, ...] = ()          # vuoto: qualunque tipo
    where: tuple[tuple[str, str], ...] = ()
    when: tuple[tuple[str, str], ...] = ()

    def applies_to(self, item) -> bool:
        return _fields_match(item.fields, self.when)

    def accepts(self, rel: str, source) -> bool:
        if rel != self.rel or (self.sources and source.type not in self.sources):
            return False
        return _fields_match(source.fields, self.where)

    def describe(self) -> str:
        text = self.rel
        if self.sources:
            text += " da " + " o ".join(self.sources)
        if self.where:
            text += " (" + ", ".join(f"{name}={value}" for name, value in self.where) + ")"
        return text


def _fields_match(fields: dict, conditions: tuple[tuple[str, str], ...]) -> bool:
    for name, expected in conditions:
        value = fields.get(name)
        values = value if isinstance(value, list) else [value]
        if expected not in [str(v) for v in values if v is not None]:
            return False
    return True


@dataclass(frozen=True)
class Config:
    project_name: str
    safety_class: str
    statuses: tuple[str, ...]
    types: dict[str, TypeSpec]
    relations: dict[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)
    risk: RiskPolicy | None = None
    coverage: dict[str, tuple[CoverageRule, ...]] = field(default_factory=dict)

    def relation_allowed(self, rel: str, source_type: str, target_type: str) -> bool:
        return any(
            src in (source_type, "*") and dst in (target_type, "*")
            for src, dst in self.relations.get(rel, ())
        )


# Regole di copertura predefinite per classe di sicurezza. Per le classi B e C
# IEC 62304 richiede architettura (§5.3), suddivisione in unità (§5.4.1) e
# verifica delle unità (§5.5).
_COVERAGE_A = """\
URS = ["validates"]   # ogni requisito degli stakeholder è validato da una VAL
SRS = ["verifies"]    # ogni requisito software è verificato da un TEST"""

_COVERAGE_BC = """\
URS = ["validates"]                                               # validato da una VAL
SRS = ["verifies", { rel = "derives_from", from = "ARCH" }]       # verificato da un TEST e realizzato da un ARCH
ARCH = [{ rel = "derives_from", from = ["ARCH", "UNIT"], when = { kind = "component" } }]   # ogni componente è scomposto in ARCH più fini o in UNIT
UNIT = [{ rel = "verifies", from = "TEST", where = { level = "unit" } }]   # verificata da un test di unità
SOUP = ["uses"]                                                   # usato da un ARCH o da una UNIT"""


def default_config_text(name: str, safety_class: str) -> str:
    template = resources.files("hammer").joinpath("default_config.toml").read_text("utf-8")
    coverage = _COVERAGE_A if safety_class == "A" else _COVERAGE_BC
    return (template.replace("{name}", name).replace("{safety_class}", safety_class)
            .replace("{coverage}", coverage))


def parse_config(text: str, source: str = CONFIG_FILE) -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise HammerError(f"{source}: TOML non valido: {error}") from error

    project = data.get("project", {})
    types: dict[str, TypeSpec] = {}
    for prefix, spec in data.get("types", {}).items():
        if not prefix.isupper() or not prefix.isalnum():
            raise HammerError(f"{source}: prefisso di tipo non valido: {prefix}")
        fields = {}
        for name, options in spec.get("fields", {}).items():
            kind = options.get("kind", "text")
            if kind not in FIELD_KINDS:
                raise HammerError(f"{source}: {prefix}.{name}: kind {kind!r} non valido ({', '.join(FIELD_KINDS)})")
            fields[name] = FieldSpec(
                name=name,
                required=bool(options.get("required", False)),
                values=tuple(options["values"]) if "values" in options else None,
                is_list=bool(options.get("list", False)),
                kind=kind,
                minimum=options.get("min"),
                maximum=options.get("max"),
            )
        types[prefix] = TypeSpec(prefix=prefix, name=spec.get("name", prefix), fields=fields)

    relations: dict[str, tuple[tuple[str, str], ...]] = {}
    for rel, spec in data.get("relations", {}).items():
        pairs = []
        for pair in spec.get("allowed", []):
            src, sep, dst = pair.partition("->")
            if not sep:
                raise HammerError(f"{source}: relazione {rel}: coppia non valida {pair!r}")
            pairs.append((src.strip(), dst.strip()))
        relations[rel] = tuple(pairs)

    return Config(
        project_name=project.get("name", ""),
        safety_class=str(project.get("safety_class", "")),
        statuses=tuple(project.get("statuses", ["draft", "review", "approved", "obsolete"])),
        types=types,
        relations=relations,
        risk=parse_risk_policy(data["risk"], source) if "risk" in data else None,
        coverage=_parse_coverage(data.get("coverage", {}), types, relations, source),
    )


def _parse_coverage(data: dict, types: dict, relations: dict, source: str) -> dict[str, tuple[CoverageRule, ...]]:
    coverage = {}
    for type_name, entries in data.items():
        where = f"{source}: [coverage] {type_name}"
        if type_name not in types:
            raise HammerError(f"{source}: [coverage] tipo sconosciuto {type_name}")
        rules = []
        for entry in entries:
            if isinstance(entry, str):
                entry = {"rel": entry}
            if not isinstance(entry, dict) or "rel" not in entry:
                raise HammerError(f"{where}: regola non valida {entry!r}")
            unknown_keys = set(entry) - {"rel", "from", "where", "when"}
            if unknown_keys:
                raise HammerError(f"{where}: chiavi sconosciute {', '.join(sorted(unknown_keys))}")
            if entry["rel"] not in relations:
                raise HammerError(f"{where}: relazione sconosciuta {entry['rel']}")
            sources = entry.get("from", [])
            sources = (sources,) if isinstance(sources, str) else tuple(sources)
            unknown_types = [t for t in sources if t not in types]
            if unknown_types:
                raise HammerError(f"{where}: tipi sconosciuti {', '.join(unknown_types)}")
            conditions = {key: tuple((str(k), str(v)) for k, v in entry.get(key, {}).items())
                          for key in ("where", "when")}
            rules.append(CoverageRule(rel=entry["rel"], sources=sources, **conditions))
        coverage[type_name] = tuple(rules)
    return coverage


def load_config(cq_dir: Path) -> Config:
    path = cq_dir / CONFIG_FILE
    if not path.is_file():
        raise HammerError(f"configurazione mancante: {path}")
    return parse_config(path.read_text("utf-8"), str(path))
