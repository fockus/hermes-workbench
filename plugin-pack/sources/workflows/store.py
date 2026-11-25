"""The durable run store (REQ-013): ``$HERMES_HOME/workflows/runs/<run>/``.

A run folder holds ``script.js`` (the executed source), ``request.json`` (args, budget, entry),
``journal.jsonl`` (written by runner/main.mjs, one vendored WorkflowJournalEntry per settled agent call)
and ``result.json`` (status, the script's return value, error). A run without ``result.json`` is
unfinished. Folders are 0700 and files 0600; a run whose last write is older than 14 days is removed
when the next run starts (``prune``).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

KEEP_DAYS = 14
DIR_MODE, FILE_MODE = 0o700, 0o600
RUN_ID = re.compile(r"wf-[A-Za-z0-9-]{1,64}")


def runs_root() -> Path:
    try:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()  # honours the context-local profile override first
    except ImportError:
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "workflows" / "runs"


def new_run_id() -> str:
    return "wf-%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), secrets.token_hex(3))


class RunStore:
    def __init__(self, root: Path | None = None):
        self.root = root or runs_root()

    def folder(self, run_id: str) -> Path | None:
        """The folder of ``run_id``, or None for a string that is not a run id (no path escapes the store)."""
        return self.root / run_id if RUN_ID.fullmatch(run_id or "") else None

    def prune(self, now: float | None = None) -> None:
        if not self.root.is_dir():
            return
        cutoff = (time.time() if now is None else now) - KEEP_DAYS * 86400
        for folder in self.root.iterdir():
            try:
                stale = RUN_ID.fullmatch(folder.name) and folder.is_dir() and not folder.is_symlink() and (folder / "script.js").is_file() and _last_write(folder) < cutoff
            except OSError:  # removed by a concurrent prune
                continue
            if stale:
                shutil.rmtree(folder, ignore_errors=True)

    def create(self, run_id: str, source: str, request: dict) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        for path in (self.root.parent, self.root):
            os.chmod(path, DIR_MODE)
        folder = self.root / run_id
        folder.mkdir(mode=DIR_MODE)
        os.chmod(folder, DIR_MODE)
        _write(folder / "script.js", source)
        _write(folder / "request.json", json.dumps(request, default=str))
        return folder

    def finish(self, folder: Path, record: dict) -> Path:
        """Write ``result.json``; the journal exists afterwards even when the runner never opened it."""
        journal = folder / "journal.jsonl"
        if not journal.exists():
            _write(journal, "")
        return _write(folder / "result.json", json.dumps(record, indent=1, default=str))

    def result(self, folder: Path) -> dict[str, Any] | None:
        """The stored record of a finished run, or None (unfinished, unknown or unreadable)."""
        try:
            record = json.loads((folder / "result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return record if isinstance(record, dict) else None


def _last_write(folder: Path) -> float:
    stamps = [folder.lstat().st_mtime]
    stamps += [p.lstat().st_mtime for p in folder.iterdir()]
    return max(stamps)


def _write(path: Path, text: str) -> Path:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        os.fchmod(fh.fileno(), FILE_MODE)
        fh.write(text)
    return path
