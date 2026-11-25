"""Managed children survive a restart (Astra review round 1, callers.py:136).

After a process restart stock ``continue`` rebuilds a finished child under its original subagent id with no fresh
spawn (tools/delegate_tool_continue.py), so the in-memory caller map knows nothing of it. Every child the plugin
binds is therefore recorded in its own kanban.db table: the effective policy (tools, disallowed tools, Bash
pattern groups including those inherited from scoped spawners), its name, output-schema flag and root
conversation. ``restore`` gives that back before the child's first resumed tool call. A row that cannot be read, or
whose policy fails the schema (every field and element type-checked), restores ``DENIED``: it runs with no tools.
Full tools come only from a positive ``unrestricted`` record; a child with no row at all is DENIED by the enforcer
unless a fresh routed spawn claims it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from .callers import Child, Spawn
from .calls import DENIED
from .kanban_store import KanbanStore
from .registry import AgentDef

logger = logging.getLogger("subagents.children")

UNRESTRICTED = "unrestricted"


def policy_of(agent: AgentDef | None) -> str:
    if agent is None:
        return UNRESTRICTED
    return json.dumps({
        "name": agent.name, "description": agent.description,
        "tools": sorted(agent.tools) if agent.tools is not None else None, "disallowed": sorted(agent.disallowed),
        "model": agent.model, "effort": agent.effort, "background": agent.background, "may_spawn": agent.may_spawn,
        "source": str(agent.source), "commands": list(agent.commands), "scopes": [list(g) for g in agent.scopes],
    }, sort_keys=True)


def _names(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError("%s must be a list of names" % field)
    return value


def _optional(value: Any, field: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError("%s must be a string or null" % field)
    return value


def _flag(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError("%s must be a boolean" % field)
    return value


def agent_of(policy: Any) -> AgentDef | None:
    """The policy back; raises ValueError when any field is missing or of the wrong type."""
    if policy == UNRESTRICTED:
        return None
    data = json.loads(policy) if isinstance(policy, str) else None
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) for k in ("name", "description", "source")):
        raise ValueError("the policy is not a record with a name, description and source")
    tools = None if data.get("tools", ...) is None else _names(data.get("tools"), "tools")
    scopes = data.get("scopes")
    if not isinstance(scopes, list):
        raise ValueError("scopes must be a list of pattern groups")
    return AgentDef(data["name"], data["description"], "", frozenset(tools) if tools is not None else None,
                    frozenset(_names(data.get("disallowed"), "disallowed")), _optional(data.get("model"), "model"),
                    _optional(data.get("effort"), "effort"), _flag(data.get("background"), "background"),
                    _flag(data.get("may_spawn"), "may_spawn"), Path(data["source"]),
                    tuple(_names(data.get("commands"), "commands")),
                    tuple(tuple(_names(g, "scopes")) for g in scopes))


class Children:
    """``save`` a bound child; ``restore`` a resumed one by its subagent id."""

    def __init__(self, store: KanbanStore):
        self.store = store

    def save(self, child: Child) -> None:
        if not child.subagent_id:
            return
        self.store.write("INSERT OR REPLACE INTO subagents_children (subagent_id, root, name, schema, policy, "
                         "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                         (child.subagent_id, child.root, child.name, None if child.schema is None else int(child.schema),
                          policy_of(child.agent), time.time()))

    def restore(self, subagent_id: str) -> Spawn | None:
        """The persisted spawn of a managed child; ``DENIED`` when its row cannot be read whole; None when the
        store answered and holds no row for it."""
        try:
            rows = self.store.read("SELECT root, name, schema, policy FROM subagents_children WHERE subagent_id = ?",
                                   (subagent_id,), strict=True)
        except (sqlite3.Error, OSError) as exc:
            logger.warning("subagents: cannot read the stored policy of %s (%s); it runs with no tools", subagent_id, exc)
            return Spawn(DENIED, None, True)
        if not rows:
            return None
        root, name, schema, policy = rows[0]
        try:
            agent = agent_of(policy)
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("subagents: the stored policy of %s is unreadable (%s); it runs with no tools",
                           subagent_id, exc)
            return Spawn(DENIED, name, True, root=root or "")
        return Spawn(agent, name, None if schema is None else bool(schema), root=root or "")
