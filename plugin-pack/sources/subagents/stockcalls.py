"""The stock surfaces the launcher and the messenger call or read: delegate_task's route error, what its result says
about the children it ran, and the gateway route key of the calling context."""

from __future__ import annotations

import json

SPAWN = "delegate_task"


def route_error() -> type[Exception]:
    from agent.delegation_provider import DelegationRouteError

    return DelegationRouteError


def nothing_ran(result: str) -> bool:
    """A delegate_task result that is a bare tool error: stock dispatched no child of the call."""
    try:
        data = json.loads(result)
    except ValueError:
        return False
    return isinstance(data, dict) and "error" in data and not {"results", "delegation_id", "inline_results"} & set(data)


def bound_key() -> str:
    """The gateway route key bound in the caller's context (stock gateway/session_context.py), else "" (a CLI)."""
    try:
        from gateway.session_context import bound_session_key
    except ImportError:
        return ""
    return str(bound_session_key() or "")
