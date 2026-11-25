"""Paired A/B runs and the per-class report (spec Task 11, REQ-029).

Every task runs ``repeats`` times in every mode. The mode order rotates with the repeat and the
task's position, so no mode always goes first (provider caches and drift hit both alike). A run
record is ``accepted, fresh_tokens, cached_tokens, cost, seconds, calls``; tokens, cost and calls
are summed over all attempts, so cost_per_accepted charges rejected attempts to accepted ones. A
record may add ``error``, ``usage_missing`` (its requests without usage), ``requests`` (model requests
sent) and ``tool_errors`` (tool name -> failed results) and ``evidence`` (what the attempt did, see ``evidence``;
``attempts`` in the report lists it per task, mode and repeat); ``gross_input_tokens`` is fresh + cached.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Callable, Iterable, Mapping, Sequence

MODES = ("direct", "on")
RECORD_KEYS = ("accepted", "fresh_tokens", "cached_tokens", "cost", "seconds", "calls")
Runner = Callable[[Mapping[str, Any], str], Mapping[str, Any]]


def quantile(values: Iterable[float], q: float) -> float | None:
    """Linear-interpolated quantile (the inclusive method); None without values."""
    xs = sorted(values)
    if not xs:
        return None
    pos = (len(xs) - 1) * q
    lo, hi = xs[math.floor(pos)], xs[math.ceil(pos)]
    return lo + (hi - lo) * (pos - math.floor(pos))


def summarize(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    accepted = sum(1 for r in records if r["accepted"])
    unknown = sum(1 for r in records if r["cost"] is None)  # no pricing for the route: never a measured 0
    cost = None if unknown else sum(r["cost"] for r in records)
    seconds = [r["seconds"] for r in records]
    return {
        "runs": len(records),
        "accepted": accepted,
        "accepted_rate": accepted / len(records) if records else 0.0,
        "fresh_input_tokens": sum(r["fresh_tokens"] for r in records),
        "cached_input_tokens": sum(r["cached_tokens"] for r in records),
        "cost": cost,
        "cost_unknown_runs": unknown,
        "cost_per_accepted": cost / accepted if accepted and cost is not None else None,
        "errors": sum(1 for r in records if r.get("error")),
        "p50_seconds": quantile(seconds, 0.5),
        "p95_seconds": quantile(seconds, 0.95),
        "calls": sum(r["calls"] for r in records),
        "usage_missing_requests": sum(r.get("usage_missing", 0) for r in records),
        "gross_input_tokens": sum(r["fresh_tokens"] + r["cached_tokens"] for r in records),
        "requests_per_attempt": sum(r.get("requests", 0) for r in records) / len(records) if records else 0.0,
        "tool_errors": sum(sum(r.get("tool_errors", {}).values()) for r in records),
        "tool_errors_by_tool": dict(sum((Counter(r.get("tool_errors", {})) for r in records), Counter())),
    }


def _checked(
    record: Mapping[str, Any], task: Mapping[str, Any], mode: str
) -> dict[str, Any]:
    missing = [k for k in RECORD_KEYS if k not in record]
    if missing:
        raise ValueError(
            "the run of task %s in %s mode lacks %s"
            % (task["id"], mode, ", ".join(missing))
        )
    kept = {k: record[k] for k in RECORD_KEYS}
    kept.update({k: record[k] for k in ("error", "usage_missing", "requests", "tool_errors", "evidence") if record.get(k)})
    return kept


def run_eval(
    tasks: Sequence[Mapping[str, Any]],
    runner: Runner,
    repeats: int = 3,
    modes: Sequence[str] = MODES,
) -> dict[str, Any]:
    """Run every task ``repeats`` times in each mode and report per class and mode.

    ``tasks`` are ``{"id", "class"}``; ``runner(task, mode)`` returns one run record."""
    order: list[list[Any]] = []
    attempts: list[dict[str, Any]] = []
    by_cell: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for rep in range(repeats):
        for index, task in enumerate(tasks):
            shift = (rep + index) % len(modes)
            for mode in [*modes[shift:], *modes[:shift]]:
                record = _checked(runner(task, mode), task, mode)
                order.append([task["id"], mode, rep])
                if "evidence" in record:
                    attempts.append({"task": task["id"], "mode": mode, "rep": rep, **record["evidence"]})
                by_cell.setdefault((task["class"], mode), []).append(record)
    classes: dict[str, dict[str, Any]] = {}
    for (cls, mode), records in sorted(by_cell.items()):
        classes.setdefault(cls, {})[mode] = summarize(records)
    totals = {
        m: summarize([r for (_, mode), rs in by_cell.items() if mode == m for r in rs])
        for m in modes
    }
    return {
        "modes": list(modes),
        "repeats": repeats,
        "order": order,
        "attempts": attempts,
        "classes": classes,
        "totals": totals,
    }
