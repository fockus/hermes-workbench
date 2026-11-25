"""Script limits and the first-line pragma (spec Task 6, REQ-018, REQ-019).

``# codemode: seconds=<n> calls=<n>`` on a script's first line lowers or raises its time and
nested-call limits within CEILING; memory stays fixed. Seconds count sandbox execution only,
never time awaiting tools or approvals.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace

PRAGMA = re.compile(r"^\s*#\s*codemode:(.*)$")


@dataclass(frozen=True)
class Limits:
    seconds: float = 300.0
    calls: int = 100
    memory: int = 256 * 1024 * 1024


DEFAULT_LIMITS = Limits()
CEILING = Limits(seconds=900.0, calls=200)
_KINDS = {"seconds": float, "calls": int}


def parse_pragma(
    code: str, base: Limits = DEFAULT_LIMITS, ceiling: Limits = CEILING
) -> tuple[Limits, list[str]]:
    """The limits of one script and notes on pragma values that were ignored or clamped."""
    match = PRAGMA.match(code.split("\n", 1)[0])
    if not match:
        return base, []
    limits, notes = base, []
    for item in match.group(1).split():
        key, _, raw = item.partition("=")
        kind = _KINDS.get(key)
        try:
            value = kind(raw) if kind else None
        except ValueError:
            value = None
        if value is None or (isinstance(value, float) and not math.isfinite(value)) or value <= 0:
            notes.append("pragma %r ignored: use seconds=<n> calls=<n>" % item)
            continue
        top = getattr(ceiling, key)
        if value > top:
            shown = raw if len(raw) <= 20 else raw[:17] + "..."
            notes.append(
                "pragma %s=%s is above the ceiling %s; %s used" % (key, shown, top, top)
            )
            value = top
        limits = replace(limits, **{key: value})
    return limits, notes


def call_limit_stop(limits: Limits) -> str:
    """What the script's next call raises once the limit is reached."""
    return "call limit of %d nested calls reached: no further calls run" % limits.calls


def call_limit_message(limits: Limits, ran: int) -> str:
    """The error of a script the call limit stopped; ``ran`` counts the calls dispatched."""
    return (
        "call limit reached: %d nested calls ran (limit %d), the script was stopped. Split the task into smaller "
        "codemode calls (or raise it with a first-line `# codemode: calls=<n>`, at most %d)."
        % (ran, limits.calls, CEILING.calls)
    )


def time_limit_message(limits: Limits) -> str:
    return (
        "time limit reached: the script ran %gs of sandbox time (tool waits excluded) and was stopped. Split the "
        "task into smaller codemode calls (or raise it with a first-line `# codemode: seconds=<n>`, at most %g)."
        % (limits.seconds, CEILING.seconds)
    )


NESTED_WAIT_SECONDS = 420.0  # one Hermes generic tool deadline of nested waiting on top of sandbox time


def deadline_floor(args: dict) -> float:
    """How long one codemode call may block (``ToolEntry.deadline_floor``): its sandbox time limit
    (pragma included, up to the ceiling) plus nested-call waits, which do not count as sandbox time."""
    code = args.get("code") if isinstance(args, dict) else None
    limits, _ = parse_pragma(code if isinstance(code, str) else "", DEFAULT_LIMITS)
    return limits.seconds + NESTED_WAIT_SECONDS
