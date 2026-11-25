"""Eval sessions run without orchestrator limits (AGR-095, I-062), session-scoped.

The live subagents plugin's orchestrator mode refuses ``codemode``, ``write_file`` and ``patch`` for a
main agent and caps its reads, which made the 2026-11-23 runs measure refusals. The eval marks each of
its own agents; the marker lives on the agent object, so it covers that session only (a chat session in
the same process stays limited) and never touches the user's configuration. ``is_exempt`` is the
zero-argument predicate the subagents Enforcer takes as ``unattended``: it reads the marker off stock's
active agent. A runtime whose subagents plugin does not honour the marker still refuses, and the
preflight then stops the eval (preflight.py)."""

from __future__ import annotations

from typing import Any

MARKER = "orchestrator_exempt"


def mark(agent: Any) -> None:
    """Exempt this agent's session from orchestrator limits."""
    try:
        setattr(agent, MARKER, True)
    except AttributeError:  # a slotted agent cannot carry it: the preflight catches the refusal
        pass


def _active_agent() -> Any:
    try:
        from agent.subagent_lifecycle import get_active_subagent_parent
    except ImportError:
        return None
    return get_active_subagent_parent()


def is_exempt(agent: Any = None) -> bool:
    """Whether ``agent`` (default: stock's active agent, the one whose tool call is being judged) is an
    eval agent."""
    agent = _active_agent() if agent is None else agent
    return getattr(agent, MARKER, False) is True
