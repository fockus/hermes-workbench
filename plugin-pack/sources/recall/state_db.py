"""Read-only view of the profile's Hermes ``state.db`` for the plugin (restated from the service's ``sources``,
which the plugin never imports): the newest compaction summary of a session and its user / final assistant
texts. The database is opened with a ``mode=ro`` URI and a short busy timeout; a missing, locked or unreadable
database gives None / [] and never raises."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

_JSON = "\x00json:"  # hermes_state.SessionDB._CONTENT_JSON_PREFIX
_TEXT_PARTS = ("text", "input_text", "output_text")
_VISIBLE = "(active = 1 OR compacted = 1)"
_SUMMARY = "SELECT content FROM messages WHERE session_id = ? AND _compressed_summary = 1 AND %s ORDER BY id DESC LIMIT 1"
# the final messages of a session, one per display slot as the transcript shows it (an archived original and its copy once)
_TURNS = ("SELECT role, content FROM (SELECT m.*, ROW_NUMBER() OVER (PARTITION BY COALESCE(m.display_order, m.id) "
          "ORDER BY m.active DESC, m.id DESC) AS pick FROM messages m WHERE m.session_id = ? AND "
          "m._compressed_summary = 0 AND m.role IN ('user', 'assistant') AND (m.active = 1 OR m.compacted = 1) "
          "AND TRIM(COALESCE(m.tool_calls, '')) IN ('', '[]', 'null') "
          "AND COALESCE(m.finish_reason, '') NOT IN ('incomplete', 'verification_required', 'verify_hook_continue')) "
          "WHERE pick = 1 ORDER BY COALESCE(display_order, id), id")


def compaction_summary(profile_home: Path, session_id: str) -> str | None:
    rows = _read(profile_home, _SUMMARY % _VISIBLE, session_id)
    text = _text(rows[0][0]) if rows else ""
    return text or None


def user_text(profile_home: Path, session_id: str) -> list[tuple[str, str]]:
    """``(role, text)`` of the user messages and the final assistant replies of a session, oldest first."""
    return [(role, text) for role, content in _read(profile_home, _TURNS, session_id) or []
            if (text := _text(content))]


def _read(profile_home: Path, sql: str, session_id: str) -> list | None:
    db = Path(profile_home) / "state.db"
    try:
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)
        try:
            return conn.execute(sql, (session_id,)).fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError):
        return None


def _text(content: object) -> str:
    if not isinstance(content, str) or not content.startswith(_JSON):
        return content if isinstance(content, str) else ""
    try:
        parts = json.loads(content[len(_JSON):])
    except ValueError:
        return ""
    if isinstance(parts, str):
        return parts
    return "\n".join(p["text"] for p in parts if isinstance(p, dict) and p.get("type") in _TEXT_PARTS
                     and isinstance(p.get("text"), str)) if isinstance(parts, list) else ""
