"""Detection (REQ-002, REQ-019): stock ``kanban_diagnostics.compute_task_diagnostics`` plus own rules for
the gaps, mapped to one ``Finding`` per task, per kind, per tick.

The stock function is looked up on its module at call time, and the fingerprint comes from the task and
events read for that very call, so the executor can tell when the task changed since (REQ-018).

Own rules (thresholds from config.py):

* ``no_progress`` - running with no new event (comments are events) for 30 min. ``ponytail: workspace
  diffs are not read; a worker that edits files without a comment or event for 30 min is nudged. Upgrade:
  compare the workspace git tree once harness runs report it.``
* ``unassigned`` - ready or todo with no assignee for 1 h since its last event; the findings of a card with no
  assignee carry its parent card's assignee profile as the lane (``parent_role``, AGR-072), empty when there is
  none, so ``assign`` has a role or becomes the human question;
* ``long_review`` - in review for 24 h since review was requested;
* ``needs_input`` - a needs_input block open for 24 h (owners' and the compaction-quality watchdog's alike);
* ``claim_expired`` - running past ``claim_expires`` with an assignee that is not a profile: a team-board
  claim set by team mode (SA2-TEAM); this takes over the hermes-subagents claim sweep (D-24 there). The
  stock dispatcher reclaims expired claims of profile workers itself.

The stock rules get the user's kanban config, as ``hermes kanban diagnostics`` passes it, and see every
non-archived task; the stock module is imported only while the probe finds it (NFR-005).
"""

from __future__ import annotations

from dataclasses import dataclass

import hermes_cli.kanban_db as kb

from .config import Thresholds, thresholds


@dataclass(frozen=True)
class Finding:
    board: str
    task_id: str
    kind: str          # stock diagnostic kind or own rule id
    lane: str          # assignee profile or owner lane
    fingerprint: str   # status|assignee|current_run|last_event_id at detection
    detail: str


def fingerprint(task, events: list) -> str:
    last = events[-1].id if events else 0
    return "%s|%s|%s|%s" % (task.status, task.assignee or "", task.current_run_id or "", last)


def _since(task, events: list, kinds: tuple = ()) -> float:
    """When the newest event (of ``kinds``, if given) happened; the task's creation without one."""
    times = [e.created_at for e in events if not kinds or e.kind in kinds]
    return float(max(times) if times else task.created_at or 0)


def _block_reason(events: list) -> str:
    for event in reversed(events):
        if event.kind == "blocked" and isinstance(event.payload, dict) and event.payload.get("reason"):
            return str(event.payload["reason"])
    return "needs input"


def _profile_worker(assignee: str | None) -> bool:
    from hermes_cli.profiles import profile_exists

    return bool(assignee) and profile_exists(assignee)


def parent_role(conn, graph, by_id: dict) -> str:
    """The one profile the card's parents are assigned to, normalized as stock ``assign_task`` stores names;
    "" with no parent, no parent assignee, parents of different roles, or an assignee that is not a profile."""
    from hermes_cli.profiles import normalize_profile_name, profile_exists

    roles = set()
    for parent in (graph or {}).get("parents", ()):
        task = by_id.get(parent["id"]) or kb.get_task(conn, parent["id"])  # an archived parent is not listed
        if task is not None and task.assignee:
            roles.add(normalize_profile_name(task.assignee))
    if len(roles) != 1:
        return ""
    (role,) = roles
    try:
        return role if profile_exists(role) else ""
    except ValueError:  # a name stock profiles refuse is no known profile
        return ""


def own_rules(task, events: list, now: float, limits: Thresholds) -> list[tuple[str, str]]:
    """``(kind, detail)`` for every own rule the task meets at ``now``."""
    found = []
    idle = now - _since(task, events)
    if task.status == "running":
        if task.claim_expires is not None and now >= task.claim_expires and task.assignee and not _profile_worker(task.assignee):
            found.append(("claim_expired", "claim of %s expired %d min ago" % (
                task.assignee or "nobody", (now - task.claim_expires) // 60)))
        if idle >= limits.no_progress:
            found.append(("no_progress", "running with no event or comment for %d min" % (idle // 60)))
    elif task.status in ("ready", "todo") and not task.assignee and idle >= limits.unassigned:
        found.append(("unassigned", "%s with no assignee for %d min" % (task.status, idle // 60)))
    elif task.status == "review":
        waited = now - _since(task, events, ("review_requested",))
        if waited >= limits.long_review:
            found.append(("long_review", "in review for %d h" % (waited // 3600)))
    elif task.status == "blocked" and task.block_kind == "needs_input":
        waited = now - _since(task, events, ("blocked",))
        if waited >= limits.needs_input:
            found.append(("needs_input", "unanswered for %d h: %s" % (waited // 3600, _block_reason(events))))
    return found


def _stock_rules(disabled: frozenset):
    """``(task, events, runs, now, graph) -> diagnostics`` over the stock rules with the user's kanban
    config, or None when the probe disabled them; the function is looked up on its module per call."""
    if "stock_diagnostics" in disabled:
        return None
    import hermes_cli.kanban_diagnostics as kd
    from hermes_cli.config import load_config_readonly

    cfg = kd.config_from_runtime_config(load_config_readonly())

    def run(task, events, runs, now, graph):
        return kd.compute_task_diagnostics(task, events, runs, now=now, graph=graph, config=cfg)

    return run


def task_findings(board: str, task, events: list, runs: list, graph, now: float, limits: Thresholds,
                  disabled: frozenset, stock=None, role: str = "") -> list[Finding]:
    kinds: dict[str, str] = {}
    if stock is not None:
        for diag in stock(task, events, runs, int(now), graph):
            kinds.setdefault(diag.kind, diag.title or diag.detail or diag.kind)
    for kind, detail in own_rules(task, events, now, limits):
        if kind not in disabled:
            kinds.setdefault(kind, detail)
    fp = fingerprint(task, events)
    return [Finding(board, task.id, kind, task.assignee or role, fp, "%s (%s)" % (detail, task.title))
            for kind, detail in kinds.items()]


def scan(conn, board: str, tick) -> list[Finding]:
    """The ``detect`` port of tick.py: every finding on one board."""
    tasks = kb.list_tasks(conn)  # every non-archived task, as the stock fleet diagnostics read them
    graphs = kb.task_graph_contexts(conn, [t.id for t in tasks])
    limits, stock = thresholds(tick.ctx), _stock_rules(tick.disabled)
    by_id = {t.id: t for t in tasks}
    found = []
    # ponytail: two reads per task (events, runs); fine for hundreds of cards per board, batch them past that
    for task in tasks:
        graph = graphs.get(task.id)
        role = "" if task.assignee else parent_role(conn, graph, by_id)
        found.extend(task_findings(board, task, kb.list_events(conn, task.id), kb.list_runs(conn, task.id),
                                   graph, tick.now, limits, tick.disabled, stock, role))
    return found
