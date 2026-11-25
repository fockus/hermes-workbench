"""Progress watchdog for long runs (compaction-quality design C6; REQ-019, REQ-020, REQ-021).

One ``Watchdog`` watches one run. ``on_turn`` is called once per model turn with that turn's
signals. There are three progress signals: a todo revision change, a file mutation, and a tool
result not seen among the last ``RECENT_RESULTS`` results.

- Stuck: no progress for ``stuck_turns`` turns or ``stuck_seconds`` seconds. The run stops with a
  report naming the last progress and the latest result, and a kanban task is blocked with it.
- Checkpoint: at 80 % of ``run_budget_seconds`` a progressing run writes a handoff and then
  continues in a new run, once.
- Day limit: 24 h after ``task_started_at`` (the task's first run) the run stops with its state.

Side effects go through ``effects`` and time comes from ``clock``, so this module needs no Hermes.
"""
from __future__ import annotations

import hashlib
from collections import deque
from typing import Callable, Iterable, Optional, Protocol

DAY_SECONDS = 24 * 3600
CHECKPOINT_SHARE = 0.8
RECENT_RESULTS = 50
QUOTE_CHARS = 200


class Effects(Protocol):
    def stop(self, report: str) -> None: ...

    def block_task(self, task_id: str, report: str) -> None: ...

    def write_handoff(self) -> None: ...

    def continue_in_new_run(self) -> None: ...


def _quote(text: str) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= QUOTE_CHARS else flat[:QUOTE_CHARS] + " …"


class Watchdog:
    """Progress tracking for one run; see the module docstring."""

    def __init__(self, *, clock: Callable[[], float], effects: Effects, run_budget_seconds: Optional[float],
                 stuck_turns: int = 8, stuck_seconds: float = 1800, kanban_task_id: Optional[str] = None,
                 task_started_at: Optional[float] = None):
        self._clock, self._effects = clock, effects
        self._budget, self._stuck_turns, self._stuck_seconds = run_budget_seconds, stuck_turns, stuck_seconds
        self._kanban_task_id = kanban_task_id
        now = clock()
        self._run_started = now
        self._task_started = now if task_started_at is None else task_started_at
        self._progress_at, self._progress, self._progress_turn = now, "the run started", 0
        self._turn = self._idle = 0
        self._revision = None
        self._recent: deque = deque(maxlen=RECENT_RESULTS)
        self._latest = None
        self._checkpointed = False
        self.stopped = False

    def on_turn(self, *, todo_revision=None, file_mutated: bool = False, tool_results: Iterable = ()) -> None:
        if self.stopped:
            return
        now = self._clock()
        self._turn += 1
        found = self._progress_signal(todo_revision, file_mutated, tool_results)
        if found:
            self._progress, self._progress_at, self._progress_turn, self._idle = found, now, self._turn, 0
        else:
            self._idle += 1
        if now - self._task_started >= DAY_SECONDS:
            self._halt("Stopped: the task has run %.1f h in total (limit 24 h). %s"
                       % ((now - self._task_started) / 3600, self._state(now)))
        elif self._idle >= self._stuck_turns or now - self._progress_at >= self._stuck_seconds:
            self._halt("Stopped as stuck: no progress for %d turn(s) / %d min. %s"
                       % (self._idle, (now - self._progress_at) // 60, self._state(now)))
        elif self._budget and not self._checkpointed and now - self._run_started >= CHECKPOINT_SHARE * self._budget:
            self._checkpointed = True
            self._effects.write_handoff()
            self._effects.continue_in_new_run()

    def _progress_signal(self, revision, mutated: bool, results: Iterable) -> Optional[str]:
        found = []
        if mutated:
            found.append("a file edit")
        if revision is not None and revision != self._revision:
            self._revision = revision
            found.append("todo list revision %s" % revision)
        for result in results:  # only a digest and a bounded excerpt are kept, never the result
            self._latest = _quote(result)
            key = hashlib.sha1(str(result).encode("utf-8", "replace")).digest()
            if key not in self._recent:
                found.append("new tool result: %s" % self._latest)
            self._recent.append(key)
        return "; ".join(found)

    def _state(self, now: float) -> str:
        latest = "; latest tool result: %s" % self._latest if self._latest is not None else ""
        return ("Last progress at turn %d (%d min ago): %s%s. Run turn %d, run time %d min."
                % (self._progress_turn, (now - self._progress_at) // 60, self._progress, latest,
                   self._turn, (now - self._run_started) // 60))

    def _halt(self, report: str) -> None:
        self.stopped = True
        self._effects.stop(report)
        if self._kanban_task_id:
            self._effects.block_task(self._kanban_task_id, report)
