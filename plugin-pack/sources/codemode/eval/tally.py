"""Per-tool error tally of an attempt (AGR-095, I-062). A call failed when its tool-completion event says so
(stock's ``post_tool_call`` hook: it fires for direct and nested calls alike, so an error a codemode script
caught with ``settle`` still counts) or its tool message is an error object (stock's ``{"error": ...}``,
which is also what a pre_tool_call block and an orchestrator refusal return), with or without the guardrail's
appended ``[Tool loop warning: ...]``. Each ``tool_call_id`` counts once."""

from __future__ import annotations

import contextlib
import json
import re
import threading
from collections import Counter
from typing import Any, Iterator, Mapping, Sequence

HOOK = "post_tool_call"
_GUARDRAIL = re.compile(r"\n\n\[Tool loop (?:warning|hard stop): .*\]\s*\Z", re.S)


def failed(content: Any) -> bool:
    """Whether a tool result is an error: an ``{"error": ...}`` object or text starting with ``Error``."""
    if not isinstance(content, str):
        return False
    text = _GUARDRAIL.sub("", content)
    try:
        data = json.loads(text)
    except ValueError:
        return text.startswith("Error")
    return isinstance(data, dict) and bool(data.get("error"))


class ToolEvents:
    """The failed tool calls of the watched sessions, by ``tool_call_id``, from ``post_tool_call`` events."""

    def __init__(self, sessions: Sequence[str]) -> None:
        self.sessions, self.failed, self._lock = sessions, {}, threading.Lock()

    def __call__(self, tool_name: str = "", tool_call_id: str = "", session_id: str = "", status: Any = None,
                 result: Any = None, **_: Any) -> None:
        if tool_call_id and session_id in self.sessions and (status == "error" or failed(result)):
            with self._lock:
                self.failed.setdefault(str(tool_call_id), tool_name or "?")


@contextlib.contextmanager
def watching(events: ToolEvents) -> Iterator[None]:
    """Deliver ``post_tool_call`` events to ``events`` while the attempt runs (the eval runs one attempt at
    a time). Without a plugin manager the tally falls back to the tool messages alone."""
    try:
        from hermes_cli.plugins import get_plugin_manager

        callbacks = get_plugin_manager()._hooks.setdefault(HOOK, [])
    except Exception:
        yield
        return
    callbacks.append(events)
    try:
        yield
    finally:
        with contextlib.suppress(ValueError):
            callbacks.remove(events)


def tool_errors(messages: Sequence[Any], events: Mapping[str, str] | None = None) -> dict[str, int]:
    """Tool name -> failed calls: the failed ``events`` plus the failed tool messages, each matched to its
    call by ``tool_call_id`` and counted once."""
    names = {c.get("id"): (c.get("function") or {}).get("name") or "?"
             for m in messages if isinstance(m, Mapping) and m.get("role") == "assistant"
             for c in m.get("tool_calls") or [] if isinstance(c, Mapping)}
    errors = dict(events or {})
    for index, m in enumerate(messages):
        if isinstance(m, Mapping) and m.get("role") == "tool" and failed(m.get("content")):
            errors.setdefault(str(m.get("tool_call_id") or "#%d" % index), names.get(m.get("tool_call_id"))
                              or m.get("name") or "?")
    return dict(Counter(errors.values()))
