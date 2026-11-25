"""Who is calling (SPIKE.md § 2): the caller map that binds a child session to what its parent's spawn
asked for, plus the spawning agent itself.

Stock fires ``subagent_start`` inside ``_build_child_agent`` (tools/delegate_tool.py:378-386), before
it sets the child's output schema and before ``_register_child`` puts the child into the live
registry, so nothing about the child can be read from stock at that point. Everything is taken from
the parent's own ``pre_tool_call`` on delegate_task instead: the type and name, whether the task
carries an output schema, and the spawning agent (stock ``get_active_subagent_parent()``, bound for
the parent's whole turn by agent/turn_facade.py:144). Which task a ``subagent_start`` belongs to is
decided by calls.py (by the task's final stock position); this map binds the child's session to it. A
child of a typed agent is capped at what its spawner may call (``capped``).
"""

from __future__ import annotations

import threading
import weakref
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from .registry import SPAWN_TOOL, AgentDef

ROOTS_KEPT = 4096  # main sessions whose root is remembered in memory (the rest are read back)


def _nobody() -> Any:
    return None


def agent_ref(obj: Any) -> Callable[[], Any]:
    """A weak reference to an agent (stock holds its owners the same way); a strong one if it cannot be weak."""
    if obj is None:
        return _nobody
    try:
        return weakref.ref(obj)
    except TypeError:
        return lambda: obj


def active_parent() -> Any:
    """The agent whose turn is running on this context (stock), or None outside an agent turn."""
    try:
        from agent.subagent_lifecycle import get_active_subagent_parent
    except ImportError:  # seams.private_drift warns about it; the owner then falls back to the recorded main agent
        return None
    return get_active_subagent_parent()


def routed_here(agent: Any) -> bool:
    """Whether stock routes ``agent``'s delegate_task through this plugin's provider: the backend its tools were built
    from, else config.yaml's ``delegation.backend`` (tools/delegate_tool_routes.py ``resolve_task_routes``). An
    unreadable backend counts as this one, so a child never gets more than this plugin grants."""
    from .provider import SubagentsProvider

    try:
        from agent.delegation_backends import pinned_backend, saved_backend

        return (pinned_backend(agent) or saved_backend()) == SubagentsProvider.name
    except Exception:  # a changed private surface (seams.private_drift) or an unreadable config
        return True


def task_has_schema(args: Any, index: int) -> bool:
    """Whether task ``index`` of a delegate_task call carries an output schema, by stock's own rule
    (tools/delegate_tool_tasks.py:107-122): the task's own, or the top-level one for a single task."""
    if not isinstance(args, dict):
        return False
    tasks = args.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return index == 0 and args.get("output_schema") is not None
    task = tasks[index] if 0 <= index < len(tasks) else None
    own = task.get("output_schema") if isinstance(task, dict) else None
    return own is not None or (len(tasks) == 1 and args.get("output_schema") is not None)


@dataclass(frozen=True)
class Spawn:
    """What a parent's delegate_task asked for one child."""

    agent: AgentDef | None
    name: str | None
    schema: bool | None
    owner: Callable[[], Any] = _nobody  # the spawning agent
    root: str = ""  # restored after a restart (children.py): the root conversation its parent chain started from


@dataclass(frozen=True)
class Child:
    session_id: str
    subagent_id: str
    agent: AgentDef | None  # None = untyped child of an untyped parent: full tool set
    name: str | None
    depth: int
    parent_session_id: str
    schema: bool | None = None  # None: its spawn was never seen here, so its output contract is unknown
    owner: Callable[[], Any] = field(default=_nobody, compare=False)
    root: str = ""  # the root conversation (the main session its spawn chain starts from)


def capped(agent: AgentDef, spawner: AgentDef) -> AgentDef:
    """``agent`` as a child of ``spawner`` may run it: only tools both allow, and a terminal command only when it
    matches every Bash scope of the child and of each scoped spawner above it."""
    if agent.tools is None or spawner.tools is None:
        tools = agent.tools if spawner.tools is None else spawner.tools
    else:
        tools = agent.tools & spawner.tools
    disallowed = agent.disallowed | spawner.disallowed
    scopes = tuple(dict.fromkeys((*agent.scopes, *spawner.command_groups)))
    return replace(agent, tools=tools, disallowed=disallowed, scopes=scopes,
                   may_spawn=agent.may_spawn and (tools is None or SPAWN_TOOL in tools) and SPAWN_TOOL not in disallowed)


class CallerMap:
    """Who is calling: child sessions bound at subagent_start to what their parent's spawn asked for."""

    def __init__(self, lookup: Callable[[str], str] = lambda session_id: "") -> None:
        self._lock = threading.Lock()
        self._children: dict[str, Child] = {}
        self._by_subagent: dict[str, Child] = {}
        self.lookup = lookup  # the persisted root of a rotated main session ("" when none)
        self._roots: dict[str, str] = {}  # main session -> its stable root conversation, newest last

    def bind(self, parent_session_id: str, child_session_id: str, subagent_id: str, found: Spawn | None) -> Child:
        """A started child (``found``: its spawn, None when no spawn of it was seen). A continued one (same
        subagent id, possibly a new session) keeps what it was; the child of a typed agent keeps that agent's
        restrictions, a typed one is capped at them."""
        with self._lock:
            known = self._by_subagent.get(subagent_id)
            parent = self._children.get(parent_session_id)
            if found is not None:
                agent, name, schema, owner = found.agent, found.name, found.schema, found.owner
            elif known is not None:  # a continued child
                agent, name, schema, owner = known.agent, known.name, known.schema, known.owner
            else:
                agent, name, schema, owner = None, None, None, _nobody
            if parent is not None and parent.agent is not None:
                agent = parent.agent if agent is None else capped(agent, parent.agent)
            root = parent.root if parent else (found.root if found is not None and found.root else
                                               known.root if known is not None else None)
        root = root or self.root_of(parent_session_id)
        with self._lock:
            child = Child(child_session_id, subagent_id, agent, name, (parent.depth if parent else 0) + 1,
                          parent_session_id, schema, owner, root)
            self._children[child_session_id] = child
            if subagent_id:
                self._by_subagent[subagent_id] = child
            return child

    def get(self, session_id: str) -> Child | None:
        return self._children.get(session_id)

    def known(self, subagent_id: str) -> Child | None:
        """The child last bound under that subagent id in this process."""
        return self._by_subagent.get(subagent_id)

    def root_of(self, session_id: str) -> str:
        """The root conversation of a session: a child's recorded root; for a main session the root it kept
        through compaction rotations (persisted), else its own id."""
        child = self._children.get(session_id)
        if child is not None and child.root:
            return child.root
        with self._lock:
            known = self._roots.get(session_id)
        if known is None and session_id:
            known = self.lookup(session_id) or session_id
            self.alias(session_id, known)
        return known or session_id

    def alias(self, session_id: str, root: str) -> None:
        """``session_id`` belongs to the conversation ``root`` (a rotated main session)."""
        with self._lock:
            self._roots.pop(session_id, None)
            self._roots[session_id] = root
            while len(self._roots) > ROOTS_KEPT:
                self._roots.pop(next(iter(self._roots)))

    def rename(self, old_session_id: str, new_session_id: str) -> Child | None:
        """A child whose session stock compaction rotated: the same agent under its new session id. The old id
        stays an alias, so its own children still find their parent and root task."""
        with self._lock:
            child = self._children.get(old_session_id)
            if child is None or not new_session_id or new_session_id == old_session_id:
                return None
            moved = replace(child, session_id=new_session_id)
            self._children[new_session_id] = moved
            if child.subagent_id:
                self._by_subagent[child.subagent_id] = moved
            return moved
