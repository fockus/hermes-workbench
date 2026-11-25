"""Thresholds of the heartbeat's own detection rules, from the plugin config ``thresholds`` (minutes).

``ponytail: the defaults (30 min / 1 h / 24 h / 24 h) are guesses (design C2); tune them from the journal
after the first week.`` A value that is not a positive number keeps the default."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MINUTES = {"no_progress": 30, "unassigned": 60, "long_review": 24 * 60, "needs_input": 24 * 60}


@dataclass(frozen=True)
class Thresholds:
    """Seconds a task may sit in a state before its rule fires (fires at the threshold, not before)."""

    no_progress: float
    unassigned: float
    long_review: float
    needs_input: float


def _minutes(value, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return default
    return float(value)


def thresholds(ctx) -> Thresholds:
    raw = ctx.get_config("thresholds") or {}
    raw = raw if isinstance(raw, dict) else {}
    return Thresholds(**{rule: _minutes(raw.get(rule), default) * 60 for rule, default in DEFAULT_MINUTES.items()})
