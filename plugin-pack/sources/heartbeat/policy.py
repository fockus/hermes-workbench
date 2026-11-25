"""The policy table and the autonomy classes (REQ-003, REQ-005, REQ-006, REQ-007).

``policy.yaml`` maps a finding kind to an action (or a ladder of actions by the task's automatic actions in the
last 24 h); ``ctx.get_config("policy")`` rows merge over it, and a row set to None leaves the kind unclassified.
``classify`` is the one place the classes and limits live; ``act.apply`` re-runs it for every decision, so a
decision from the table, the supervisor, an owner or a user's answer meets the same limits.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

AUTO = frozenset({"reclaim", "nudge", "continue", "reassign_same_role", "assign", "stop"})
APPROVAL = frozenset({"archive", "cancel", "reassign_other", "git"})
FORBIDDEN = frozenset({"complete", "unblock"})  # done, and lifting needs_input instead of the human (REQ-006)
LIMIT = 3  # automatic actions per task per 24 h (REQ-007)
SOURCES = ("table", "supervisor", "owner", "user")


@dataclass(frozen=True)
class Decision:
    finding: object      # detect.Finding
    action: str          # design.md Action
    klass: str           # auto | approval | approved | human | owner | signal | unclassified
    reason: str
    source: str = "table"


def shipped() -> dict:
    return yaml.safe_load(Path(__file__).with_name("policy.yaml").read_text(encoding="utf-8")) or {}


def _row_ok(value) -> bool:
    return value is None or isinstance(value, str) or (
        isinstance(value, list) and bool(value) and all(isinstance(v, str) for v in value))


def table(ctx) -> dict:
    """The shipped table with the config rows over it; a malformed config row keeps the shipped one."""
    rows = dict(shipped())
    config = ctx.get_config("policy") or {}
    if isinstance(config, dict):
        rows.update({str(kind): value for kind, value in config.items() if _row_ok(value)})
    return rows


def classify(finding, action: str, counts: int, source: str = "table", reason: str = "") -> Decision:
    """The class of ``action`` on the finding's task after ``counts`` automatic actions in 24 h."""
    why = reason or finding.detail
    if action in FORBIDDEN:
        return Decision(finding, "ask_human", "human", "refused %s from %s: %s" % (action, source, why), source)
    if action in APPROVAL:
        if source == "user":  # the human's own answer is the approval: carried out by its stock call
            return Decision(finding, action, "approved", "approved by the user: %s" % why, source)
        return Decision(finding, action, "approval", "approval needed to %s: %s" % (action, why), source)
    if action in AUTO:
        if counts >= LIMIT:
            return Decision(finding, "ask_human", "human", "%d automatic actions in 24 h, %s not taken: %s"
                            % (counts, action, why), source)
        return Decision(finding, action, "auto", why, source)
    if action == "ask_human":
        return Decision(finding, action, "human", why, source)
    return Decision(finding, "ask_human", "human", "unknown action %r: %s" % (action, why), source)


def decide(finding, counts: int, rows: dict | None = None, source: str = "table") -> Decision:
    """The table's decision for one finding; ``counts`` = automatic actions on its task in the last 24 h."""
    row = (shipped() if rows is None else rows).get(finding.kind)
    if row is None:
        return Decision(finding, "ask_human", "unclassified", "no policy row for %s" % finding.kind, source)
    action = row[min(counts, len(row) - 1)] if isinstance(row, list) else row
    return classify(finding, action, counts, source)


def route(findings: list, tick) -> list[Decision]:
    """The tick's route port: one Decision per finding."""
    if not findings:
        return []
    rows, counts = table(tick.ctx), {}
    decisions = []
    for finding in findings:
        key = (finding.board, finding.task_id)
        if key not in counts:
            counts[key] = tick.store.actions_since(*key)
        decisions.append(decide(finding, counts[key], rows))
    return decisions
