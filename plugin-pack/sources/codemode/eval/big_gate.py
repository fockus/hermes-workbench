"""The gate of the big task class (AGR-103): on mode passes only when the fresh input of the ``big``
class drops by ``gate.TOKEN_DROP``, its accepted rate is not below direct, and no usage is missing.
The short-task cost rule of ``rollout_gate`` does not apply: the big class holds no short task."""

from __future__ import annotations

from typing import Any, Mapping

from .gate import _EPS, GateResult, drop_reasons, usage_reasons

BIG_CLASS = "big"


def big_gate(report: Mapping[str, Any], subscription: bool = False) -> GateResult:
    """Judge the ``big`` class of ``report``; every failed criterion is one reason. ``subscription`` is
    accepted for the shape of ``rollout_gate``: this gate judges tokens and quality, never cost."""
    modes = report["classes"].get(BIG_CLASS, {})
    direct, on = modes.get("direct"), modes.get("on")
    if direct is None or on is None:
        return GateResult(False, ("class %s was not run in both modes" % BIG_CLASS,))
    reasons = usage_reasons({"direct": direct, "on": on})
    if not direct["accepted"] and not on["accepted"]:  # a cheap early failure is not a win
        reasons.append("class %s: no attempt was accepted in either mode" % BIG_CLASS)
    reasons.extend(drop_reasons(direct, on))
    if on["accepted_rate"] + _EPS < direct["accepted_rate"]:
        reasons.append("class %s: accepted rate %.2f in on mode, %.2f direct"
                       % (BIG_CLASS, on["accepted_rate"], direct["accepted_rate"]))
    return GateResult(not reasons, tuple(reasons))
