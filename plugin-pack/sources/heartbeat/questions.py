"""The single human-question channel (REQ-009, REQ-010, REQ-011, REQ-017, NFR-004).

A question is a stock ``block_task(kind="needs_input")`` plus one open row in the store's ``questions`` table.
Each row keeps the ``anchor`` of the phase it was asked in (``status|latest block event id``); a row whose task
left that phase is stale:

* ``ask`` - the executor's ask port: a running or ready task is blocked needs_input (a running worker is
  reclaimed first, since ``block_task`` never signals it), a task in another phase (review, todo) keeps it, and
  the row is added; a task never has two open rows;
* ``adopt`` - per board after act: a needs_input block set by an owner or the compaction-quality watchdog gets a
  row with its block reason; a stale row is closed;
* ``deliver`` - the tick's deliver port: one masked line per new question, nothing inside quiet hours, one digest
  of the held questions after them, and one reminder for a question delivered 24 h ago.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import time
from pathlib import Path

import hermes_cli.kanban_db as kb

from . import policy
from .detect import _block_reason

DAY = 24 * 3600
QUIET = {"start": "23:00", "end": "09:00"}
# the reason policy.classify gives an approval decision: "approval needed to <action>: <why>"
_PROPOSED = re.compile(r"approval needed to (\w+):")


def waiting(task) -> bool:
    return task.status == "blocked" and task.block_kind == "needs_input"


def anchor(conn, task) -> str:
    """The phase a question belongs to: the task status and its latest block or review request (a re-block or a
    new review request is a new one)."""
    blocks = [e.id for e in kb.list_events(conn, task.id) if e.kind in ("blocked", "block_loop_detected", "review_requested")]
    return "%s|%s" % (task.status, blocks[-1] if blocks else "")


def _board_of(conn, tick) -> str:
    """The board slug of a kanban connection (the ask port gets no slug)."""
    from . import tick as loop

    path = Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
    for slug in loop._boards(tick.disabled):
        if kb.kanban_db_path(slug).resolve() == path:
            return slug
    return kb.DEFAULT_BOARD


def _held_qid(db, board: str, task_id: str, where: str) -> str | None:
    """An open question on the task, or one asked in the same phase (an answered one is not asked again)."""
    row = db.execute("SELECT qid FROM questions WHERE board = ? AND task_id = ? AND (open = 1 OR anchor = ?)",
                     (board, task_id, where)).fetchone()
    return row[0] if row else None


def _insert(db, board: str, task_id: str, text: str, action: str, now: float, where: str) -> str:
    while True:  # 8 hex chars: [a-z0-9], safe on a command line; retried on the rare collision
        qid = secrets.token_hex(4)
        try:
            db.execute("INSERT INTO questions (qid, board, task_id, text, action, created, anchor)"
                       " VALUES (?, ?, ?, ?, ?, ?, ?)", (qid, board, task_id, text, action, now, where))
            return qid
        except sqlite3.IntegrityError:
            continue


def _adopted_text(conn, task, tick) -> str:
    reason = _block_reason(kb.list_events(conn, task.id))
    return tick.ctx.redact_text("%s: %s" % (task.title, reason), force=True)


def ask(conn, task, question: str, tick) -> None:
    """The ask port: ``question`` arrives masked from the executor."""
    board = _board_of(conn, tick)
    with tick.store.connect() as db:
        held = _held_qid(db, board, task.id, anchor(conn, task))
    if held:
        tick.store.journal(board, task.id, "ask_held", "question %s holds this phase: %s" % (held, question), "table")
        return
    if waiting(task):  # an owner's block: its own reason is the question
        text, action = _adopted_text(conn, task, tick), "ask_human"
    else:
        found = _PROPOSED.findall(question)
        text, action = question, (found[-1] if found and found[-1] in policy.APPROVAL else "ask_human")
        why = _block(conn, task, question, tick) if task.status in ("running", "ready") else None
        if why:
            tick.store.journal(board, task.id, "ask_failed", "%s: %s" % (why, question), "table")
            return
        # re-read: a second needs_input block after an answer lands in stock triage, and the row follows it there
        task = kb.get_task(conn, task.id)
    with tick.store.connect() as db:
        _insert(db, board, task.id, text, action, tick.now, anchor(conn, task))


def _block(conn, task, question: str, tick) -> str | None:
    """Block a running or ready task needs_input; None on success or when a running worker cannot be ended (the
    question is then asked without a block), else why not (the task is left as it was)."""
    if "ask_human" in tick.disabled:
        return "the stock needs_input block changed (contract probe)"
    if task.status == "running":  # block_task drops the claim without signalling the worker; reclaim ends it
        row = conn.execute("SELECT worker_started_at FROM tasks WHERE id = ?", (task.id,)).fetchone()
        if (row and row[0] == "unverified") or "reclaim" in tick.disabled:
            return None  # the worker cannot be ended: asked in the running phase, claim kept
        if not kb.reclaim_task(conn, task.id, reason="heartbeat: %s" % question):
            return "the running worker could not be reclaimed"
        if kb.get_task(conn, task.id).status != "ready":  # a reviewer run returns to review: no block there
            return None
    if not kb.block_task(conn, task.id, reason=question, kind="needs_input"):
        return "the stock needs_input block did not apply"
    return None


def adopt(conn, board: str, tick) -> None:
    """Rows for needs_input blocks nobody asked through ``ask``; closes rows whose task left their phase."""
    tasks = {t.id: t for t in kb.list_tasks(conn)}
    blocked = {i for i, t in tasks.items() if waiting(t)}
    if not blocked and not tick.store.path.is_file():
        return
    with tick.store.connect() as db:
        live = set()
        for task_id, qid, held in db.execute("SELECT task_id, qid, anchor FROM questions WHERE board = ? AND open = 1",
                                             (board,)).fetchall():
            if task_id in tasks and held == anchor(conn, tasks[task_id]):
                live.add(task_id)
            else:
                db.execute("UPDATE questions SET open = 0 WHERE qid = ?", (qid,))
        for task_id in blocked - live:
            task = tasks[task_id]
            _insert(db, board, task_id, _adopted_text(conn, task, tick), "ask_human", tick.now, anchor(conn, task))


def _minutes(value: str) -> int:
    hours, minutes = (int(part) for part in str(value).split(":"))
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        raise ValueError("not a time of day: %r" % value)
    return hours * 60 + minutes


def quiet(tick, when: float) -> bool:
    """Whether local time ``when`` is inside ``quiet_hours`` (a window may wrap midnight)."""
    config = tick.ctx.get_config("quiet_hours") or QUIET
    try:
        start, end = _minutes(config["start"]), _minutes(config["end"])
    except (KeyError, TypeError, ValueError):  # a malformed window keeps the default one
        start, end = _minutes(QUIET["start"]), _minutes(QUIET["end"])
    local = time.localtime(when)
    minute = local.tm_hour * 60 + local.tm_min
    return start <= minute < end if start <= end else minute >= start or minute < end


def line(kind: str, board: str, task_id: str, text: str, qid: str) -> str:
    text = " ".join(str(text).split())  # a multi-line title or reason stays on the one line that carries its qid
    return "[heartbeat] %s on board %s, task %s — %s — answer: /kanban-ask %s" % (kind, board, task_id, text, qid)


def deliver(tick) -> list[str]:
    """The deliver port: the lines the cron job prints."""
    if not tick.store.path.is_file() or quiet(tick, tick.now):
        return []
    with tick.store.connect() as db:
        new = db.execute("SELECT qid, board, task_id, text, created FROM questions WHERE open = 1"
                         " AND delivered IS NULL ORDER BY created").fetchall()
        due = db.execute("SELECT qid, board, task_id, text FROM questions WHERE open = 1 AND reminded = 0"
                         " AND delivered <= ? ORDER BY delivered", (tick.now - DAY,)).fetchall()
        lines = []
        if any(quiet(tick, r[4]) for r in new):
            lines.append("[heartbeat] digest: %d question(s) held over quiet hours" % len(new))
        lines += [line("question", b, t, text, q) for q, b, t, text, _c in new]
        lines += [line("reminder", b, t, text, q) for q, b, t, text in due]
        lines = [tick.ctx.redact_text(x, force=True) for x in lines]
        # marked only once every line is built and masked: a failure rolls the marks back with the transaction
        db.executemany("UPDATE questions SET delivered = ? WHERE qid = ?", [(tick.now, r[0]) for r in new])
        db.executemany("UPDATE questions SET reminded = 1 WHERE qid = ?", [(r[0],) for r in due])
    return lines


def get(store, qid: str):
    """``(qid, board, task_id, text, action, open, anchor)`` of one question, or None."""
    if not store.path.is_file():
        return None
    with store.connect() as db:
        return db.execute("SELECT qid, board, task_id, text, action, open, anchor FROM questions WHERE qid = ?",
                          (qid,)).fetchone()


def open_qids(store) -> list[str]:
    if not store.path.is_file():
        return []
    with store.connect() as db:
        return [q for (q,) in db.execute("SELECT qid FROM questions WHERE open = 1 ORDER BY created")]


def close(store, qid: str) -> None:
    with store.connect() as db:
        db.execute("UPDATE questions SET open = 0 WHERE qid = ?", (qid,))


def later(store, qid: str, now: float) -> None:
    """"later": the question stays open and is reminded once more 24 h from ``now``."""
    with store.connect() as db:
        db.execute("UPDATE questions SET delivered = ?, reminded = 0 WHERE qid = ?", (now, qid))
