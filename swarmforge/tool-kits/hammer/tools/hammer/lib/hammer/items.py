"""Elementi: file Markdown con intestazione YAML, uno per elemento."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .errors import HammerError

ID_PATTERN = re.compile(r"^([A-Z][A-Z0-9]*)-(\d{4,})$")
ITEMS_DIR = "items"
FINGERPRINT_LENGTH = 12

# Campi esclusi dall'impronta: cambiarli non rende sospetti i link entranti.
_NOT_FINGERPRINTED = {"id", "type", "status", "links"}
_HEADER_ORDER = ("id", "type", "title", "status")


@dataclass
class Link:
    rel: str
    to: str
    fingerprint: str = ""

    def as_dict(self) -> dict:
        return {"rel": self.rel, "to": self.to, "fingerprint": self.fingerprint}


@dataclass
class Item:
    id: str
    type: str
    title: str
    status: str
    fields: dict = field(default_factory=dict)
    links: list[Link] = field(default_factory=list)
    body: str = ""
    path: Path | None = None

    def fingerprint(self) -> str:
        content = {
            "title": self.title,
            "fields": {k: v for k, v in self.fields.items() if k not in _NOT_FINGERPRINTED},
            "body": self.body.strip(),
        }
        canonical = json.dumps(content, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]

    def to_text(self) -> str:
        meta: dict = {"id": self.id, "type": self.type, "title": self.title, "status": self.status}
        meta.update(self.fields)
        if self.links:
            meta["links"] = [link.as_dict() for link in self.links]
        header = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000)
        body = self.body.strip("\n")
        return f"---\n{header}---\n" + (f"\n{body}\n" if body else "")


def split_id(item_id: str) -> tuple[str, int]:
    match = ID_PATTERN.match(item_id)
    if not match:
        raise HammerError(f"ID non valido: {item_id!r} (atteso TIPO-NNNN)")
    return match.group(1), int(match.group(2))


def format_id(prefix: str, number: int) -> str:
    return f"{prefix}-{number:04d}"


def item_path(cq_dir: Path, item_id: str) -> Path:
    prefix, _ = split_id(item_id)
    return cq_dir / ITEMS_DIR / prefix / f"{item_id}.md"


def parse_item(text: str, path: Path | None = None) -> Item:
    where = str(path) if path else "elemento"
    if not text.startswith("---\n"):
        raise HammerError(f"{where}: manca l'intestazione YAML iniziale '---'")
    end = text.find("\n---\n", 3)
    if end == -1:
        end = text.find("\n---", 3)
        if end == -1 or text[end + 4:].strip():
            raise HammerError(f"{where}: intestazione YAML non chiusa da '---'")
    header, body = text[4:end], text[end + 5:]
    try:
        meta = yaml.safe_load(header) or {}
    except yaml.YAMLError as error:
        raise HammerError(f"{where}: YAML non valido: {error}") from error
    if not isinstance(meta, dict):
        raise HammerError(f"{where}: l'intestazione deve essere una mappa YAML")

    links = []
    for raw in meta.pop("links", None) or []:
        if not isinstance(raw, dict) or "rel" not in raw or "to" not in raw:
            raise HammerError(f"{where}: link non valido: {raw!r}")
        links.append(Link(rel=str(raw["rel"]), to=str(raw["to"]), fingerprint=str(raw.get("fingerprint", ""))))

    missing = [key for key in _HEADER_ORDER if key not in meta]
    if missing:
        raise HammerError(f"{where}: campi obbligatori mancanti: {', '.join(missing)}")
    return Item(
        id=str(meta.pop("id")),
        type=str(meta.pop("type")),
        title=str(meta.pop("title")),
        status=str(meta.pop("status")),
        fields=meta,
        links=links,
        body=body.lstrip("\n"),
        path=path,
    )


@dataclass
class LoadResult:
    items: list[Item]
    errors: list[tuple[Path, str]]

    def by_id(self) -> dict[str, Item]:
        return {item.id: item for item in self.items}


def load_items(cq_dir: Path) -> LoadResult:
    items, errors = [], []
    root = cq_dir / ITEMS_DIR
    if root.is_dir():
        for path in sorted(root.glob("*/*.md")):
            try:
                items.append(parse_item(path.read_text("utf-8"), path))
            except HammerError as error:
                errors.append((path, str(error)))
    return LoadResult(items=items, errors=errors)


def next_id(cq_dir: Path, prefix: str) -> str:
    """Prossimo numero libero: gli ID non si riusano, nemmeno quelli obsoleti."""
    highest = 0
    folder = cq_dir / ITEMS_DIR / prefix
    if folder.is_dir():
        for path in folder.glob("*.md"):
            match = ID_PATTERN.match(path.stem)
            if match and match.group(1) == prefix:
                highest = max(highest, int(match.group(2)))
    return format_id(prefix, highest + 1)


def write_item(item: Item) -> None:
    if item.path is None:
        raise HammerError(f"{item.id}: percorso del file sconosciuto")
    item.path.parent.mkdir(parents=True, exist_ok=True)
    item.path.write_text(item.to_text(), "utf-8")
