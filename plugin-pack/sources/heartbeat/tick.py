"""``hermes heartbeat tick``: one pass over every kanban board, without a model (REQ-001, NFR-001).

A non-blocking ``fcntl`` lock on ``<Hermes root>/heartbeat/tick.lock`` makes an overlapping tick print
``skipped`` and return at once. Then the contract probe runs (NFR-005), and for each board of
``kanban_db_boards.list_boards`` the four ports run in order: detect -> route -> act, then deliver once.

Ports (injected, so the policy and question tracks plug in without touching the loop):

* ``detect(conn, board, tick) -> list[Finding]`` - the stuck-task rules (detect.py, loaded on the first board);
* ``route(findings, tick) -> list[Decision]`` - the policy table (policy.route);
* ``act(conn, decisions, tick) -> None`` - the single executor (act.act);
* ``deliver(tick) -> list[str]`` - the question lines and digest printed for the cron job (questions.deliver).

Board state is read through stock ``hermes_cli.kanban_db``; nothing here imports a model client.
"""

from __future__ import annotations

import contextlib
import fcntl
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import act as executor
from . import policy, probe, questions
from .store import Store

SKIPPED = "skipped: the previous heartbeat tick is still running"


@dataclass(frozen=True)
class Tick:
    """What every port of one tick sees."""

    ctx: Any
    store: Store
    now: float
    disabled: frozenset = field(default_factory=frozenset)


def no_route(findings: list, tick: Tick) -> list:
    return []


def no_act(conn, decisions: list, tick: Tick) -> None:
    return None


def no_deliver(tick: Tick) -> list[str]:
    return []


def detect(conn, board: str, tick: Tick) -> list:
    from . import detect as rules

    return rules.scan(conn, board, tick)


@dataclass(frozen=True)
class Ports:
    detect: Callable[..., list] = detect
    route: Callable[..., list] = policy.route
    act: Callable[..., None] = executor.act
    deliver: Callable[..., list] = questions.deliver


def state_dir() -> Path:
    from hermes_constants import get_default_hermes_root

    # the Hermes root, not the profile home: boards are shared by every profile and so is their store
    path = get_default_hermes_root() / "heartbeat"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _boards(disabled: frozenset) -> list[str]:
    import hermes_cli.kanban_db as kb

    if "boards" in disabled:  # list_boards changed shape: the default board is still readable
        return [kb.DEFAULT_BOARD]
    import hermes_cli.kanban_db_boards as boards

    return [meta["slug"] for meta in boards.list_boards(include_archived=False)]


@contextlib.contextmanager
def _open_board(slug: str):
    """The board's connection, or None when it has no kanban.db yet (an empty tick creates nothing)."""
    import hermes_cli.kanban_db as kb
    from hermes_cli.kanban_db_connect import connect

    # explicit slug, 'default' too: board=None would resolve to the user's current board
    if not kb.kanban_db_path(slug).is_file():
        yield None
        return
    with contextlib.closing(connect(board=slug)) as conn:
        yield conn


def _pass(ctx, ports: Ports, state: Path) -> list[str]:
    import hermes_cli.kanban_db as kb

    result = probe.check()
    tick = Tick(ctx, Store(state / "heartbeat.db"), time.time(), result.disabled)
    lines = [result.warning] if result.warning else []
    seen: set[Path] = set()
    # the slug is an iteration variable, not user intent: a HERMES_KANBAN_DB pin maps every slug to one file
    with kb.pin_first_board_resolution():
        for slug in _boards(result.disabled):
            path = kb.kanban_db_path(slug)
            if path in seen:
                continue
            seen.add(path)
            with _open_board(slug) as conn:
                if conn is not None:
                    ports.act(conn, ports.route(ports.detect(conn, slug, tick), tick), tick)
                    questions.adopt(conn, slug, tick)
    lines.extend(ports.deliver(tick))
    return lines


def run(ctx, ports: Ports | None = None) -> list[str]:
    """One tick; returns the lines to print (the cron job delivers them)."""
    state = state_dir()
    with open(state / "tick.lock", "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return [SKIPPED]
        return _pass(ctx, ports or Ports(), state)


def main(ctx) -> None:
    for line in run(ctx):
        print(line)
