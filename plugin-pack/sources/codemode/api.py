"""code-mode's API for other plugins (spec Task 14; REQ-031): ultracode's CodeModePort adapts
``CodeModeAPI().available()`` and ``CodeModeAPI().run_limits(session_id, seconds, tool_calls)``.

``run_limits`` sets the base limits of one session's scripts, up to ``limits.CEILING`` (900 s, 200 calls);
a script's first-line pragma still moves them within the ceiling, and every other session keeps
``limits.DEFAULT_LIMITS``. The raised limits end with the session (its session-end hooks). The registered
runtime (``LimitedRuntime``) and the tool deadline (``deadline_floor``) read them.
"""

from __future__ import annotations

import math
import threading
from dataclasses import replace
from typing import Any

from .limits import CEILING, DEFAULT_LIMITS, NESTED_WAIT_SECONDS, Limits, parse_pragma
from .runtime import Runtime

_SESSIONS_MAX = (
    512  # sessions holding raised limits at once; the oldest is dropped first
)
_lock = threading.Lock()
_limits: dict[str, Limits] = {}
_published: list[Runtime] = []  # the runtime the latest register() published, until its unload


class CodeModeAPI:
    """What ultracode needs from code mode."""

    def available(self) -> bool:
        """True while the latest register() of this plugin has the codemode tool registered (the pinned
        Monty was found) and it was not unloaded."""
        with _lock:
            return bool(_published)

    def run_limits(self, session_id: str, seconds: float, tool_calls: int) -> None:
        """Base limits for the scripts of ``session_id``: at most ``seconds`` of sandbox time and
        ``tool_calls`` nested calls each, clamped to the ceiling. ValueError on a non-positive value."""
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("run_limits needs the session id")
        if isinstance(tool_calls, bool) or not isinstance(tool_calls, int):
            raise ValueError(
                "run_limits: tool_calls must be a positive integer, not %r" % (tool_calls,)
            )
        limits = replace(
            DEFAULT_LIMITS,
            seconds=min(_positive("seconds", seconds), CEILING.seconds),
            calls=int(min(_positive("tool_calls", tool_calls), CEILING.calls)),
        )
        with _lock:
            _limits.pop(session_id, None)
            if len(_limits) >= _SESSIONS_MAX:
                _limits.pop(next(iter(_limits)))
            _limits[session_id] = limits


def _positive(name: str, value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(
            "run_limits: %s must be a positive number, not %r" % (name, value)
        )
    return value


def withdraw() -> None:
    """register() starts: until it publishes a runtime, code mode is not available."""
    with _lock:
        _published.clear()


def session_limits(session_id: str | None) -> Limits:
    """The base limits of a session's scripts."""
    with _lock:
        return _limits.get(session_id or "", DEFAULT_LIMITS)


def forget(session_id: str | None) -> None:
    with _lock:
        _limits.pop(session_id or "", None)


def deadline_floor(args: dict) -> float:
    """codemode's ``ToolEntry.deadline_floor``: the bridge's floor, raised to the calling session's
    sandbox time limit plus nested-call waits when run_limits raised it."""
    from .bridge import outer_floor
    from .caller import GrantUnknown, calling_agent

    floor = outer_floor(args)
    try:
        session = getattr(calling_agent(), "session_id", None)
    except GrantUnknown:
        session = None
    base = session_limits(session)
    if base == DEFAULT_LIMITS:
        return floor
    code = args.get("code") if isinstance(args, dict) else None
    limits, _ = parse_pragma(code if isinstance(code, str) else "", base)
    return max(floor, limits.seconds + NESTED_WAIT_SECONDS)


class _SessionBase:
    """The executor, running each script on its session's base limits."""

    def __init__(self, executor: Any):
        self._executor = executor

    def run(
        self,
        session_id: str,
        code: str,
        dispatch: Any,
        base: Limits | None = None,
        **kw: Any,
    ) -> Any:
        return self._executor.run(
            session_id, code, dispatch, base=base or session_limits(session_id), **kw
        )


class LimitedRuntime(Runtime):
    """The registered runtime: scripts run on their session's run_limits, which end with the session;
    ``available()`` holds from ``publish()`` (its tool is registered) until unload."""

    def publish(self) -> None:
        with _lock:
            _published[:] = [self]

    def executor(self) -> Any:
        return _SessionBase(super().executor())

    def end_session(self, session_id: str | None = None, **host: Any) -> None:
        super().end_session(session_id, **host)
        forget(session_id)

    def close(self) -> None:
        with _lock:
            if self in _published:
                _published.clear()
        super().close()
