"""A team card's way through verification (Task 11; REQ-025, REQ-027; review round 2): held in Review, its verdict
recorded for one submission, the lead's approval recognised. A submission is the card's latest ``completed`` event; a
verdict is written only while the card waits in Review for that submission with the ``verify`` command and workspace
the check ran (their fingerprint), so a newer submission or a changed command never inherits an older pass. The lead's
approval skips verification only when the last event before it, comments and attachments aside, is this gate's pass
for the card's current fingerprint. Split from team_gates.py for size.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import team
from .messages import MAIN
from .team_gates_ops import send
from .team_gates_stop import drain, halt

MAX_FAILURES = 2
PASSED, FAILED = "verification_passed", "verification_failed"  # this gate's own event kinds
SAME = ("commented", "attached")  # events that change neither the submission nor its check
RECORDED, STALE, CHANGED = "recorded", "stale", "changed"


def fingerprint(meta: dict) -> str:
    """The check a verdict is valid for: the card's ``verify`` command and workspace (hashed: never shown)."""
    raw = json.dumps([str(meta.get("verify") or ""), str(meta.get("workspace") or ".")])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def submission(conn: Any, task_id: str) -> Any:
    """The id of the card's latest ``completed`` event, or None."""
    row = conn.execute("SELECT MAX(id) AS id FROM task_events WHERE task_id = ? AND kind = 'completed'",
                       (task_id,)).fetchone()
    return row["id"] if row else None


def approved(events: list, body: str | None) -> bool:
    """True when the latest ``completed`` comes right after this gate's pass of the current check (``SAME`` aside)."""
    at = max((i for i, e in enumerate(events) if e.kind == "completed"), default=None)
    for event in reversed(events[:at] if at is not None else []):
        if event.kind not in SAME:
            return event.kind == PASSED and (event.payload or {}).get("check") == fingerprint(json.loads(body or "{}"))
    return False


def current(task_id: str, mine: Any) -> dict | None:
    """The card's body while it waits in Review for submission ``mine``; else None (a newer one or the lead acted)."""
    kb, closing = team._kanban()
    with closing as conn:
        card = kb.get_task(conn, task_id)
        if card is None or card.status != "review" or submission(conn, task_id) != mine:
            return None
        return json.loads(card.body or "{}")


def hold(task_id: str, assignee: str) -> bool:
    """``done -> review`` with stock's ``review_requested`` event, its dependents invalidated in the same transaction
    (none can be claimed while the card is verified). A dependent a teammate was already running is stopped and keeps
    its file locks until that teammate exits; a stock worker of one is ended as stock ends reclaimed workers. False
    when the card is no longer done."""
    state = team._state()
    kb, closing = team._kanban()
    with state.lock, closing as conn:  # no claim frees a dependent's locks between the move and the drain
        with kb.write_txn(conn):
            # ponytail: stock has no done -> review transition (request_review takes running/ready only), so this is
            # request_review's UPDATE and event on a done card; ceiling: stock's task columns as of today; upgrade:
            # call stock's transition once one accepts 'done'
            moved = conn.execute("UPDATE tasks SET status = 'review', completed_at = NULL, claim_lock = NULL, "
                                 "claim_expires = NULL, worker_pid = NULL WHERE id = ? AND status = 'done'",
                                 (task_id,)).rowcount
            if moved != 1:
                return False
            kb._append_event(conn, task_id, "review_requested",
                             {"summary": "verification", "implementer": assignee, "reviewer": None})
            out = kb.invalidate_descendants_for_parent_reopen(conn, task_id, author=assignee)
        halted = [(e["id"], drain(state, e["id"])) for e in out["invalidated"] if e["prior_status"] == "running"]
    for pid, claim_lock, started_at in out["terminations"]:  # post-commit, as stock drains them
        if pid:
            kb._terminate_reclaimed_worker(pid, claim_lock, started_at=started_at)
    lead = team._team(assignee).lead if halted else ""
    for dependent, entry in halted:
        halt(dependent, entry, assignee, lead, "a task it depends on went back to verification")
    return True


def decide(task_id: str, assignee: str, mine: Any, check: dict, passed: bool, detail: str, author: str | None,
           title: str) -> str:
    """Records the verdict of ``check`` for submission ``mine`` on the card still in Review; a failure reopens or
    (second time) blocks it. ``STALE`` when the card moved on, ``CHANGED`` when its check changed meanwhile."""
    kb, closing = team._kanban()
    with closing as conn:
        with kb.write_txn(conn):
            card = kb.get_task(conn, task_id)
            if card is None or card.status != "review" or submission(conn, task_id) != mine:
                return STALE
            if fingerprint(json.loads(card.body or "{}")) != fingerprint(check):
                return CHANGED
            failures = sum(e.kind == FAILED for e in kb.list_events(conn, task_id)) + (0 if passed else 1)
            payload = {"submission": mine, "check": fingerprint(check), **({} if passed else {"failures": failures})}
            kb._append_event(conn, task_id, PASSED if passed else FAILED, payload)
            kb.add_comment(conn, task_id, assignee, "Verification passed (exit 0); waits in Review for the lead."
                           if passed else "Verification failed: %s" % detail)
        if not passed:
            kb.reopen_review_task(conn, task_id)
            if failures >= MAX_FAILURES:
                kb.block_task(conn, task_id, kind="needs_input", reason="failed verification %d times; last: %s"
                              % (failures, detail[:300]))
    team.release(task_id)
    if not passed and author:
        then = ("It is blocked for the user's decision; stop working on it." if failures >= MAX_FAILURES else
                "The card is back on the board; fix it before you report it done again.")
        send(author, "Verification of team task %r failed: %s\n%s" % (title, detail, then),
             str(check.get("lead") or MAIN))
    return RECORDED


def fail_closed(task_id: str, assignee: str, reason: str) -> None:
    """After a gate error: the card back from done and blocked with needs_input for the user."""
    hold(task_id, assignee)
    kb, closing = team._kanban()
    with closing as conn:
        kb.reopen_review_task(conn, task_id)
        kb.block_task(conn, task_id, kind="needs_input", reason=reason)
    team.release(task_id)
