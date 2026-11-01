"""Adattatori dei sistemi CI: identità dell'esecuzione dalle variabili d'ambiente.

Per aggiungere un sistema CI basta una funzione che, se riconosce l'ambiente,
restituisce un dizionario con almeno 'system', 'commit', 'run_id' e 'url'.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .errors import HammerError

from collections.abc import Callable, Mapping


def github_actions(env: Mapping[str, str]) -> dict | None:
    if env.get("GITHUB_ACTIONS") != "true":
        return None
    server, repository, run_id = env.get("GITHUB_SERVER_URL"), env.get("GITHUB_REPOSITORY"), env.get("GITHUB_RUN_ID")
    url = f"{server}/{repository}/actions/runs/{run_id}" if server and repository and run_id else None
    return {
        "system": "github-actions",
        "commit": env.get("GITHUB_SHA"),
        "ref": env.get("GITHUB_REF"),
        "event": env.get("GITHUB_EVENT_NAME"),
        "repository": repository,
        "workflow": env.get("GITHUB_WORKFLOW"),
        "job": env.get("GITHUB_JOB"),
        "run_id": run_id,
        "run_attempt": env.get("GITHUB_RUN_ATTEMPT"),
        "url": url,
        "environment": {"runner_os": env.get("RUNNER_OS"), "runner_arch": env.get("RUNNER_ARCH"),
                        "runner_name": env.get("RUNNER_NAME")},
    }


ADAPTERS: list[Callable[[Mapping[str, str]], dict | None]] = [github_actions]


def detect(env: Mapping[str, str]) -> dict | None:
    for adapter in ADAPTERS:
        info = adapter(env)
        if info is not None:
            return info
    return None


def download_github_artifacts(root: Path, commit: str, artifact: str, destination: Path) -> list[Path]:
    """Scarica con 'gh' l'artefatto 'artifact' di ogni esecuzione completata sul commit."""
    gh = shutil.which("gh")
    if gh is None:
        raise HammerError("gh non trovato nel PATH: serve per --download")
    listing = subprocess.run([gh, "run", "list", "--commit", commit, "--limit", "100",
                              "--json", "databaseId,status"], cwd=root, capture_output=True, text=True)
    if listing.returncode != 0:
        raise HammerError(f"gh run list: {listing.stderr.strip()}")
    folders = []
    for run in json.loads(listing.stdout or "[]"):
        if run.get("status") != "completed":
            continue
        folder = destination / str(run["databaseId"])
        download = subprocess.run([gh, "run", "download", str(run["databaseId"]), "--name", artifact,
                                   "--dir", str(folder)], cwd=root, capture_output=True, text=True)
        if download.returncode == 0:
            folders.append(folder)
        elif "no artifact" not in download.stderr.lower():
            raise HammerError(f"gh run download {run['databaseId']}: {download.stderr.strip()}")
    return folders
