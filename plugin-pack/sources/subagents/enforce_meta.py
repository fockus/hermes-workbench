"""Call arguments the caller rules of enforce.py read: the Tool Search bridge and terminal command scopes.

The bridge is stock tools/tool_search.py.

``tool_search`` and ``tool_describe`` only read the deferred-tool catalog. ``tool_call`` runs the tool it names,
so it is judged as that tool: ``{"calls": [{"name", "arguments"}]}`` or the legacy ``{"name", "arguments"}``,
``calls`` and ``arguments`` possibly JSON strings (stock normalize_tool_call_entries). A call this module cannot
read is judged as ``tool_call`` itself, which is refused to every restricted caller (fail closed)."""

from __future__ import annotations

import fnmatch
import json
import re
from typing import Any, Callable

from .registry import SPAWN_TOOL

DISCOVERY = frozenset({"tool_search", "tool_describe"})
BRIDGE_CALL = "tool_call"
SPAWN_HINT = ("Not started: call %s directly, not through tool_call; only a direct call is staged with its agent "
              "type." % SPAWN_TOOL)
CONNECTOR_PREFIX = "connectors__"  # stock is_connector_name: only connector tools may share one tool_call
LOCAL_BATCH_HINT = ("Not run: tool_call takes exactly one entry for local tools; you sent %d. Issue each call as a "
                    "separate tool_call.")
MALFORMED_HINT = "Not run: tool_call needs 'calls', a non-empty array of {name, arguments}."
SHELL_CHAIN_RE = re.compile(r"[;&|`<>\n\r]|\$\(")  # a scoped command never chains, pipes, redirects or substitutes

Verdict = Callable[[str, Any], "dict[str, str] | None"]


def _json(value: Any, empty: Any = None) -> Any:
    """``value`` itself, a JSON string decoded, None or a blank string as ``empty``; False when it is not JSON."""
    if isinstance(value, str) and not value.strip():  # stock reads a blank arguments string as no arguments
        return empty
    if value is None or not isinstance(value, str):
        return empty if value is None else value
    try:
        return json.loads(value)
    except ValueError:
        return False


def inner_calls(args: Any) -> list[tuple[str, dict[str, Any]]] | None:
    """The (tool, arguments) pairs a tool_call runs, or None when any entry is malformed."""
    if not isinstance(args, dict):
        return None
    raw = _json(args["calls"]) if args.get("calls") is not None else [args]
    raw = [raw] if isinstance(raw, dict) else raw
    if not isinstance(raw, list) or not raw:
        return None
    calls = []
    for entry in raw:
        name = entry.get("name") if isinstance(entry, dict) else None
        arguments = _json(entry.get("arguments"), {}) if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name.strip() or not isinstance(arguments, dict):
            return None
        calls.append((name.strip(), arguments))
    return calls


def judge_bridge_call(args: Any, verdict: Verdict, fail_closed: bool = False) -> dict[str, str] | None:
    """The first refusal among the tools a tool_call names, each judged (and counted) as a direct call.
    ``fail_closed`` (a typed child) refuses an unreadable call even where tool_call itself is allowed."""
    calls = inner_calls(args)
    if calls is None:
        return {"action": "block", "message": MALFORMED_HINT} if fail_closed else verdict(BRIDGE_CALL, args)
    if len(calls) > 1 and any(not tool.startswith(CONNECTOR_PREFIX) for tool, _ in calls):
        return {"action": "block", "message": LOCAL_BATCH_HINT % len(calls)}  # stock resolve_underlying_call
    if any(tool == SPAWN_TOOL for tool, _ in calls):  # pre_tool_call stages only a direct spawn
        return {"action": "block", "message": SPAWN_HINT}
    for tool, arguments in calls:
        refused = verdict(tool, arguments)
        if refused is not None:
            return refused
    return None


def command_allowed(patterns: tuple[str, ...], command: Any) -> bool:
    """Claude Code Bash scopes: ``prefix:*`` (the prefix and its arguments), a ``*`` glob, or one exact command."""
    if not isinstance(command, str) or SHELL_CHAIN_RE.search(command):
        return False
    command = " ".join(command.split())
    for pattern in patterns:
        if pattern.endswith(":*"):
            prefix = " ".join(pattern[:-2].split())
            if command == prefix or command.startswith(prefix + " "):
                return True
        elif fnmatch.fnmatchcase(command, " ".join(pattern.split())):
            return True
    return False
