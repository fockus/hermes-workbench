"""Sandbox time and stop requests of one running script (used by repl.py).

Only Monty turns count toward the script's time, never the host answering a nested call. While a
turn runs, a watcher thread polls the deadline and the caller's interrupt bit (Hermes ``/stop`` or
a new user message, caller.py); either one stops the script through ``stop(reason)``, which kills
the worker. An interrupt that arrives while the host answers a call stops the script before the
next turn. A stop and the turn's end are decided under one lock, and the watcher is joined
before the turn returns, so a late watcher never stops a later turn or checkout.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

POLL = 0.05  # seconds between interrupt checks while a turn runs
TIME = "time"
INTERRUPTED = "interrupted: Hermes stopped the script"


class Clock:
    def __init__(
        self,
        seconds: float,
        stop: Callable[[str], None],
        interrupted: Callable[[], bool] | None = None,
    ):
        self.left, self.fired = seconds, False
        self._stop, self._interrupted = stop, interrupted or (lambda: False)

    def turn(self, fn: Callable[[], Any]) -> Any:
        if self._interrupted():
            self._stop(INTERRUPTED)
        done, start = threading.Event(), time.monotonic()
        decide = threading.Lock()  # the turn's end and a stop never interleave
        watcher = threading.Thread(
            target=self._watch,
            args=(done, decide, start, self.left),
            name="codemode-clock",
            daemon=True,
        )
        watcher.start()
        try:
            return fn()
        finally:
            spent = time.monotonic() - start  # the callable's time only, not the watcher's retirement
            with decide:
                done.set()
            watcher.join()  # retired before the caller releases or reuses the worker
            self.left -= spent

    def _watch(self, done: threading.Event, decide: threading.Lock, start: float, left: float) -> None:
        while not done.is_set():
            remaining = left - (time.monotonic() - start)
            if remaining <= 0:
                reason = TIME
            elif done.wait(min(POLL, remaining)):
                return
            elif self._interrupted():
                reason = INTERRUPTED
            else:
                continue
            with decide:
                if done.is_set():  # the turn ended first: nothing to stop
                    return
                if reason == TIME:
                    self.fired = True
                self._stop(reason)
            return
