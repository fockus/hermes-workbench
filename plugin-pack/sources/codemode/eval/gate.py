"""The rollout gate (spec Task 11, REQ-030): on mode may become the default only when its fresh input
tokens (uncached input + cache writes, all tasks; cache reads are reported, not counted: AGR-095) drop by
at least 30 % against direct, no task class loses
accepted rate, and short tasks cost at most 10 % more per accepted result. A request without usage
(or any model call the counters miss) leaves tokens and cost incomplete, so the gate cannot pass
(hardening G5d)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

TOKEN_DROP = 0.30
SHORT_COST_RISE = 0.10
SHORT_CLASS = "short"
_EPS = 1e-9


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reasons: tuple[str, ...]


def _input_tokens(summary: Mapping[str, Any]) -> int:
    return summary["fresh_input_tokens"]


def usage_reasons(summaries: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """One reason per mode whose model calls the usage counters miss."""
    return ["%d model calls in %s mode are not covered by the usage counters (a request without "
            "usage, an uncounted continuation, a delegation or an auxiliary call), so tokens and "
            "cost are incomplete and cannot be judged" % (summary["usage_missing_requests"], mode)
            for mode, summary in summaries.items() if summary.get("usage_missing_requests", 0)]


def drop_reasons(direct: Mapping[str, Any], on: Mapping[str, Any]) -> list[str]:
    """The fresh-input criterion: on mode must use at least ``TOKEN_DROP`` less than direct."""
    base, cand = _input_tokens(direct), _input_tokens(on)
    if base > 0 and cand <= base * (1 - TOKEN_DROP) + _EPS:
        return []
    drop = (1 - cand / base) * 100 if base > 0 else 0.0
    return ["input tokens dropped %.1f%% (%d -> %d fresh, cache reads not counted); the gate needs %d%%"
            % (drop, base, cand, TOKEN_DROP * 100)]


def rollout_gate(report: Mapping[str, Any], subscription: bool = False) -> GateResult:
    """Judge on mode against direct; every failed criterion is one reason. On a ``subscription`` route
    (no per-token price, AGR-059) unknown cost is not applicable: tokens and quality decide."""
    reasons = usage_reasons({mode: report["totals"][mode] for mode in ("direct", "on")})
    reasons.extend(drop_reasons(report["totals"]["direct"], report["totals"]["on"]))
    for cls, modes in sorted(report["classes"].items()):
        direct, on = modes.get("direct"), modes.get("on")
        if direct is None or on is None:
            reasons.append("class %s was not run in both modes" % cls)
        elif on["accepted_rate"] + _EPS < direct["accepted_rate"]:
            reasons.append(
                "class %s: accepted rate %.2f in on mode, %.2f direct"
                % (cls, on["accepted_rate"], direct["accepted_rate"])
            )
    short = report["classes"].get(SHORT_CLASS, {})
    reasons.extend(_short_cost(short, subscription))
    return GateResult(not reasons, tuple(reasons))


def _short_cost(short: Mapping[str, Any], subscription: bool = False) -> list[str]:
    """The short-task cost criterion; unknown cost and a zero baseline are judged, never divided."""
    if not short:
        return ["no %s tasks ran, so the short-task cost cannot be judged" % SHORT_CLASS]
    unknown = [m for m in ("direct", "on") if short.get(m, {}).get("cost") is None]
    if unknown and subscription:  # cost not applicable, but the short tasks must still be judged
        if all(short.get(m, {}).get("accepted_rate", 0) > 0 for m in ("direct", "on")):
            return []
        return ["%s tasks have no accepted result in one mode, so the short-task criterion cannot be judged"
                % SHORT_CLASS]
    if unknown:
        return ["%s tasks have unknown cost in %s mode (no pricing for the route), so their cost cannot be "
                "judged" % (SHORT_CLASS, " and ".join(unknown))]
    direct_cost, on_cost = (short[m].get("cost_per_accepted") for m in ("direct", "on"))
    if direct_cost is None or on_cost is None:
        return ["%s tasks have no accepted result in one mode, so their cost cannot be judged" % SHORT_CLASS]
    if on_cost <= direct_cost * (1 + SHORT_COST_RISE) + _EPS:
        return []
    if direct_cost <= 0:
        return ["%s tasks cost $%.4f per accepted result in on mode against $%.4f direct; the gate allows "
                "%d%% more" % (SHORT_CLASS, on_cost, direct_cost, SHORT_COST_RISE * 100)]
    return ["%s tasks cost %.1f%% more per accepted result; the gate allows %d%%"
            % (SHORT_CLASS, (on_cost / direct_cost - 1) * 100, SHORT_COST_RISE * 100)]
