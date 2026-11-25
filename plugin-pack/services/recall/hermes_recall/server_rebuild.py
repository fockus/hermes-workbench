"""Background dense rebuild of the hermes-recall service (spec Task 6; REQ-026), split out of
server_engine.py by size.

An index whose ``model_id()`` is neither None nor the embedder's is rebuilt in this order: every chunk is
embedded for the new model (``missing_vectors`` in ``BATCH`` batches), then ``set_model_id(new)``, then
the other models' vectors are deleted through the index's write queue. A chunk written between the last
empty check and the switch is embedded by one more pass after it; later ones by the job thread's pass. Search stays degraded (BM25 only)
until ``set_model_id``, because ``search`` compares the index's model with the embedder's.
``state()`` is ``{state: idle|running|done|failed, index, progress: 0..1}`` (progress of that index).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Iterable

from hermes_recall.indexer import BATCH
from hermes_recall.store import Index

log = logging.getLogger(__name__)


class Rebuild:
    def __init__(self, stop: threading.Event):
        self._stop = stop
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"state": "idle", "index": None, "progress": 0.0}
        self._thread: threading.Thread | None = None

    def state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def _set(self, **fields: Any) -> None:
        with self._lock:
            self._state.update(fields)

    def start(self, embedder: Any, indexes: Iterable[tuple[str, Index]], on_error: Callable[[str, str], None]) -> bool:
        """Start the rebuild of every stale index; False when none is stale (or there is no embedder)."""
        if embedder is None:
            return False
        model = embedder.model_id
        stale = [(name, index) for name, index in indexes if index.model_id() not in (None, model)]
        if not stale:
            return False
        self._set(state="running", index=stale[0][0], progress=0.0)
        self._thread = threading.Thread(target=self._run, args=(embedder, stale, on_error),
                                        name="recall-rebuild", daemon=True)
        self._thread.start()
        return True

    def join(self, timeout: float) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _embed(self, embedder: Any, index: Index) -> None:
        model = embedder.model_id
        (total,) = index.rows("SELECT COUNT(*) FROM chunks")[0]
        while not self._stop.is_set() and (batch := index.missing_vectors(model, BATCH)):
            vectors = embedder.embed_documents([text for _, text in batch])
            index.set_vectors(model, zip([cid for cid, _ in batch], vectors)).result()
            (done,) = index.rows("SELECT COUNT(*) FROM vectors WHERE model_id = ?", (model,))[0]
            self._set(progress=min(1.0, done / total) if total else 1.0)

    def _run(self, embedder: Any, stale: list[tuple[str, Index]], on_error: Callable[[str, str], None]) -> None:
        model = embedder.model_id
        name = stale[0][0]
        try:
            for name, index in stale:
                self._set(index=name, progress=0.0)
                self._embed(embedder, index)
                if self._stop.is_set():
                    return
                index.set_model_id(model).result()
                self._embed(embedder, index)  # a chunk written between the last check and the switch
                index.submit(lambda conn: conn.execute("DELETE FROM vectors WHERE model_id != ?", (model,))).result()
                self._set(progress=1.0)
            self._set(state="done")
        except Exception as exc:  # noqa: BLE001 - reported in status, the service keeps serving BM25
            log.exception("recall: rebuild of index %s failed", name)
            on_error(name, repr(exc))
            self._set(state="failed")
