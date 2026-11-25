"""Read-only sources of hermes-recall (spec Task 4; REQ-001, design C4): profiles, Hermes projects,
bank and wiki documents, and sessions of the stock ``state.db``. Nothing here writes.

Locations (as the memory-bank plugin's ``resolve_bank``, restated in the contract harness):

* profile ``default`` is the Hermes root ``home``; profile ``X`` is ``home/profiles/X``;
* the general bank is ``<profile home>/.memory-bank``; a Hermes project of the profile's
  ``projects.db`` has its bank in ``<folder>/.memory-bank`` when a folder has one, else in
  ``<profile home>/projects/<slug>/.memory-bank``, and its wiki in ``<profile home>/projects/<slug>/llm-wiki``;
* the shared wiki ``home/llm-wiki`` belongs to ``default`` only (scope ``wiki:shared``, index ``shared-wiki``);
* a wiki is its ``wiki/**/*.md`` pages and its ``raw/**/*.md|*.txt`` sources; a bank is ``**/*.md``.

A file whose real path leaves its root (a symlink out of the bank or wiki) is skipped. Sessions hold
user messages and final assistant replies (no tool messages, no assistant message carrying tool
calls, no Codex interim reply with ``finish_reason = 'incomplete'``, no reply-verification draft with
``finish_reason`` ``'verification_required'`` or ``'verify_hook_continue'`` (agent/turn_stop_gates.py), no row hidden from the transcript
by ``/undo`` or a compaction carry: Hermes shows ``active = 1 OR compacted = 1``) and the compaction
summaries Hermes stores as ``messages._compressed_summary = 1``; multi-part content (``\x00json:`` + JSON)
yields its text parts; a compaction's archived original and its active copy share a ``display_order``
and give one message (stock timeline rule: per slot, the first by ``active DESC, id DESC``). ``state.db``
has no ``updated_at`` column; a session's ``updated_at`` is the latest of ``started_at``,
``ended_at``, ``last_activity_at`` and its newest message timestamp. Databases are opened with a
``mode=ro`` URI; a missing database yields no rows, an unreadable one None (unknown, which callers
never take for "nothing there": they do not prune on it).
"""

from __future__ import annotations

import functools
import json
import socket
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

SHARED = "wiki:shared"
SHARED_INDEX = "shared-wiki"
_BANK = ".memory-bank"
_JSON = "\x00json:"  # hermes_state.SessionDB._CONTENT_JSON_PREFIX
_TEXT_PARTS = ("text", "input_text", "output_text")
_UPDATED = ("MAX(COALESCE(s.ended_at, 0), COALESCE(s.last_activity_at, 0), s.started_at, "
            "COALESCE((SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = s.id), 0))")

# One row per display slot (legacy NULL: the row itself), the first by ``active DESC, id DESC``: as
# hermes_state_timeline.py shows a compacted session, so an archived original and its copy count once.
_MESSAGES = ("SELECT session_id, role, content, timestamp, tool_calls, _compressed_summary, finish_reason FROM ("
             "SELECT m.*, COALESCE(m.display_order, m.id) AS slot, ROW_NUMBER() OVER (PARTITION BY m.session_id, "
             "COALESCE(m.display_order, m.id) ORDER BY m.active DESC, m.id DESC) AS pick FROM messages m "
             "WHERE m.session_id IN (%s) AND (m.active = 1 OR m.compacted = 1)) WHERE pick = 1 ORDER BY slot, id")


@dataclass(frozen=True)
class Scope:
    profile: str
    kind: Literal["folder", "project", "general"]
    project_id: str | None
    machine: str


@dataclass(frozen=True)
class Doc:
    kind: str  # "memory_bank" or "wiki" (design Hit.source)
    ref: str  # str(path)
    path: Path
    scope: str  # "project:<slug>", "general" or "wiki:shared"
    project_id: str | None


@dataclass(frozen=True)
class Session:
    id: str
    project_id: str | None
    updated_at: float
    messages: list[tuple[str, str, float]]  # (role, text, ts): user messages and final replies
    compactions: list[tuple[str, float]]  # (summary, ts)
    rewind_count: int = 0  # sessions.rewind_count: bumped by /undo, which leaves every timestamp as it was


def scope_label(project_id: str | None) -> str:
    return "project:%s" % project_id if project_id else "general"


@functools.lru_cache(maxsize=1)
def machine() -> str:
    return socket.gethostname()


def profiles(home: Path) -> dict[str, Path]:
    home = Path(home)
    out = {"default": home} if home.is_dir() else {}
    root = home / "profiles"
    if root.is_dir():
        out.update({p.name: p for p in sorted(root.iterdir()) if p.is_dir()})
    return out


def _read(db: Path, sql: str, args: tuple = ()) -> list[tuple] | None:
    """Rows of ``sql``: [] for a missing database, None for one that cannot be read."""
    if not db.is_file():
        return []
    try:
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def projects(profile_home: Path) -> dict[str, list[str]] | None:
    """slug -> folders of the profile's live (not archived) Hermes projects (a project may have none);
    None when ``projects.db`` cannot be read."""
    rows = _read(Path(profile_home) / "projects.db",
                 "SELECT p.slug, f.path FROM projects p LEFT JOIN project_folders f ON f.project_id = p.id "
                 "WHERE p.archived = 0 ORDER BY p.created_at, f.is_primary DESC, f.path")
    if rows is None:
        return None
    out: dict[str, list[str]] = {}
    for slug, folder in rows:
        folders = out.setdefault(slug, [])
        if folder is not None:
            folders.append(folder)
    return out


def _files(root: Path, suffixes: tuple[str, ...] = (".md",)) -> list[Path]:
    if not root.is_dir():
        return []
    real = root.resolve()
    out = []
    for path in sorted(root.rglob("*")):
        if path.suffix not in suffixes or not path.is_file():
            continue
        if path.resolve().is_relative_to(real):
            out.append(path)
    return out


def _wiki(root: Path) -> list[Path]:
    return _files(root / "wiki") + _files(root / "raw", (".md", ".txt"))


def documents(home: Path, profile: str) -> list[Doc] | None:
    """Every bank and wiki document of ``profile`` (plus the shared wiki for ``default``); None when the
    profile's ``projects.db`` cannot be read, so the list would be incomplete."""
    home = Path(home)
    ph = profiles(home).get(profile)
    if ph is None:
        return []
    known = projects(ph)
    if known is None:
        return None
    docs = [Doc("memory_bank", str(p), p, "general", None) for p in _files(ph / _BANK)]
    for slug, folders in known.items():
        scope = scope_label(slug)
        banks = [Path(f) / _BANK for f in folders if (Path(f) / _BANK).is_dir()]
        for bank in banks or [ph / "projects" / slug / _BANK]:
            docs += [Doc("memory_bank", str(p), p, scope, slug) for p in _files(bank)]
        docs += [Doc("wiki", str(p), p, scope, slug) for p in _wiki(ph / "projects" / slug / "llm-wiki")]
    if profile == "default":
        docs += [Doc("wiki", str(p), p, SHARED, None) for p in _wiki(home / "llm-wiki")]
    return docs


def _project_for(cwd: str | None, known: dict[str, list[str]]) -> str | None:
    """The project whose folder is the longest prefix of ``cwd``."""
    best: tuple[int, str] | None = None
    for slug, folders in known.items():
        for folder in folders:
            base = folder.rstrip("/")
            if cwd and (cwd == base or cwd.startswith(base + "/")) and (best is None or len(base) > best[0]):
                best = (len(base), slug)
    return best[1] if best else None


def _final(role: str, tool_calls: str | None, finish_reason: str | None) -> bool:
    if finish_reason in ("incomplete", "verification_required", "verify_hook_continue"):
        return False  # a Codex interim reply or a reply-verification draft, not the final one
    return role == "user" or (role == "assistant" and (tool_calls or "").strip() in ("", "[]", "null"))


def _text(content: object) -> str:
    """The text of a stored message: multi-part content keeps its text parts; undecodable JSON gives ''."""
    if content is None:
        return ""
    if not isinstance(content, str):
        return str(content)
    if not content.startswith(_JSON):
        return content
    try:
        parts = json.loads(content[len(_JSON):])
    except ValueError:
        return ""
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        return ""
    return "\n".join(p["text"] for p in parts
                     if isinstance(p, dict) and p.get("type") in _TEXT_PARTS and isinstance(p.get("text"), str))


def session_ids(profile_home: Path) -> set[str] | None:
    """Ids of the sessions in ``state.db``; None when it is missing or unreadable (never prune on it)."""
    db = Path(profile_home) / "state.db"
    rows = _read(db, "SELECT id FROM sessions") if db.is_file() else None
    return None if rows is None else {sid for (sid,) in rows}


def session_project(profile_home: Path, session_id: str) -> str | None:
    """The project of a session by its ``state.db`` cwd, or None."""
    rows = _read(Path(profile_home) / "state.db", "SELECT cwd FROM sessions WHERE id = ?", (session_id,))
    return _project_for(rows[0][0], projects(profile_home) or {}) if rows else None


def sessions(profile_home: Path, marks: Mapping[str, float] | None = None,
             rewinds: Mapping[str, int] | None = None) -> list[Session]:
    """Sessions of the profile's ``state.db``, oldest first: all when ``marks`` is None, else those with
    no mark, updated after their mark (``marks``: session id -> newest indexed time) or, with ``rewinds``
    (session id -> rewind count at its last load, absent = 0), rewound since. None from ``projects``
    (unreadable projects.db) gives [] so nothing is scoped wrongly or marked; the next call retries. A
    batch whose messages cannot be read yields no session at all, never an empty one."""
    known = projects(profile_home)
    if known is None:
        return []
    db = Path(profile_home) / "state.db"
    heads = [(sid, cwd, updated, rewound) for sid, cwd, updated, rewound in
             _read(db, "SELECT s.id, s.cwd, %s AS updated, s.rewind_count FROM sessions s "
                       "ORDER BY updated, s.id" % _UPDATED) or []
             if marks is None or sid not in marks or updated > marks[sid]
             or (rewinds is not None and (rewound or 0) != rewinds.get(sid, 0))]
    turns: dict[str, list[tuple[str, str, float]]] = {}
    summaries: dict[str, list[tuple[str, float]]] = {}
    unread: set[str] = set()
    for start in range(0, len(heads), 500):  # bounded IN list per query
        ids = [head[0] for head in heads[start:start + 500]]
        rows = _read(db, _MESSAGES % ",".join("?" * len(ids)), tuple(ids))
        if rows is None:
            unread.update(ids)
            continue
        for sid, role, content, ts, tool_calls, compressed, finish_reason in rows:
            text = _text(content)
            if not text:
                continue
            if compressed:
                summaries.setdefault(sid, []).append((text, ts))
            elif _final(role, tool_calls, finish_reason):
                turns.setdefault(sid, []).append((role, text, ts))
    return [Session(sid, _project_for(cwd, known), updated, turns.get(sid, []), summaries.get(sid, []), rewound or 0)
            for sid, cwd, updated, rewound in heads if sid not in unread]
