"""``/kanban-ask [qid]``: the human answers a heartbeat question (REQ-012, REQ-017).

Not ``/heartbeat``: stock Hermes has that built-in, and a plugin command named like a built-in is skipped.
It runs in the gateway process, not in the tick, so it opens the store and the board itself. Without a qid it
walks every open question; with one it asks through ``ctx.ask_choice`` (the question masked), or takes the
answer typed after it: ``/kanban-ask <qid> answer <text>``, ``approve``, ``reassign [<profile>]``, ``cancel``,
``later``. A question whose task left the phase it was asked in is closed instead. An answer comments on the task
(masked), carries the chosen action out through ``act.apply`` with source
``user`` (complete and lifting needs_input stay refused there), lifts needs_input with the stock
``unblock_task`` - the only place that does (design.md § Policy) - and closes the question. ``later`` keeps it
open and re-arms its reminder for 24 h.
"""

from __future__ import annotations

import time

import hermes_cli.kanban_db as kb

from . import act, policy, probe, questions
from .detect import Finding, fingerprint
from .store import Store

CHOICES = ("answer", "approve", "reassign", "cancel", "later")
ACTIONS = {"reassign": "reassign_other", "cancel": "cancel"}


def usage(qid: str) -> str:
    return "usage: /kanban-ask %s answer <text> (or: approve, reassign <profile>, cancel, later)" % qid


def handle(ctx, raw: str = "") -> str:
    """The ``/kanban-ask`` handler; returns the reply text."""
    from .tick import Tick, state_dir

    tick = Tick(ctx, Store(state_dir() / "heartbeat.db"), time.time(), probe.check().disabled)
    args = (raw or "").split(None, 2)
    if not args:
        qids = questions.open_qids(tick.store)
        return "\n".join(_one(tick, q, None, "") for q in qids) if qids else "[heartbeat] no open questions"
    return _one(tick, args[0], args[1] if len(args) > 1 else None, args[2] if len(args) > 2 else "")


def _one(tick, qid: str, choice: str | None, text: str) -> str:
    mask = tick.ctx.redact_text
    row = questions.get(tick.store, qid)
    if row is None or not row[5]:
        return mask("[heartbeat] no open question %s" % qid, force=True)
    if choice is None:
        choice = tick.ctx.ask_choice(mask(row[3], force=True), list(CHOICES))
        if choice is None:
            return usage(qid)
    choice, text = str(choice).strip().lower(), text.strip()
    if choice not in CHOICES or (choice == "answer" and not text):
        return usage(qid)
    if choice != "later" and "answer" in tick.disabled:
        return "[heartbeat] question %s not answered: the stock contract changed (contract probe)" % qid
    from .tick import _open_board

    with kb.pin_first_board_resolution(), _open_board(row[1]) as conn:
        task = kb.get_task(conn, row[2]) if conn is not None else None
        if task is None or questions.anchor(conn, task) != row[6]:
            questions.close(tick.store, qid)
            return mask("[heartbeat] question %s is closed: task %s changed since it was asked" % (qid, row[2]),
                        force=True)
        if choice == "later":
            questions.later(tick.store, qid, tick.now)
            return "[heartbeat] question %s stays open; one reminder in 24 h" % qid
        return _carry(tick, conn, row, task, choice, text)


def _pick(tick, conn, task) -> str:
    """The profile a reassign goes to, asked when the answer named none."""
    others = [a["name"] for a in kb.known_assignees(conn) if a["name"] != task.assignee]
    if not others:
        return ""
    prompt = "Reassign %s (%s) to which profile?" % (task.id, task.assignee or "nobody")
    return tick.ctx.ask_choice(tick.ctx.redact_text(prompt, force=True), others) or ""


def _carry(tick, conn, row, task, choice: str, text: str) -> str:
    qid, board = row[0], row[1]
    target = ""
    if choice == "approve" and row[4] == "reassign_other":  # the proposal named no profile: ask for one
        choice, text = "reassign", ""  # free text after approve is no profile name
    if choice == "reassign":
        target = text.split()[0] if text else _pick(tick, conn, task)
        if not target:
            return "usage: /kanban-ask %s reassign <profile>" % qid
    action = ACTIONS.get(choice) or (row[4] if choice == "approve" and row[4] in policy.APPROVAL else None)
    summary = {"answer": text, "reassign": "reassign to %s" % target}.get(choice, " ".join(filter(None, (
        choice, action if choice == "approve" else None))))
    summary = tick.ctx.redact_text(summary, force=True)  # the comment, the finding and the journal get it masked
    kb.add_comment(conn, task.id, act.AUTHOR, "[heartbeat] answer — %s; next: the heartbeat carries the answer out"
                   % summary)
    if action:
        now = kb.get_task(conn, task.id)
        finding = Finding(board, task.id, "user_answer", target or now.assignee or "",
                          fingerprint(now, kb.list_events(conn, task.id)), summary)
        result = act.apply(conn, policy.Decision(finding, action, "approval", summary, "user"), tick)
        if result != "applied":
            return "[heartbeat] %s not carried out for task %s (see its comments); question %s stays open" % (
                action, task.id, qid)
    current = kb.get_task(conn, task.id)
    if current is not None and questions.waiting(current):
        kb.unblock_task(conn, task.id)
    questions.close(tick.store, qid)
    tick.store.journal(board, task.id, "answer", summary, "user")
    return "[heartbeat] question %s answered (%s) on task %s" % (qid, choice, task.id)
