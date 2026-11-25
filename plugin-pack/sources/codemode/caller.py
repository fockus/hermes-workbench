"""What code-mode reads about the agent turn that called it: its tool grant and its interrupt bit.

The grant follows stock Hermes's tri-state rule for tools run on a session's behalf
(``model_tools.handle_function_call``: ``enabled_tools`` None picks the process default,
``enabled/disabled_toolsets`` None means unrestricted; ``code_execution_tool._sandbox_tools_for``:
None → every sandbox tool, an explicit list → exactly that list):

- inside an agent turn (``agent.subagent_lifecycle.get_active_subagent_parent``, the agent
  ``run_conversation`` binds for its own turn, a child binds itself), the grant is that agent's
  ``valid_tool_names`` plus the deferred tools its ``tool_call`` bridge may reach (stock
  ``scoped_deferrable_names`` of the agent's pre-assembly selection, served.py: the set stock
  checks ``tool_call`` against, read without stock's process-wide side effect);
- an agent whose grant cannot be read raises ``GrantUnknown``: code-mode refuses, never all tools;
- no agent turn at all (a host call: the CLI, tests) → None, the full registry.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

TOOL_CALL = "tool_call"


class GrantUnknown(RuntimeError):
    """The calling agent's tool grant cannot be read; code-mode must not run."""


@dataclass(frozen=True)
class Grant:
    direct: frozenset[str]  # the agent's valid_tool_names
    deferred: frozenset[str]  # reachable only through its tool_call bridge, which validates arguments

    @property
    def names(self) -> frozenset[str]:
        return self.direct | self.deferred


def calling_agent() -> object | None:
    """The agent whose turn called code-mode; None outside any agent turn (a host call)."""
    try:
        from agent.subagent_lifecycle import get_active_subagent_parent

        return get_active_subagent_parent()
    except (
        Exception
    ) as exc:  # cannot tell whether an agent called: refuse rather than guess
        raise GrantUnknown(
            "codemode refused to run: the calling agent cannot be determined (%s)" % exc
        ) from exc


def tool_grant(agent: object | None, defs: list[dict] | None = None) -> Grant | None:
    """The tools ``agent``'s session may use; None for a host call (no agent). ``defs``: the
    agent's pre-assembly selection (served.definitions); without one no tool is deferred."""
    if agent is None:
        return None
    names = getattr(agent, "valid_tool_names", None)
    if not isinstance(names, (set, frozenset, list, tuple)) or not all(
        isinstance(n, str) for n in names
    ):
        raise GrantUnknown(
            "codemode refused to run: the tool grant of the calling agent cannot be read"
        )
    direct = frozenset(names)
    deferred = _deferred(defs) - direct if TOOL_CALL in direct else frozenset()
    return Grant(direct, deferred)


def _deferred(defs: list[dict] | None) -> frozenset[str]:
    if defs is None:
        return frozenset()
    try:
        from tools.tool_search import scoped_deferrable_names

        return frozenset(scoped_deferrable_names(defs))
    except Exception:  # fewer tools, never more
        return frozenset()


def interrupt_check() -> Callable[[], bool]:
    """``check() -> bool``: whether Hermes interrupted the calling tool thread (``/stop``, a new
    user message). Bound to the caller's thread now, so a watchdog thread can ask it later."""
    try:
        from tools.interrupt import acting_for_tid, is_thread_interrupted
    except ImportError:
        return lambda: False
    tid, acting = threading.get_ident(), acting_for_tid.get()
    return lambda: is_thread_interrupted(tid) or is_thread_interrupted(acting)
