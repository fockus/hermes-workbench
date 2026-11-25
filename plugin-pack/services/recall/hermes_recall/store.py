"""SQLite store of hermes-recall (spec Task 2; REQ-001, REQ-011, REQ-016, REQ-026, REQ-027).

One file per index: ``$HERMES_HOME/recall/<profile>.sqlite`` and ``shared-wiki.sqlite``, in WAL mode.
Tables: ``sources`` (one row per document or session, unique by ``(kind, ref)``; ``note`` records a
cap or binary-skip decision, REQ-025), ``chunks``, the FTS5 table ``chunks_fts`` over header and text
(rowid = chunk id), ``vectors`` (float32 blobs per chunk and model) and ``index_meta`` (``model_id``, and the
``format_version`` / ``redaction_version`` marks of index_version.py).

Every write goes through the index's single write queue: one thread that owns the write connection
and runs the submitted jobs in order (REQ-027). Write methods return a ``concurrent.futures.Future``;
``.result()`` waits for the job and re-raises its error. Reads use a separate connection and see the
last committed state (WAL lets them run while a write is in progress). SQL is parameterised only.

API: ``open_index(home, name)`` -> ``Index``; writes ``put_source``, ``delete_source``,
``set_vectors``, ``set_model_id``, ``submit``; reads ``fts``, ``chunks``, ``vectors``,
``missing_vectors``, ``model_id``, ``rows``; ``close``.
"""

from __future__ import annotations

import queue
import re
import sqlite3
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import numpy as np

from hermes_recall.chunker import Chunk

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL, scope TEXT,
    project_id TEXT, machine TEXT, hash TEXT, mtime REAL, updated_at REAL, note TEXT, UNIQUE(kind, ref));
CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL REFERENCES sources(id),
    ordinal INTEGER, lines TEXT, header TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS chunks_source ON chunks(source_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(body);
CREATE TABLE IF NOT EXISTS vectors(chunk_id INTEGER NOT NULL, model_id TEXT NOT NULL, dim INTEGER, blob BLOB,
    PRIMARY KEY(chunk_id, model_id));
CREATE TABLE IF NOT EXISTS index_meta(key TEXT PRIMARY KEY, value TEXT);
"""
_WORD = re.compile(r"\w+")
_STOP = object()


@dataclass(frozen=True)
class Source:
    kind: str  # "session", "bank", "wiki", ...
    ref: str  # path or session id
    scope: str  # "project:<slug>", "general" or "wiki:shared"
    project_id: str | None
    machine: str
    hash: str
    mtime: float


def index_path(home: Path, name: str) -> Path:
    """``<home>/recall/<name>.sqlite``; ``name`` is a profile or ``shared-wiki``."""
    if not name or name in (".", "..") or Path(name).name != name:
        raise ValueError("index name must be one path component: %r" % name)
    return Path(home) / "recall" / ("%s.sqlite" % name)


def open_index(home: Path, name: str) -> "Index":
    return Index(index_path(home, name))


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


class WriteQueue:
    """One writer thread that owns ``conn`` and runs jobs ``fn(conn)`` in order, each in a transaction."""

    def __init__(self, conn: sqlite3.Connection, name: str):
        self._conn = conn
        self._jobs: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="recall-write-%s" % name, daemon=True)
        self._thread.start()

    def submit(self, fn: Callable[[sqlite3.Connection], Any]) -> Future:
        future: Future = Future()
        with self._lock:
            if self._closed:
                future.set_exception(RuntimeError("index closed"))
            else:
                self._jobs.put((fn, future))
        return future

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._jobs.put(_STOP)
        self._thread.join()
        self._conn.close()

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is _STOP:
                return
            fn, future = job
            if not future.set_running_or_notify_cancel():
                continue
            try:
                with self._conn:
                    result = fn(self._conn)
            except BaseException as exc:  # handed to the caller through the future
                future.set_exception(exc)
            else:
                future.set_result(result)


class Index:
    """One index file: writes through its ``WriteQueue``, reads on a separate connection."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        writer = _connect(path)
        writer.execute("PRAGMA journal_mode = WAL")
        writer.executescript(SCHEMA)
        self._queue = WriteQueue(writer, path.stem)
        self._reader = _connect(path)
        self._reader.isolation_level = None  # autocommit: a failed write never leaves a stale read transaction
        self._reader.execute("PRAGMA query_only = ON")
        self._read_lock = threading.Lock()

    def close(self) -> None:
        self._queue.close()
        with self._read_lock:
            self._reader.close()

    # --- writes (through the queue) -------------------------------------------------------------

    def submit(self, fn: Callable[[sqlite3.Connection], Any]) -> Future:
        """Run ``fn(conn)`` on the writer thread inside one transaction."""
        return self._queue.submit(fn)

    def put_source(self, source: Source, chunks: Sequence[Chunk], note: str | None = None,
                   append: bool = False) -> Future:
        """Insert ``source`` with ``chunks``, replacing an earlier version; resolves to the new chunk ids.
        With ``append`` an existing source keeps its chunks: its row is updated and ``chunks`` are added
        after them (ordinals continue)."""
        return self.submit(lambda conn: _put(conn, source, chunks, note, append))

    def delete_source(self, kind: str, ref: str) -> Future:
        """Remove a source with its chunks, FTS rows and vectors; resolves to the number of chunks removed."""
        return self.submit(lambda conn: _drop(conn, kind, ref))

    def set_vectors(self, model_id: str, pairs: Iterable[tuple[int, Sequence[float]]]) -> Future:
        rows = []
        for chunk_id, vector in pairs:
            arr = np.asarray(vector, dtype=np.float32)
            rows.append((chunk_id, model_id, int(arr.size), arr.tobytes(), chunk_id))
        # A chunk id is never reused (AUTOINCREMENT), so a vector for a chunk dropped meanwhile is discarded.
        return self.submit(lambda conn: conn.executemany(
            "INSERT OR REPLACE INTO vectors(chunk_id, model_id, dim, blob) "
            "SELECT ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM chunks WHERE id = ?)", rows))

    def set_model_id(self, model_id: str) -> Future:
        """Record the embedding model of this index (REQ-016); vectors of other models stay until rebuilt."""
        return self.submit(lambda conn: conn.execute(
            "INSERT OR REPLACE INTO index_meta(key, value) VALUES ('model_id', ?)", (model_id,)))

    # --- reads -----------------------------------------------------------------------------------

    def rows(self, sql: str, args: Sequence = ()) -> list[tuple]:
        """A read-only parameterised query on the reader connection."""
        with self._read_lock:
            return self._reader.execute(sql, tuple(args)).fetchall()

    def model_id(self) -> str | None:
        found = self.rows("SELECT value FROM index_meta WHERE key = 'model_id'")
        return found[0][0] if found else None

    def fts(self, text: str, limit: int = 50) -> list[int]:
        """Chunk ids matching any word of ``text`` (BM25 order, best first); the text is never FTS syntax."""
        words = _WORD.findall(text)
        if not words:
            return []
        expr = " OR ".join('"%s"' % w for w in words)
        return [cid for (cid,) in self.rows(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?", (expr, limit))]

    def chunks(self, ids: Iterable[int]) -> dict[int, "ChunkRow"]:
        ids = list(ids)
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        found = self.rows(
            "SELECT c.id, s.kind, s.ref, s.scope, s.project_id, c.ordinal, c.lines, c.header, c.text "
            "FROM chunks c JOIN sources s ON s.id = c.source_id WHERE c.id IN (%s)" % marks, ids)
        return {row[0]: ChunkRow(*row) for row in found}

    def vectors(self, model_id: str) -> tuple[list[int], np.ndarray]:
        """Chunk ids and their vectors of ``model_id`` as one float32 matrix (one row per id)."""
        found = self.rows("SELECT chunk_id, blob FROM vectors WHERE model_id = ? ORDER BY chunk_id", (model_id,))
        if not found:
            return [], np.zeros((0, 0), dtype=np.float32)
        return [cid for cid, _ in found], np.stack([np.frombuffer(b, dtype=np.float32) for _, b in found])

    def missing_vectors(self, model_id: str, limit: int = 256) -> list[tuple[int, str]]:
        """Up to ``limit`` ``(chunk_id, header + text)`` pairs without a vector of ``model_id``."""
        return self.rows(
            "SELECT c.id, c.header || char(10) || c.text FROM chunks c LEFT JOIN vectors v "
            "ON v.chunk_id = c.id AND v.model_id = ? WHERE v.chunk_id IS NULL ORDER BY c.id LIMIT ?",
            (model_id, limit))


@dataclass(frozen=True)
class ChunkRow:
    id: int
    kind: str
    ref: str
    scope: str
    project_id: str | None
    ordinal: int
    lines: str
    header: str
    text: str


def _put(conn: sqlite3.Connection, source: Source, chunks: Sequence[Chunk], note: str | None,
         append: bool = False) -> list[int]:
    fields = (source.scope, source.project_id, source.machine, source.hash, source.mtime, time.time(), note)
    found = append and conn.execute(
        "SELECT id FROM sources WHERE kind = ? AND ref = ?", (source.kind, source.ref)).fetchone()
    if found:
        sid = found[0]
        conn.execute("UPDATE sources SET scope = ?, project_id = ?, machine = ?, hash = ?, mtime = ?, "
                     "updated_at = ?, note = COALESCE(?, note) WHERE id = ?", fields + (sid,))
        (base,) = conn.execute("SELECT COALESCE(MAX(ordinal) + 1, 0) FROM chunks WHERE source_id = ?", (sid,)).fetchone()
    else:
        _drop(conn, source.kind, source.ref)
        sid = conn.execute(
            "INSERT INTO sources(kind, ref, scope, project_id, machine, hash, mtime, updated_at, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (source.kind, source.ref) + fields).lastrowid
        base = 0
    ids = []
    for chunk in chunks:
        cid = conn.execute(
            "INSERT INTO chunks(source_id, ordinal, lines, header, text) VALUES (?, ?, ?, ?, ?)",
            (sid, base + chunk.ordinal, chunk.lines, chunk.header, chunk.text)).lastrowid
        conn.execute("INSERT INTO chunks_fts(rowid, body) VALUES (?, ?)", (cid, chunk.header + "\n" + chunk.text))
        ids.append(cid)
    return ids


def _drop(conn: sqlite3.Connection, kind: str, ref: str) -> int:
    removed = 0
    for (sid,) in conn.execute("SELECT id FROM sources WHERE kind = ? AND ref = ?", (kind, ref)).fetchall():
        ids = [(cid,) for (cid,) in conn.execute("SELECT id FROM chunks WHERE source_id = ?", (sid,))]
        conn.executemany("DELETE FROM chunks_fts WHERE rowid = ?", ids)
        conn.executemany("DELETE FROM vectors WHERE chunk_id = ?", ids)
        conn.execute("DELETE FROM chunks WHERE source_id = ?", (sid,))
        conn.execute("DELETE FROM sources WHERE id = ?", (sid,))
        removed += len(ids)
    return removed
