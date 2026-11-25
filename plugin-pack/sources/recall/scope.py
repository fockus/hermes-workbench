"""Session scope of the plugin (spec Task 7; design: folder bank -> Hermes project -> profile, the rule of the
memory-bank and llm-wiki plugins; their code is not importable without those plugins, so it is restated here).

``resolve(cwd, home)`` gives the ``{kind, project_id}`` the service searches in:

* ``cwd`` or an ancestor holds a ``.memory-bank``: ``{"kind": "folder", "project_id": <slug>}``; the slug is the
  Hermes project that owns the folder (``home/projects.db``, the longest folder wins) and None when no project
  does: the service then searches the bot's general scope, as only Hermes projects have an index scope;
* else a Hermes project owns ``cwd``: ``{"kind": "project", "project_id": <slug>}``;
* else ``{"kind": "general"}``.

The slug is the one the indexer labels chunks with (``project:<slug>``). A missing or unreadable ``projects.db``
names no project. With ``strict=True`` an existing but unreadable ``projects.db`` raises ``Unreadable``
instead: a write that must not land in a shared scope cannot tell then who owns the folder."""

from __future__ import annotations

import sqlite3
from pathlib import Path

BANK = ".memory-bank"
_PROJECT_FOLDERS = ("SELECT p.slug, f.path FROM projects p JOIN project_folders f ON f.project_id = p.id "
                    "WHERE p.archived = 0")


class Unreadable(Exception):
    """``projects.db`` exists but could not be read: the owner of a folder is unknown."""


def resolve(cwd: str | Path, home: str | Path, *, strict: bool = False, busy_timeout: float = 5.0) -> dict:
    where = _resolved(cwd)
    slug = _project(where, Path(home) / "projects.db", strict, busy_timeout)
    if any((folder / BANK).is_dir() for folder in (where, *where.parents)):
        return {"kind": "folder", "project_id": slug}
    return {"kind": "project", "project_id": slug} if slug else {"kind": "general"}


def _resolved(path: str | Path) -> Path:
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return Path(path)


def _project(where: Path, db: Path, strict: bool, busy_timeout: float) -> str | None:
    if not db.is_file():
        return None
    try:
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=busy_timeout)
        try:
            rows = conn.execute(_PROJECT_FOLDERS).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        if strict:
            raise Unreadable(str(exc)) from exc
        return None
    best: tuple[int, str] | None = None
    for slug, folder in rows:
        base = _resolved(folder)
        if (where == base or base in where.parents) and (best is None or len(base.parts) > best[0]):
            best = (len(base.parts), slug)
    return best[1] if best else None

