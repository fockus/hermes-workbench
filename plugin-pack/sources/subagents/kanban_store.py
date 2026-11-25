"""The plugin's own tables in the stock kanban.db (D-07..D-09: the launch queue, agent names and
message history live there, next to the boards kanban-gantt shows).

Connections and transactions are stock (`kanban_db_connect.connect_closing`, `write_txn`). Hermes
fences every kanban write inside a delegate child (SPIKE.md § 6), and hooks fired by a child's tool
call run in that fence, so a write from a fenced context is kept in an in-memory outbox and applied by
the next unfenced write or ``flush()`` on the main agent's side; it is never forced past the fence. Each
deferred write keeps the board it was made for and is applied there (Astra review round 1).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger("subagents.kanban_store")

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS subagents_queue (agent_id TEXT PRIMARY KEY, provider TEXT NOT NULL, "
    "root_task TEXT NOT NULL, pid INTEGER NOT NULL, queued_at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS subagents_names (name TEXT NOT NULL, subagent_id TEXT NOT NULL, "
    "owner_session TEXT NOT NULL, state TEXT NOT NULL, updated_at REAL NOT NULL, root TEXT NOT NULL, "
    "PRIMARY KEY (root, name))",
    "CREATE TABLE IF NOT EXISTS subagents_children (subagent_id TEXT PRIMARY KEY, root TEXT NOT NULL, name TEXT, "
    "schema INTEGER, policy TEXT NOT NULL, updated_at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS subagents_roots (session TEXT PRIMARY KEY, root TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS subagents_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT NOT NULL, "
    "recipient TEXT NOT NULL, subagent_id TEXT NOT NULL, delivery TEXT NOT NULL, text TEXT NOT NULL, "
    "created_at REAL NOT NULL)",
)

Statement = tuple[str, tuple[Any, ...]]

def _ensure(conn: Any) -> None:
    """The plugin tables of SCHEMA, inside the caller's write transaction (no earlier layout was ever installed)."""
    for statement in SCHEMA:
        conn.execute(statement)


def current_board() -> Path:
    """The board database writes go to now (stock ``kanban_db_path``: profile, board, ``HERMES_KANBAN_DB``)."""
    from hermes_cli.kanban_db import kanban_db_path

    return Path(kanban_db_path()).resolve()


def _fenced(path: Path) -> bool:
    from agent.delegation_context import kanban_path_is_fenced

    return kanban_path_is_fenced(path)


class KanbanStore:
    """Parameterised statements against the plugin tables of a board's kanban.db. A write is bound to the board
    current when it is made; a deferred one keeps that board until its own fence lets it through."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._outbox: dict[Path, list[Statement]] = {}  # destination board -> its deferred writes, in order
        self._ready: set[Path] = set()  # boards whose plugin tables this process created

    def write(self, sql: str, params: Sequence[Any] = (), board: Path | None = None) -> None:
        """Queues the statement for ``board`` (the current one when None) and applies it unless fenced."""
        board = board or current_board()
        with self._lock:
            self._outbox.setdefault(board, []).append((sql, tuple(params)))
        if not _fenced(board):
            self.flush()

    def flush(self) -> None:
        """Applies each board's deferred writes in one transaction on that board; a failed one is kept for retry."""
        from hermes_cli.kanban_db_connect import connect_closing, write_txn

        with self._lock:
            for board in [b for b in self._outbox if not _fenced(b)]:
                pending = self._outbox.pop(board)
                try:
                    with connect_closing(board) as conn, write_txn(conn):
                        _ensure(conn)
                        self._ready.add(board)
                        for sql, params in pending:
                            conn.execute(sql, params)
                except (sqlite3.Error, OSError) as exc:
                    self._outbox[board] = pending + self._outbox.get(board, [])
                    logger.warning("subagents: kanban.db write to %s failed (%d kept for retry): %s", board,
                                   len(pending), exc)

    def read(self, sql: str, params: Sequence[Any] = (), strict: bool = False) -> list[tuple[Any, ...]]:
        """Rows of the current board as tuples; empty when the board or the plugin tables do not exist yet. A failed
        read is empty too, unless ``strict``: then it raises (a policy lookup must not take an error for absence)."""
        from hermes_cli.kanban_db_connect import connect_closing, write_txn

        board = current_board()
        try:
            with connect_closing(board) as conn:
                if board not in self._ready and not _fenced(board):  # the tables exist before any read
                    with write_txn(conn):
                        _ensure(conn)
                    self._ready.add(board)
                return [tuple(row) for row in conn.execute(sql, tuple(params))]
        except (sqlite3.Error, OSError) as exc:
            if strict:
                raise
            logger.debug("subagents: kanban.db read skipped: %s", exc)
            return []


_STORE: KanbanStore | None = None
_STORE_LOCK = threading.Lock()


def get_store() -> KanbanStore:
    """The process store: one outbox for every writer (limiter, names, messages)."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = KanbanStore()
        return _STORE
