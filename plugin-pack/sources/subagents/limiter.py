"""One limiter for every agent of the Hermes process — subagents, workflow, team and harness agents
(REQ-016, D-07): 8 running, 4 per provider, the rest queued in kanban.db and started first-in
first-out as soon as their provider has room, at most 1000 agents per root task.

The in-memory state is authoritative for this process; the ``subagents_queue`` rows make waiting
launches visible on the board and are removed when a launch starts or is dropped.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .kanban_store import KanbanStore, current_board, get_store

MAX_RUNNING, PER_PROVIDER, PER_ROOT = 8, 4, 1000
RUNNING, QUEUED, REFUSED, FULL = "running", "queued", "refused", "full"


@dataclass(frozen=True)
class _Launch:
    agent_id: str
    provider: str
    board: Path | None = None  # the board its queue row was written to


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # alive, owned by another user
    return True


class Limiter:
    """``acquire`` admits, queues or refuses a launch; ``release`` frees its slot and returns the ids
    started from the queue."""

    def __init__(self, store: KanbanStore, max_running: int = MAX_RUNNING, per_provider: int = PER_PROVIDER,
                 per_root: int = PER_ROOT):
        self.store = store
        self.max_running, self.per_provider, self.per_root = max_running, per_provider, per_root
        self._lock = threading.Lock()
        self._running: dict[str, str] = {}
        self._queue: list[_Launch] = []
        self._roots: dict[str, int] = {}
        self._drop_rows_of_dead_processes()

    def acquire(self, agent_id: str, *, provider: str | None, root_task: str | None = None,
                queue_only: bool = False, wait: bool = True, force: bool = False) -> str:
        """``queue_only`` queues even with room: the caller already gave that room to launches not started yet.
        ``wait=False`` never queues: no room answers FULL and takes nothing. ``force`` counts a launch that
        already runs (stock started it where nothing could refuse it) even past the caps."""
        provider = provider or ""
        with self._lock:
            if agent_id in self._running:
                return RUNNING
            if any(launch.agent_id == agent_id for launch in self._queue):
                return QUEUED
            if root_task is not None and self._roots.get(root_task, 0) >= self.per_root and not force:
                return REFUSED
            fits = force or (not queue_only and self._fits(provider))
            if not (fits or wait):
                return FULL
            if root_task is not None:
                self._roots[root_task] = self._roots.get(root_task, 0) + 1
            if fits:
                self._running[agent_id] = provider
                return RUNNING
            board = current_board()
            self._queue.append(_Launch(agent_id, provider, board))
            # under the limiter lock: a launch's queue row is written and deleted in admission order
            self.store.write("INSERT OR REPLACE INTO subagents_queue VALUES (?, ?, ?, ?, ?)",
                             (agent_id, provider, root_task or "", os.getpid(), time.time()), board=board)
        return QUEUED

    def release(self, agent_id: str) -> list[str]:
        with self._lock:
            dropped = [launch for launch in self._queue if launch.agent_id == agent_id]
            self._queue = [launch for launch in self._queue if launch.agent_id != agent_id]
            self._running.pop(agent_id, None)
            started = []
            for launch in list(self._queue):
                if self._fits(launch.provider):
                    self._queue.remove(launch)
                    self._running[launch.agent_id] = launch.provider
                    started.append(launch)
            for gone in [*dropped, *started]:  # on the board each row was queued on, in order
                self.store.write("DELETE FROM subagents_queue WHERE agent_id = ?", (gone.agent_id,), board=gone.board)
        return [launch.agent_id for launch in started]

    def drain(self) -> list[str]:
        """Starts the queued launches that fit now; their ids, as ``release`` returns them."""
        return self.release("")

    def preview(self, provider: str | None, *, root_task: str | None = None, ahead: Sequence[str] = ()) -> str:
        """What ``acquire`` would answer now, with ``ahead`` (their providers) admitted first; takes no slot."""
        provider = provider or ""
        with self._lock:
            if root_task is not None and self._roots.get(root_task, 0) + len(ahead) >= self.per_root:
                return REFUSED
            running = [*self._running.values(), *ahead]
        return RUNNING if len(running) < self.max_running and _same(running, provider) < self.per_provider else QUEUED

    def provider_of(self, agent_id: str) -> str:
        """The provider a running launch is counted on ("" when unknown)."""
        with self._lock:
            return self._running.get(agent_id, "")

    def position(self, agent_id: str) -> int | None:
        """1-based place of a queued launch, None when it is not queued."""
        with self._lock:
            ids = [launch.agent_id for launch in self._queue]
        return ids.index(agent_id) + 1 if agent_id in ids else None

    def _fits(self, provider: str) -> bool:
        running = list(self._running.values())
        return len(running) < self.max_running and _same(running, provider) < self.per_provider

    def _drop_rows_of_dead_processes(self) -> None:
        """A crashed process leaves queue rows nobody will start."""
        pids = {row[0] for row in self.store.read("SELECT DISTINCT pid FROM subagents_queue")}
        for pid in pids:
            if isinstance(pid, int) and pid != os.getpid() and not _alive(pid):
                self.store.write("DELETE FROM subagents_queue WHERE pid = ?", (pid,))


# Hermes imports this plugin once per Hermes home of the process, each under its own package name
# (hermes_cli/plugins_loader.py:795-803), so module globals are per home. The capacity state lives on one
# sys.modules entry every load finds; queued launches keep a reference to the load that queued them (launch.py).
PROCESS_KEY = "subagents._process"


def process_shared(attr: str, factory: Callable[[], Any]) -> Any:
    """The one ``attr`` of this process, made by the first plugin load that asks for it."""
    holder = sys.modules.setdefault(PROCESS_KEY, types.ModuleType(PROCESS_KEY))
    lock = holder.__dict__.setdefault("lock", threading.Lock())  # dict.setdefault is atomic
    with lock:
        if attr not in holder.__dict__:
            setattr(holder, attr, factory())
        return getattr(holder, attr)


def get_limiter() -> Limiter:
    """The process limiter shared by every caller (delegate_task, the API, teams, harness agents) and every
    Hermes home loaded in this process."""
    return process_shared("limiter", lambda: Limiter(get_store()))


def _same(running: Sequence[str], provider: str) -> int:
    """Launches that may be on ``provider``: an unknown one ("") may be on any provider, so it counts against every
    candidate, and a launch of unknown provider counts as on the busiest one (judge-A G12)."""
    unknown = sum(not p for p in running)
    if provider:
        return unknown + sum(p == provider for p in running)
    return unknown + max((sum(p == q for p in running) for q in set(running) if q), default=0)
