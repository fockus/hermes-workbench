"""Agent names (REQ-018) and restart recovery (REQ-034, SPIKE.md § 8).

A name given in a delegate_task task is bound to the child's subagent id at ``subagent_start``
(the caller map already knows the name) and kept in kanban.db with the owner session and a state:
running, idle, resumed or lost. ``pre_tool_call`` rewrites a ``continue``/``steer`` call that names
an agent to its subagent id. Rows left running by an earlier plugin load are checked once against
the stock recovery store: a re-fired ``subagent_start`` marks them resumed, no continuable record
marks them lost; the main agent sees that in its next model call.

Each name keeps the agent that spawned it (captured at the spawn, callers.py), which stock's
``steer``/``continue`` take as ``parent_agent``. The main agent of every session is also recorded at its
own unfenced hooks (stock ``get_active_subagent_parent()``), so after a restart, when no spawning agent
survives, the recorded main agent of the agent's own session continues it (SPIKE.md § 7); never another one.

A name belongs to one root conversation (Astra review round 1, messages.py:96): its rows live in the profile's
board database (kanban.db) and carry the root session; every lookup, listing and rewrite is made in the caller's
root conversation. An active name (running or resumed) cannot be given again there, and ``main`` is reserved.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from .callers import CallerMap, active_parent, agent_ref
from .calls import action_of
from .kanban_store import KanbanStore

logger = logging.getLogger("subagents.names")

RUNNING, IDLE, RESUMED, LOST = "running", "idle", "resumed", "lost"
LIVE = (RUNNING, RESUMED)
MAINS_KEPT = 256  # main sessions whose agent is remembered (a gateway serves many)
REWRITTEN = ("continue", "steer")
RESERVED = "main"  # send_message(to="main") reaches the main agent
COLUMNS = "name, subagent_id, owner_session, state, root"
# a row is replaced only by its own agent or over an inactive one: never over another agent's active claim
UPSERT = ("INSERT INTO subagents_names (%s, updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (root, name) DO UPDATE "
          "SET subagent_id = excluded.subagent_id, owner_session = excluded.owner_session, state = excluded.state, "
          "updated_at = excluded.updated_at WHERE subagents_names.subagent_id = excluded.subagent_id OR "
          "subagents_names.state NOT IN ('running', 'resumed')" % COLUMNS)
# Stock states `action='continue'` can rebuild from (tools/delegate_tool_resumable.py:31-32).
CONTINUABLE = ("resumable", "completed", "stopped", "superseded", "claimed", "running")


@dataclass(frozen=True)
class NamedAgent:
    name: str
    subagent_id: str
    owner_session: str
    state: str
    root: str = ""  # the root conversation the name belongs to
    owner_ref: Callable[[], Any] | None = None  # the agent that spawned it, while it is alive


def continuable_ids() -> set[str] | None:
    """Subagent ids stock recovery can still continue; read-only, empty when the store or its table is absent, None
    when the store cannot be read (a failed read is never "nothing is continuable")."""
    from hermes_constants import get_hermes_home

    path = Path(get_hermes_home()) / "state.db"
    if not path.is_file():
        return set()
    marks = ",".join("?" * len(CONTINUABLE))
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            rows = conn.execute("SELECT subagent_id FROM resumable_subagents WHERE state IN (%s)" % marks,
                                CONTINUABLE).fetchall()
    except sqlite3.OperationalError as exc:
        return set() if "no such table" in str(exc) else None
    except sqlite3.Error:
        return None
    return {row[0] for row in rows}


class Names:
    """Hook handlers: subagent_start/subagent_stop, on_session_start, pre_tool_call, pre_llm_call."""

    def __init__(self, store: KanbanStore, callers: CallerMap):
        self.store = store
        self.callers = callers
        self._lock = threading.Lock()
        self._by_name: dict[tuple[str, str, str], NamedAgent] = {}  # (board, root, name)
        self._started: set[str] = set()  # subagent ids started under this plugin load
        self._checked: set[str] = set()  # boards reconciled under this plugin load
        # the first CLI main session after a restart also sees what the restart changed (a classic CLI process is
        # one conversation); in a gateway a notice reaches only the conversation that owns the agent
        self._heir = ""
        self._report: dict[str, dict[str, str]] = {}  # root conversation -> name -> state
        self._mains: dict[str, Callable[[], Any]] = {}  # main session -> its agent, newest last

    def main_agent(self, session_id: str) -> Any:
        """The recorded main agent of ``session_id`` while it is alive, else None (never another session's)."""
        with self._lock:
            ref = self._mains.get(session_id)
        return ref() if ref is not None else None

    def root_of(self, session_id: str) -> str:
        """The root conversation of a session: the main session its spawn chain starts from."""
        return self.callers.root_of(session_id)

    def sole_root(self) -> str:
        """The one conversation this plugin load knows, or "" when there are none or several (a gateway)."""
        with self._lock:
            roots = {key[1] for key in self._by_name} | set(self._mains)
        return next(iter(roots)) if len(roots) == 1 else ""

    def _record_main(self, session_id: str) -> None:
        agent = active_parent()
        if agent is not None and session_id:
            with self._lock:
                self._mains.pop(session_id, None)
                self._mains[session_id] = agent_ref(agent)
                while len(self._mains) > MAINS_KEPT:
                    self._mains.pop(next(iter(self._mains)))

    def resolve(self, name: str, root: str) -> NamedAgent | None:
        """The agent of that name in the root conversation ``root``; None in any other one."""
        if not root:
            return None
        with self._lock:
            known = self._by_name.get((_board(), root, name))
        if known is not None:
            return known
        rows = self.store.read("SELECT %s FROM subagents_names WHERE root = ? AND name = ?" % COLUMNS, (root, name))
        return NamedAgent(*rows[0]) if rows else None

    def known(self, root: str) -> list[str]:
        rows = self.store.read("SELECT name FROM subagents_names WHERE root = ? ORDER BY name", (root,)) if root else []
        board = _board()
        with self._lock:
            mine = {key[2] for key in self._by_name if key[:2] == (board, root)}
        return sorted({row[0] for row in rows} | mine)

    def refusal(self, name: Any, root: str) -> str | None:
        """Why a spawn may not take ``name`` in ``root``: reserved, or held by an active agent there."""
        if not isinstance(name, str):
            return None
        if name.strip().lower() == RESERVED:
            return "Not started: the agent name %r is reserved for the main agent; choose another." % name
        held = self.resolve(name, root)
        if held is not None and held.state in LIVE:
            return ("Not started: an active agent is already named %r (%s) in this conversation; message it with "
                    "send_message or choose another name." % (name, held.subagent_id))
        return None

    def subagent_start(self, parent_session_id: str = "", child_session_id: str = "", child_subagent_id: str = "",
                       **_: Any) -> None:
        if not child_subagent_id:
            return
        child = self.callers.get(child_session_id)
        ref = child.owner if child is not None and child.owner() is not None else None
        restored = None if child_subagent_id in self._started else self._by_subagent_row(child_subagent_id)
        self._started.add(child_subagent_id)
        if restored is not None:  # first start under this load of an agent an earlier load named
            recovered = restored.state != IDLE
            root = self.root_of(parent_session_id) or restored.root
            if root != restored.root:  # stock resumed it for another conversation (it checks ownership): it moves
                self._drop(restored)
                restored = replace(restored, root=root, owner_session=parent_session_id)
            self._set(replace(restored, state=RESUMED if recovered else RUNNING, owner_ref=ref), report=recovered)
        elif child is not None and child.name:
            root = self.root_of(parent_session_id)
            held = self.resolve(child.name, root)
            if (held is not None and held.state in LIVE and held.subagent_id != child_subagent_id) or not self._set(
                    NamedAgent(child.name, child_subagent_id, parent_session_id, RUNNING, root, ref), claim=True):
                logger.warning("subagents: the name %r is held by another active agent; child %s runs unnamed",
                               child.name, child_subagent_id)

    def label(self, session_id: str) -> str | None:
        """How a child signs its messages: its name only while that name is bound to it, else its subagent id;
        None for a main session."""
        child = self.callers.get(session_id)
        if child is None:
            return None
        named = self.resolve(child.name, self.root_of(session_id)) if child.name else None
        return child.name if named is not None and named.subagent_id == child.subagent_id else (
            child.subagent_id or session_id)

    def subagent_stop(self, child_session_id: str = "", **_: Any) -> None:
        child = self.callers.get(child_session_id)
        named = self.resolve(child.name, self.root_of(child_session_id)) if child and child.name else None
        if named is not None and named.subagent_id == child.subagent_id:
            self._set(replace(named, state=IDLE))

    def discard(self, child_session_id: str) -> None:
        """A child stock built but closed before it ran: its name is free again, nothing can continue it."""
        child = self.callers.get(child_session_id)
        named = self.resolve(child.name, self.root_of(child_session_id)) if child and child.name else None
        if named is not None and named.subagent_id == child.subagent_id:
            self._drop(named)

    def on_session_start(self, session_id: str = "", platform: Any = "", **_: Any) -> None:
        """Once per board and plugin load: agents an earlier load left running are resumed by stock or lost. The board
        counts as checked only after both reads succeeded; a failed one is tried again at the next session start."""
        board = _board()
        if self.callers.get(session_id) is not None or board in self._checked:
            return
        continuable = continuable_ids()
        try:
            rows = self.store.read("SELECT %s FROM subagents_names WHERE state IN (?, ?)" % COLUMNS, LIVE, strict=True)
        except (sqlite3.Error, OSError) as exc:
            rows = None
            logger.warning("subagents: cannot read the agent names of %s (%s); checked again later", board, exc)
        if continuable is None or rows is None:
            logger.warning("subagents: restart recovery of %s postponed: a store could not be read", board)
            return
        self._checked.add(board)
        self._heir = session_id if platform == "cli" else ""
        for row in rows:
            agent = NamedAgent(*row)
            if agent.subagent_id not in self._started and agent.subagent_id not in continuable:
                self._set(replace(agent, state=LOST), report=True)

    def pre_tool_call(self, tool_name: str = "", args: Any = None, session_id: str = "", **_: Any) -> dict | None:
        if self.callers.get(session_id) is None:
            self._record_main(session_id)
            self.store.flush()
        if tool_name != "delegate_task" or not isinstance(args, dict):
            return None
        root = self.root_of(session_id)
        if action_of(args) in ("", "spawn"):
            return self._spawn_refusal(args, root)
        if action_of(args) not in REWRITTEN:
            return None
        target = args.get("subagent_id")
        named = self.resolve(target, root) if isinstance(target, str) and target else None
        if named is None or named.subagent_id == target:
            return None
        return {"action": "modify", "args": {"subagent_id": named.subagent_id}}

    def _spawn_refusal(self, args: dict[str, Any], root: str) -> dict[str, str] | None:
        tasks = args.get("tasks")
        names = [t.get("name") for t in tasks if isinstance(t, dict)] if isinstance(tasks, list) else []
        given = [n for n in names if isinstance(n, str)]
        twice = next((n for n in given if given.count(n) > 1), None)
        if twice is not None:
            return {"action": "block", "message": "Not started: the agent name %r is given twice; names are "
                                                  "unique in a conversation." % twice}
        reason = next((r for r in (self.refusal(n, root) for n in given) if r), None)
        return {"action": "block", "message": reason} if reason else None

    def pre_llm_call(self, session_id: str = "", **_: Any) -> dict[str, str] | None:
        """The main agent's next call: deferred kanban writes land, its own recovery changes are shown once."""
        if self.callers.get(session_id) is not None:
            return None
        self._record_main(session_id)
        self.store.flush()
        with self._lock:
            report = self._report.pop(session_id, {})
        if not report:
            return None
        lines = ["- %s: %s" % (name, "resumed by stock recovery" if state == RESUMED else
                               "lost (no stock recovery record); spawn it again if still needed")
                 for name, state in sorted(report.items())]
        return {"context": "Orchestrator console - agents after the restart:\n" + "\n".join(lines)}

    def _by_subagent_row(self, subagent_id: str) -> NamedAgent | None:
        rows = self.store.read("SELECT %s FROM subagents_names WHERE subagent_id = ? ORDER BY updated_at DESC "
                               "LIMIT 1" % COLUMNS, (subagent_id,))
        return NamedAgent(*rows[0]) if rows else None

    def _set(self, agent: NamedAgent, report: bool = False, claim: bool = False) -> bool:
        """Records ``agent``; a ``claim`` is refused (False) when another active agent holds the name, checked and
        taken under one lock here, and the persisted row never replaces another agent's active claim."""
        key = (_board(), agent.root, agent.name)
        with self._lock:
            previous = self._by_name.get(key)
            if claim and previous is not None and previous.state in LIVE and previous.subagent_id != agent.subagent_id:
                return False
            if agent.owner_ref is None and previous is not None and previous.subagent_id == agent.subagent_id:
                agent = replace(agent, owner_ref=previous.owner_ref)
            self._by_name[key] = agent
            if report:
                for session in dict.fromkeys((agent.root, self._heir)):
                    if session:
                        self._report.setdefault(session, {})[agent.name] = agent.state
        self.store.write(UPSERT, (agent.name, agent.subagent_id, agent.owner_session, agent.state, agent.root, time.time()))
        return True

    def _drop(self, agent: NamedAgent) -> None:
        with self._lock:
            self._by_name.pop((_board(), agent.root, agent.name), None)
        self.store.write("DELETE FROM subagents_names WHERE root = ? AND name = ?", (agent.root, agent.name))


def _board() -> str:
    """The profile's current board database: names of different boards or profiles never meet."""
    from hermes_cli.kanban_db import kanban_db_path

    return str(kanban_db_path())
