"""The numbers and decisions the engine and the runner apply (REQ-017, 019, 020, 021, 022, 031).

Pure policy plus one watchdog; no subprocess, no engine import. Public names for WE-WIRE:
``concurrency(configured, cpus, limiter_cap)``, ``RUNNER_CAPS`` and ``runner_limits(concurrency)`` (the run
request's ``limits`` object of runner/main.mjs), ``SCRIPT_MAX`` with ``script_too_large(source)``,
``AGENT_TIMEOUT`` (seconds per agent), ``cut(planned_labels, guideline)`` (REQ-019), ``budget_total(text,
param)`` (REQ-021), ``large_warning(agents, tokens)`` (REQ-022) and ``IdleWatch`` (REQ-031). REQ-020's
item-cap error itself is the vendored runtime's; this module only passes the cap.
"""

from __future__ import annotations

import os
import re
import time
from typing import Callable

MAX_CONCURRENCY = 16
RUNNER_CAPS = {"agents": 1000, "items": 4096, "nested": 256}
SCRIPT_MAX = 512 * 1024
AGENT_TIMEOUT = 1800
GUIDELINE = 10
LARGE_AGENTS = 25
LARGE_TOKENS = 1_500_000
IDLE_LIMIT = 1800

_DIRECTIVE = re.compile(r"(?:^|\s)\+(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)([kKmM]?)(?=\s|$|[.,;!?](?!\d))")
_SCALE = {"": 1, "k": 1_000, "m": 1_000_000}


def concurrency(configured: int | None = None, cpus: int | None = None, limiter_cap: int | None = None) -> int:
    """``configured`` if set, else min(16, CPUs - 2); at least 1 and never above the subagents limiter's cap."""
    if configured is not None:
        value = int(configured)
    else:
        value = min(MAX_CONCURRENCY, (cpus if cpus is not None else os.cpu_count() or 1) - 2)
    if limiter_cap:
        value = min(value, int(limiter_cap))
    return max(1, value)


def runner_limits(concurrency: int) -> dict:
    return {"concurrency": int(concurrency), **RUNNER_CAPS}


def script_too_large(source: str) -> str | None:
    size = len(source.encode("utf-8"))
    if size <= SCRIPT_MAX:
        return None
    return "refused: the script is %d bytes, over the 512 KiB (%d bytes) cap" % (size, SCRIPT_MAX)


def cut(planned_labels: list, guideline: int = GUIDELINE) -> tuple[list, list, str]:
    """Keep the first ``guideline`` agents in script order; the log line names every dropped label."""
    kept, dropped = list(planned_labels[:guideline]), list(planned_labels[guideline:])
    if not dropped:
        return kept, dropped, ""
    line = "cut to the guideline of %d agents with no answer to the size question; dropped %d: %s" % (
        guideline, len(dropped), ", ".join(str(label) for label in dropped))
    return kept, dropped, line


def budget_total(text: str, param: int | None = None) -> int | None:
    """A run parameter wins; else a ``+N`` directive (``+500k``, ``+1.5m``, ``+20000``); else None."""
    if param is not None:
        return int(param)
    match = _DIRECTIVE.search(text or "")
    if not match:
        return None
    return int(round(float(match.group(1).replace(",", "")) * _SCALE[match.group(2).lower()]))


def large_warning(agents: int, tokens: int) -> str | None:
    if agents <= LARGE_AGENTS and tokens <= LARGE_TOKENS:
        return None
    return "large workflow: %d agents, ~%s tokens (over %d agents or %s tokens)" % (
        agents, "{:,}".format(int(tokens)), LARGE_AGENTS, "{:,}".format(LARGE_TOKENS))


class IdleWatch:
    """Expires once ``limit`` seconds pass without ``touch()`` (an agent call)."""

    def __init__(self, limit: float = IDLE_LIMIT, clock: Callable[[], float] | None = None) -> None:
        # the clock is read at call time, so a patched time.monotonic is honoured
        self._clock = clock or (lambda: time.monotonic())
        self.limit = limit
        self._last = self._clock()

    def touch(self) -> None:
        self._last = self._clock()

    def expired(self) -> bool:
        return self._clock() - self._last >= self.limit

    def reason(self) -> str:
        return "stopped: no agent call for %d minutes" % (self.limit // 60)
