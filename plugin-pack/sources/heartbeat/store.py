"""The heartbeat's own state: SQLite at ``<Hermes root>/heartbeat/heartbeat.db`` (shared by every profile, like
the kanban boards) with the ``journal``, ``action_counts`` and ``questions`` tables. Parameterised SQL only; the file is created on first use, so a
tick that finds nothing writes nothing."""

from __future__ import annotations

import contextlib
import sqlite3
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, board TEXT NOT NULL, task_id TEXT NOT NULL,
    action TEXT NOT NULL, reason TEXT NOT NULL, source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS action_counts (
    board TEXT NOT NULL, task_id TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS action_counts_task ON action_counts (board, task_id, ts);
CREATE TABLE IF NOT EXISTS questions (
    qid TEXT PRIMARY KEY, board TEXT NOT NULL, task_id TEXT NOT NULL, text TEXT NOT NULL,
    action TEXT NOT NULL, open INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL,
    delivered REAL, reminded INTEGER NOT NULL DEFAULT 0, anchor TEXT);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._ready = False

    @contextlib.contextmanager
    def connect(self):
        """One transaction on the store; the schema is created on the first connection."""
        if not self._ready:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        try:
            with conn:
                if not self._ready:
                    conn.executescript(_SCHEMA)
                    self._ready = True
                yield conn
        finally:
            conn.close()

    def journal(self, board: str, task_id: str, action: str, reason: str, source: str) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO journal (ts, board, task_id, action, reason, source) VALUES (?, ?, ?, ?, ?, ?)",
                         (time.time(), board, task_id, action, reason, source))

    def journal_rows(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT count(*) FROM journal").fetchone()[0])

    def count_action(self, board: str, task_id: str) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO action_counts (board, task_id, ts) VALUES (?, ?, ?)", (board, task_id, time.time()))

    def actions_since(self, board: str, task_id: str, *, hours: float = 24) -> int:
        """Automatic actions on one task in the last ``hours`` (the 3-per-24h limit, REQ-007)."""
        with self.connect() as conn:
            row = conn.execute("SELECT count(*) FROM action_counts WHERE board = ? AND task_id = ? AND ts > ?",
                               (board, task_id, time.time() - hours * 3600)).fetchone()
        return int(row[0])
