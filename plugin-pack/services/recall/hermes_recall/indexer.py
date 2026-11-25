"""Indexer of hermes-recall (spec Task 4; REQ-001, REQ-003, REQ-013..015, REQ-022, REQ-023).

Called by the HTTP service (RC-SVC); it starts no threads or servers of its own (each ``Index`` has its
store write queue, and every write here waits for its job). API:

* ``Indexer(home, *, embedder=None, clock=time.time, stop=None, index_dir=None, only_index=None)``;
  ``index(name)`` opens an index lazily (a profile or ``shared-wiki``). The file is classified over a
  read-only connection before any DDL (index_version.verdict): a new one is stamped, one whose mark is not
  current raises ``IndexOutdated`` (``allow_outdated=True`` still opens one of our own table layout; one of
  another layout is never opened); ``is_outdated(name)``; ``close()`` closes them all. ``index_dir`` and
  ``only_index`` are for ``index_version.rebuild``: build one index in a side directory;
* ``index_turn(profile, session_id, project_id, messages) -> int``: appends the user/assistant
  messages (``{role, text, ts}``) to the session's source (kind ``session``, ref = session id) and
  returns how many were queued; earlier turns stay;
* ``index_summary(profile, session_id, text, kind)``: ``session_summary`` (replaces the previous one)
  or ``compaction`` (appended); same ref and scope as the session (REQ-003), from ``state.db`` when no
  turn of it is indexed yet; every text is redacted before chunking (REQ-024);
* ``delete_session(profile, session_id) -> int``: its turn, summary and compaction chunks with their
  vectors (REQ-022);
* ``reconcile(profile) -> Report`` and ``catch_up() -> Report`` (indexer_reconcile); ``last_reconcile(profile)``
  is the clock time of the profile's last reconcile, or None (the 5-minute rule is RC-SVC's);
* ``embed_pending(limit=256) -> int``: with an embedder, vectors for up to ``limit`` chunks without
  one, recording ``model_id`` on first use; an index of another model is left to the rebuild (REQ-026).
  FTS rows are always written; without an embedder nothing is embedded.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from hermes_recall import index_version, sources
from hermes_recall.index_version import IndexOutdated
from hermes_recall.indexer_reconcile import SESSION_KINDS, Report, catch_up, day, message_chunks, reconcile
from hermes_recall.indexer_secrets import SecretsUnavailable, clear_incomplete, incomplete_on_failure, owner_scope
from hermes_recall.store import Index, Source, index_path

__all__ = ["BATCH", "IndexOutdated", "Indexer", "Report", "SESSION_KINDS", "SUMMARY_KINDS", "SecretsUnavailable",
           "day", "message_chunks"]

SUMMARY_KINDS = ("session_summary", "compaction")
BATCH = 16  # texts per embed call: bounds the padded ONNX batch


def _turn(m: Mapping[str, Any] | Sequence[Any], now: float) -> tuple[str, str, float]:
    if isinstance(m, Mapping):
        return str(m.get("role", "")), str(m.get("text") or ""), float(m.get("ts") or now)
    role, text, ts = m
    return str(role), str(text or ""), float(ts or now)


class Indexer:
    def __init__(self, home: Path, *, embedder: Any = None, clock: Callable[[], float] = time.time,
                 stop: threading.Event | None = None, index_dir: Path | None = None, only_index: str | None = None):
        self.home = Path(home)
        self.index_dir = index_dir  # None: the service's ``<home>/recall``
        self.only_index = only_index  # set: reconcile reads, writes and prunes just this index
        self.stop = stop or threading.Event()  # set: catch-up and reconcile stop between sources
        self.embedder = embedder
        self.clock = clock
        self._indexes: dict[str, Index] = {}
        self._outdated: dict[str, str] = {}  # name -> verdict, decided when first opened; a rebuild needs a fresh Indexer
        self._reconciled: dict[str, float] = {}
        self.hydrated: set[str] = set()  # homes whose secret sources this process resolved
        self._lock = threading.Lock()

    def _path(self, name: str) -> Path:
        path = index_path(self.home, name)  # validates the name
        return path if self.index_dir is None else Path(self.index_dir) / path.name

    def _admit(self, name: str) -> None:
        """Classify and open ``name``; an index whose stamp failed is closed and not cached, so the next call
        classifies it again instead of trusting a half-checked one. Caller holds the lock."""
        path = self._path(name)
        verdict = index_version.verdict(path)
        if verdict == "incompatible":
            self._outdated[name] = verdict  # another table layout: no DDL, no queries of ours
            return
        index = Index(path)
        try:
            if verdict == "new":
                index_version.stamp(index)
        except BaseException:
            index.close()
            raise
        self._indexes[name] = index
        if verdict == "outdated":
            self._outdated[name] = verdict

    def index(self, name: str, *, allow_outdated: bool = False) -> Index:
        with self._lock:
            if name not in self._indexes and name not in self._outdated:
                self._admit(name)
            if name in self._outdated and (not allow_outdated or name not in self._indexes):
                raise IndexOutdated(name, self.home)
            return self._indexes[name]

    def is_outdated(self, name: str) -> bool:
        try:
            self.index(name, allow_outdated=True)
        except IndexOutdated:
            return True
        return name in self._outdated

    def close(self) -> None:
        with self._lock:
            indexes, self._indexes = list(self._indexes.values()), {}
        for index in indexes:
            index.close()

    # --- sessions --------------------------------------------------------------------------------

    def index_turn(self, profile: str, session_id: str, project_id: str | None,
                   messages: Iterable[Mapping[str, Any] | Sequence[Any]]) -> int:
        now = self.clock()
        turns = [t for t in (_turn(m, now) for m in messages) if t[0] in ("user", "assistant") and t[1].strip()]
        if not turns:
            return 0
        scope = sources.scope_label(project_id)
        index = self.index(profile)
        with incomplete_on_failure(index, session_id), owner_scope(self, profile):
            chunks = message_chunks("session", session_id, scope, [(text, ts) for _, text, ts in turns])
        source = Source("session", session_id, scope, project_id, sources.machine(), "", max(t[2] for t in turns))
        index.put_source(source, chunks, append=True).result()
        return len(turns)

    def index_summary(self, profile: str, session_id: str, text: str, kind: str) -> int:
        """Index a session-end or compaction summary; returns its chunk count."""
        if kind not in SUMMARY_KINDS:
            raise ValueError("summary kind must be one of %s, not %r" % (SUMMARY_KINDS, kind))
        index = self.index(profile)
        found = index.rows("SELECT scope, project_id FROM sources WHERE kind = 'session' AND ref = ?", (session_id,))
        if found:
            scope, project_id = found[0]
        else:  # no turn indexed yet (queued or failed): the session's project from its state.db cwd
            home = sources.profiles(self.home).get(profile)
            project_id = sources.session_project(home, session_id) if home else None
            scope = sources.scope_label(project_id)
        now = self.clock()
        with incomplete_on_failure(index, session_id), owner_scope(self, profile):
            chunks = message_chunks(kind, session_id, scope, [(text, now)])
        source = Source(kind, session_id, scope, project_id, sources.machine(), "", now)
        return len(index.put_source(source, chunks, append=kind == "compaction").result())

    def delete_session(self, profile: str, session_id: str) -> int:
        index = self.index(profile)
        clear_incomplete(index, session_id)
        return sum(index.delete_source(kind, session_id).result() for kind in SESSION_KINDS)

    # --- files and catch-up (indexer_reconcile) ----------------------------------------------------

    def reconcile(self, profile: str) -> Report:
        report = reconcile(self, profile)
        self._reconciled[profile] = self.clock()
        return report

    def catch_up(self, only: str | None = None) -> Report:
        return catch_up(self, only=only)

    def last_reconcile(self, profile: str) -> float | None:
        return self._reconciled.get(profile)

    # --- vectors ---------------------------------------------------------------------------------

    def embed_pending(self, limit: int = 256) -> int:
        if self.embedder is None:
            return 0
        model = self.embedder.model_id
        with self._lock:
            indexes = [i for name, i in self._indexes.items() if name not in self._outdated]
        done = 0
        for index in indexes:
            current = index.model_id()
            if current not in (None, model):
                continue  # another model's index: the rebuild (REQ-026) owns it
            while done < limit and (batch := index.missing_vectors(model, min(BATCH, limit - done))):
                vectors = self.embedder.embed_documents([text for _, text in batch])
                index.set_vectors(model, zip([cid for cid, _ in batch], vectors)).result()
                done += len(batch)
                if current is None:
                    index.set_model_id(model).result()
                    current = model
        return done
