"""Idle unload for any ``Embedder`` (REQ-019): unload after 15 minutes without an embed call.

The service's background idle check calls ``check_idle()`` at least every 5 s; that thread lives in
server.py. Time comes from ``time.monotonic()`` (looked up at call time) or an injected ``clock``.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

IDLE_SECONDS = 15 * 60


class IdleEmbedder:
    """Wraps an ``Embedder``; ``loaded`` is true from the first embed call until the (idle) unload."""

    def __init__(self, inner: Any, *, idle_seconds: float = IDLE_SECONDS, clock: Callable[[], float] | None = None):
        self.inner, self.idle_seconds, self._clock = inner, idle_seconds, clock
        self._lock = threading.Lock()
        self._loaded = False
        self._last = 0.0

    @property
    def model_id(self) -> str:
        return self.inner.model_id

    @property
    def dimensions(self) -> int:
        return self.inner.dimensions

    @property
    def max_tokens(self) -> int:
        return self.inner.max_tokens

    @property
    def loaded(self) -> bool:
        return self._loaded and getattr(self.inner, "loaded", True)

    def _now(self) -> float:
        return self._clock() if self._clock is not None else time.monotonic()

    def _call(self, fn: Callable[[], Any]) -> Any:
        with self._lock:
            self._loaded = True
            try:
                return fn()
            finally:
                self._last = self._now()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._call(lambda: self.inner.embed_documents(texts))

    def embed_query(self, text: str) -> list[float]:
        return self._call(lambda: self.inner.embed_query(text))

    def unload(self) -> None:
        with self._lock:
            self.inner.unload()
            self._loaded = False

    def check_idle(self) -> bool:
        """Unload when idle for ``idle_seconds``; never blocks (a running embed call means not idle).
        True when this call unloaded the model."""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if not self.loaded or self._now() - self._last < self.idle_seconds:
                return False
            self.inner.unload()
            self._loaded = False
            return True
        finally:
            self._lock.release()
