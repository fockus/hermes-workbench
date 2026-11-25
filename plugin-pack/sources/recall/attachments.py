"""Attachments into the current scope's LLM Wiki (spec Task 9; REQ-004, design C7).

Hermes puts a note about a user's document into the message text: ``[The user sent a document: '<name>'. It is
saved at: <path>. ...``. The text is untrusted input, so only a path that resolves to a file under
``$HERMES_HOME/cache/documents/`` is used. The file goes through the llm-wiki CLI (``ingest``, plus ``convert``
for a PDF) into the project's wiki when the scope has a project, else into the shared wiki. Nothing here raises
and nothing waits for the CLI: the caller runs ``ingest`` on a background thread."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
NOTE = re.compile(r"\[The user sent a (?:text )?document: '.*?'\. (?:It is saved at: (.+?)\. Its "
                  r"|Its content has been included below\. The file is also saved at: (.+?)\])")
TIMEOUT = 120
_ENV = ("HOME", "PATH", "TMPDIR", "LANG", "LC_ALL")  # the CLI gets no API keys of the host process
_SLUG = re.compile(r"(?!\.+$)[\w.-]+")  # one path component, never "." or ".."
_seen: set[str] = set()
_lock = threading.Lock()


def ingest(ctx: Any, user_message: str, scope: dict, hermes_home: Path) -> None:
    runtime = ctx.get_config("wiki_runtime_root")
    if not runtime:
        return
    python, script = Path(runtime) / ".venv" / "bin" / "python", Path(runtime) / "scripts" / "wiki.py"
    root = _wiki_root(scope, Path(hermes_home))
    if not (root and python.is_file() and script.is_file() and root.is_dir()):
        return
    for path in _documents(user_message, Path(hermes_home)):
        with _lock:
            if str(path) in _seen:
                continue
            _seen.add(str(path))
        steps = [["ingest", "--root", str(root), str(path)]]
        if path.suffix.lower() == ".pdf":
            steps.append(["convert", "--root", str(root)])
        for step in steps:
            if not _run([str(python), str(script), *step], Path(runtime)):
                break


def _wiki_root(scope: dict, home: Path) -> Path | None:
    """The project's wiki, the shared wiki without a project, None for a slug that is not a plain name."""
    slug = scope.get("project_id")
    if slug is None:
        return home / "llm-wiki"
    return home / "projects" / slug / "llm-wiki" if isinstance(slug, str) and _SLUG.fullmatch(slug) else None


def _documents(text: str, home: Path) -> list[Path]:
    cache = (home / "cache" / "documents").resolve()
    found = []
    for groups in NOTE.findall(text):  # the plain note and the inlined-text note put the path in different groups
        raw = next(group for group in groups if group)
        try:
            path = Path(raw).resolve()
            if path.is_relative_to(cache) and path.is_file():
                found.append(path)
        except (OSError, ValueError):  # an unreadable or NUL-carrying path names no document
            continue
    return found


def _run(args: list[str], cwd: Path) -> bool:
    env = {name: os.environ[name] for name in _ENV if name in os.environ}
    try:
        done = subprocess.run(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        log.debug("recall: the llm-wiki CLI did not run", exc_info=True)
        return False
    return done.returncode == 0
