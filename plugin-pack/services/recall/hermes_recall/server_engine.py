"""Background side of the hermes-recall service (spec Task 6; REQ-014, REQ-015, REQ-019, REQ-026, REQ-029).

``Engine(home, embedder=None, reranker=None, missing=())`` owns one ``Indexer`` (with the embedder wrapped
in ``QueryEmbedder``) and four threads, all stopped by ``close()``:

* start: ``catch_up()`` once (REQ-015), the rebuild check, then ``ready``; a failure is logged and kept as
  the status ``last_error``, and the service serves anyway;
* jobs: index writes in arrival order; after the queue drains, ``embed_pending`` one batch at a time
  until it returns 0 or a job is queued. A job's error becomes its index's ``last_error``;
* idle: ``check_idle()`` every second (REQ-019: unload after 15 idle minutes with no request);
* rebuild (server_rebuild, REQ-026).

The 5-minute rule is ``fresh(profile)`` before every search: ``reconcile(profile)`` when the profile was
never reconciled or longer than 300 s ago, once per profile at a time; ``default`` too, whose reconcile
owns the shared wiki every search reads. It never runs ``catch_up``, which
prunes sessions missing from state.db and so would drop a session indexed live before Hermes wrote it.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from concurrent.futures import Future
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from hermes_recall import search as search_mod
from hermes_recall import sources
from hermes_recall.embedders import QueryEmbedder
from hermes_recall.index_version import IndexOutdated
from hermes_recall.indexer import BATCH, Indexer
from hermes_recall.server_rebuild import Rebuild

log = logging.getLogger(__name__)
STALE_SECONDS = 300
IDLE_CHECK_SECONDS = 1.0
CLOSE_SECONDS = 60.0  # one batch of a stopping thread
_EMBED = object()  # a job that only asks for the embed pass


class _Embedded:
    """The embedder of one search whose query vector is already computed (``search`` embeds the query once)."""

    def __init__(self, embedder: Any, vector: list[float]):
        self.model_id, self._vector = embedder.model_id, vector

    def embed_query(self, text: str) -> list[float]:
        return self._vector


class Engine:
    def __init__(self, home: Path, embedder: Any = None, reranker: Any = None, missing: Sequence[str] = ()):
        self.home = Path(home)
        self.embedder = QueryEmbedder(embedder) if embedder is not None else None
        self.reranker = reranker
        self.missing = [] if embedder is not None else (list(missing) or ["no embedding model configured"])
        self.stop = threading.Event()
        self.indexer = Indexer(self.home, embedder=self.embedder, clock=lambda: time.time(), stop=self.stop)
        self.ready = threading.Event()
        self.last_error: str | None = None
        self.errors: dict[str, str] = {}
        self.rebuild = Rebuild(self.stop)
        self._jobs: queue.Queue = queue.Queue()
        self._fresh_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._threads = [threading.Thread(target=fn, name="recall-%s" % name, daemon=True)
                         for name, fn in (("start", self._start), ("jobs", self._job_loop), ("idle", self._idle_loop))]
        for thread in self._threads:
            thread.start()

    # --- threads ---------------------------------------------------------------------------------

    def _start(self) -> None:
        try:
            for name, error in self.indexer.catch_up().errors.items():
                self._failed(name, error)
            if self.stop.is_set():
                return
            self.rebuild.start(self.embedder, self._current_indexes(), self._failed)
        except Exception as exc:  # noqa: BLE001 - the service serves what it has
            if self.stop.is_set():
                return
            log.exception("recall: start-up catch-up failed")
            self.last_error = repr(exc)
        self._jobs.put(_EMBED)
        self.ready.set()

    def _job_loop(self) -> None:
        while not self.stop.is_set():
            try:
                job = self._jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            if job is not _EMBED:
                name, fn, future = job
                try:
                    future.set_result(fn())
                except Exception as exc:  # noqa: BLE001 - recorded as the index's last error
                    log.exception("recall: index job for %s failed", name)
                    self._failed(name, repr(exc))
                    future.set_exception(exc)
            if self._jobs.empty():
                self._embed_pending()

    def _current_indexes(self) -> list[tuple[str, Any]]:
        """Every index that is not outdated; an outdated one is recorded and left alone."""
        current = []
        for name in self.names():
            try:
                current.append((name, self.indexer.index(name)))
            except IndexOutdated:
                self._failed(name, "index_outdated")
        return current

    def _embed_pending(self) -> None:
        try:
            # One batch at a time: a queued index or delete job goes first.
            while not self.stop.is_set() and self._jobs.empty() and self.indexer.embed_pending(BATCH) > 0:
                pass
        except Exception as exc:  # noqa: BLE001 - search stays degraded for what is not embedded
            if not self.stop.is_set():
                log.exception("recall: embedding failed")
                self.last_error = repr(exc)

    def _idle_loop(self) -> None:
        while not self.stop.wait(IDLE_CHECK_SECONDS):
            if self.embedder is not None:
                self.embedder.check_idle()

    def _failed(self, name: str, error: str) -> None:
        self.errors[name] = error

    # --- requests --------------------------------------------------------------------------------

    def submit(self, name: str, fn: Callable[[], Any]) -> Future:
        """Queue an index write for ``name``; it runs on the job thread in arrival order. An outdated index
        refuses it here (``IndexOutdated``), so nothing is queued."""
        self.indexer.index(name)
        future: Future = Future()
        self._jobs.put((name, fn, future))
        return future

    def index_turn(self, profile: str, session_id: str, project_id: str | None, messages: list) -> Future:
        return self.submit(profile, lambda: self.indexer.index_turn(profile, session_id, project_id, messages))

    def index_summary(self, profile: str, session_id: str, text: str, kind: str) -> Future:
        return self.submit(profile, lambda: self.indexer.index_summary(profile, session_id, text, kind))

    def delete_session(self, profile: str, session_id: str) -> int:
        return self.submit(profile, lambda: self.indexer.delete_session(profile, session_id)).result(30)

    def fresh(self, profile: str) -> None:
        """The 5-minute rule: reconcile ``profile``'s files when older than 300 s (one run at a time)."""
        with self._locks_guard:
            lock = self._fresh_locks.setdefault(profile, threading.Lock())
        with lock:
            last = self.indexer.last_reconcile(profile)
            if last is not None and time.time() - last <= STALE_SECONDS:
                return
            try:
                for name, error in self.indexer.reconcile(profile).errors.items():  # an outdated index, a partial read
                    self._failed(name, error)
            except Exception as exc:  # noqa: BLE001 - search answers from what is indexed
                log.exception("recall: reconcile of %s failed", profile)
                self._failed(profile, repr(exc))
        self._jobs.put(_EMBED)

    def search(self, profile: str, scope: dict, query: str, k: int, all_scopes: bool) -> search_mod.Result:
        for name in (profile, sources.SHARED_INDEX):  # the two indexes a search reads
            self.indexer.index(name)  # IndexOutdated: refused, nothing is reconciled or read
        for name in dict.fromkeys((profile, "default")):  # the shared wiki is reconciled with ``default``
            self.fresh(name)
        embedder, vector = self.embedder, None
        if embedder is not None:
            vector = embedder.embed_query_bounded(query)  # None: a background batch holds the embedder
        result = search_mod.search(self.indexer.index, profile=profile, scope=scope, query=query, k=k,
                                   all_scopes=all_scopes, embedder=None if vector is None else _Embedded(embedder, vector),
                                   reranker=self.reranker)
        return result if vector is not None or embedder is None else replace(result, model=embedder.model_id)

    def embed_query(self, query: str) -> list[float] | None:
        vector = None if self.embedder is None else self.embedder.embed_query_bounded(query)
        return None if vector is None else [float(x) for x in vector]

    # --- status ----------------------------------------------------------------------------------

    def names(self) -> list[str]:
        return [name for name in sources.profiles(self.home) if name != sources.SHARED_INDEX] + [sources.SHARED_INDEX]

    def status(self) -> dict[str, Any]:
        indexes = []
        for name in self.names():
            chunks = count = newest = None  # an index of another table layout is never queried
            try:
                index = self.indexer.index(name, allow_outdated=True)
            except IndexOutdated:
                pass
            else:
                ((chunks,),) = index.rows("SELECT COUNT(*) FROM chunks")
                ((count, newest),) = index.rows("SELECT COUNT(*), MAX(updated_at) FROM sources")
            indexes.append({"profile": name, "chunks": chunks, "sources": count, "last_error": self.errors.get(name),
                            "outdated": self.indexer.is_outdated(name),
                            "last_indexed": None if newest is None else
                            datetime.fromtimestamp(newest, timezone.utc).isoformat(timespec="seconds")})
        embedder = self.embedder
        return {"model": embedder.model_id if embedder else None, "loaded": bool(embedder and embedder.loaded),
                "missing": list(self.missing), "indexes": indexes, "rebuild": self.rebuild.state(),
                "eval": self._eval(), "last_error": self.last_error}

    def _eval(self) -> dict:
        try:
            data = json.loads((self.home / "recall" / "eval.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    # --- lifecycle -------------------------------------------------------------------------------

    def close(self) -> None:
        """Stop every thread; each stops between batches, so the indexes are closed only after the last one."""
        self.stop.set()
        for thread in self._threads:
            thread.join(CLOSE_SECONDS)
        self.rebuild.join(CLOSE_SECONDS)
        if any(t.is_alive() for t in self._threads) or self.rebuild.alive():
            log.warning("recall: a background thread is still running; its indexes stay open")
            return
        self.indexer.close()
