"""One delegate_task call's tasks, each by its final stock position (SPIKE.md § 2; Astra review round 1).

The parent's ``pre_tool_call`` stages the call: one slot per task of the final arguments, in their order.
Nothing in a staged call is committed: another hook may still veto it. Stock runs ``route_task`` only for a
call it executes, once per task with that task's ``index`` (tools/delegate_tool_routes.py:308); that is
where a slot is admitted by the limiter, queued or refused (``Calls.route``). Stock then builds a child for
every task its route did not refuse, in task order, and fires ``subagent_start`` with a subagent id that
carries the task index (``sa-<index>-<hex>``, tools/delegate_tool.py:245). ``Calls.start`` binds the start to
the slot at that index. A start that cannot be tied to exactly one slot is bound to ``DENIED`` (no tools):
it fails closed, never open. A call ends at its ``transform_tool_result`` (``Calls.close``).

Only calls that never executed (another hook vetoed them) are ever dropped to bound memory; a call whose
tasks were routed is kept until its result.
"""

from __future__ import annotations

import contextvars
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .callers import Spawn
from .registry import AgentDef

STAGED_LIMIT = 256  # unexecuted (vetoed) calls kept; only those are ever dropped
SA_ID_RE = re.compile(r"sa-(\d+)-[0-9a-f]{8}")
VIEW = ("goal", "context", "agent_type", "name", "effort", "model_alias")  # what route_task sees of a task
# (session, call id) of the API's call while its delegate_task runs (stock routes and starts inside it); children
# run in copies of that context, so it narrows only that session's calls. Empty for a model's calls.
CALL: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar("subagents_call", default=("", ""))
STAGED, ROUTED, QUEUED, REFUSED, STARTED = "staged", "routed", "queued", "refused", "started"

DENIED = AgentDef("unidentified", "a child whose spawn could not be identified", "", frozenset(), frozenset(),
                  None, None, False, False, Path(__file__))


def action_of(args: Any) -> str:
    """delegate_task's action exactly as stock normalizes it (tools/delegate_tool.py: ``.strip().lower()``)."""
    action = args.get("action") if isinstance(args, dict) else None
    return action.strip().lower() if isinstance(action, str) else ""


def _ours(call: Call) -> bool:
    session, call_id = CALL.get()
    return not call_id or call.session_id != session or call.call_id == call_id


def view_of(task: Any) -> tuple[Any, ...]:
    return tuple((task.get(k) or None) if isinstance(task, dict) else None for k in VIEW)


@dataclass
class Slot:
    index: int
    task: dict[str, Any]
    spawn: Spawn
    state: str = STAGED
    lease: str | None = None      # the limiter id this task holds from its route until its child stops
    child: str | None = None      # the child session bound to it

    @property
    def goal(self) -> str:
        return str(self.task.get("goal") or "")


@dataclass
class Call:
    session_id: str
    turn_id: str
    call_id: str
    slots: list[Slot]
    executed: bool = False
    meta: dict[str, Any] = field(default_factory=dict)  # the launcher's notes (queued launches, relaunch)


class Calls:
    """The staged and executing delegate_task calls of this process."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._calls: list[Call] = []
        self._bound: dict[str, Slot] = {}

    def stage(self, call: Call) -> None:
        with self.lock:
            self._calls.append(call)
            staged = [c for c in self._calls if not c.executed]
            for dropped in staged[:max(0, len(staged) - STAGED_LIMIT)]:
                self._calls.remove(dropped)

    def peek(self, session_id: str, turn_id: str, index: Any, task: Any) -> Slot | None:
        """The staged slot a route belongs to, unchanged (its definition snapshot is what the route uses)."""
        with self.lock:
            hit = self._match(session_id, turn_id, index, task)
        return hit[1] if hit is not None else None

    def route(self, session_id: str, turn_id: str, index: Any, task: Any) -> tuple[Call, Slot] | None:
        """The slot stock is routing now (an executing call), or None when this route belongs to no staged call."""
        with self.lock:
            hit = self._match(session_id, turn_id, index, task)
            if hit is None:
                return None
            call, slot = hit
            call.executed = True
            slot.state = ROUTED
            return call, slot

    def _match(self, session_id: str, turn_id: str, index: Any, task: Any) -> tuple[Call, Slot] | None:
        view = view_of(task)
        found = [(c, s) for c in self._calls if (c.session_id, c.turn_id) == (session_id, turn_id) and _ours(c)
                 for s in c.slots if s.index == index and s.state == STAGED and view_of(s.task) == view]
        # an executing call first; else the newest staged one (stock executes a call right after its hooks, so an
        # older unexecuted one was vetoed)
        return next(((c, s) for c, s in found if c.executed), found[-1]) if found else None

    def start(self, session_id: str, goal: str, subagent_id: str, child_session: str = "") -> Slot | AgentDef | None:
        """The slot a stock ``subagent_start`` belongs to; ``DENIED`` when it cannot be told apart; None when no
        executed route of that parent asked for it (an internal or continued start). Only a slot its executed route
        matched binds (judge-A G4): a staged slot stock never routed through this provider never does."""
        match = SA_ID_RE.fullmatch(subagent_id or "")
        with self.lock:
            candidates = [(c, s) for c in self._calls if c.session_id == session_id and _ours(c) for s in c.slots
                          if s.state == ROUTED and s.goal == goal]
            if not candidates:
                return None
            if match is not None:
                candidates = [(c, s) for c, s in candidates if s.index == int(match.group(1))]
            if len({(s.spawn.agent, s.spawn.name, s.spawn.schema) for _, s in candidates}) != 1:
                return DENIED  # no slot, or several that differ: never guess a policy
            slot = candidates[0][1]
            slot.state, slot.child = STARTED, child_session
            self._bound[child_session] = slot
            return slot

    def bound(self, child_session: str) -> Slot | None:
        """The slot a child just started from (taken once, by the launcher at the same subagent_start)."""
        with self.lock:
            return self._bound.pop(child_session, None)

    def close(self, session_id: str, call_id: str = "") -> Call | None:
        """The end of a call (its tool result): removed and returned. A supplied id closes that call only; without
        one, the session's only executed call (never a guess among several)."""
        with self.lock:
            mine = [c for c in self._calls if c.session_id == session_id and c.executed]
            hit = [c for c in mine if c.call_id == call_id] if call_id else (mine if len(mine) == 1 else [])
            if not hit:
                return None
            self._calls.remove(hit[0])
            return hit[0]

    def drop(self, session_id: str, call_id: str) -> None:
        """A call stock reported blocked or cancelled before it ran: its slots never route or bind."""
        with self.lock:
            self._calls = [c for c in self._calls if c.executed or (c.session_id, c.call_id) != (session_id, call_id)]

    def find(self, session_id: str, call_id: str) -> Call | None:
        with self.lock:
            return next((c for c in self._calls if (c.session_id, c.call_id) == (session_id, call_id)), None)
