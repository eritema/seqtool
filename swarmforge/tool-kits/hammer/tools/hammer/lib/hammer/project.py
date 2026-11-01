"""Individuazione del progetto: la cartella che contiene cq/hammer.toml."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from .config import CONFIG_FILE, CQ_DIR, Config, load_config
from .errors import HammerError
from .items import LoadResult, load_items

INDEX_FILE = ".cq-index.sqlite"
LOCAL_EVIDENCE_DIR = ".cq-evidence"


@dataclass
class Project:
    root: Path

    @property
    def cq_dir(self) -> Path:
        return self.root / CQ_DIR

    @property
    def index_path(self) -> Path:
        return self.root / INDEX_FILE

    @cached_property
    def config(self) -> Config:
        return load_config(self.cq_dir)

    @property
    def local_evidence_dir(self) -> Path:
        return self.root / LOCAL_EVIDENCE_DIR

    @property
    def committed_evidence_dir(self) -> Path:
        return self.cq_dir / "evidence"

    def load(self) -> LoadResult:
        return load_items(self.cq_dir)

    def git_head(self) -> tuple[str | None, bool | None]:
        """Commit corrente e presenza di modifiche non committate, oppure (None, None) fuori da Git."""
        try:
            head = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, check=True).stdout.strip()
            status = subprocess.run(["git", "-C", str(self.root), "status", "--porcelain"],
                                    capture_output=True, text=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            return None, None
        return head, bool(status.strip())


def ensure_gitignored(root: Path, entries: list[str]) -> None:
    gitignore = root / ".gitignore"
    lines = gitignore.read_text("utf-8").splitlines() if gitignore.exists() else []
    missing = [entry for entry in entries if entry not in lines]
    if not missing:
        return
    with gitignore.open("a", encoding="utf-8") as handle:
        if lines and lines[-1] != "":
            handle.write("\n")
        handle.write("".join(f"{entry}\n" for entry in missing))


def find_project(start: Path) -> Project:
    start = start.resolve()
    for folder in (start, *start.parents):
        if (folder / CQ_DIR / CONFIG_FILE).is_file():
            return Project(folder)
    raise HammerError(f"nessun progetto hammer trovato da {start} in su (manca {CQ_DIR}/{CONFIG_FILE}; usare 'hammer init')")
