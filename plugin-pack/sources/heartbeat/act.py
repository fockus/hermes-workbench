"""The single executor (REQ-004, REQ-013, REQ-018, NFR-003): every action, from any source, goes through
``Executor.apply``.

``apply`` re-reads the task and drops the decision when its fingerprint differs from the one taken at detection;
re-runs ``policy.classify`` so the forbidden actions and the 3-per-24h limit hold for every source; then either
calls the stock ``hermes_cli.kanban_db`` function (looked up on the module at call time) or asks the human.
Each applied action and each question leaves one ``[heartbeat] <action> — <reason>; next: <step>`` comment and
one journal row; an attempted automatic action counts toward the limit before its stock call.

Ports (injected; the defaults keep phase 1 self-contained):

* ``Messenger`` - ``nudge(task, text) -> bool`` and ``continue_(task) -> bool`` for subagent and harness runs.
  ``DEFAULT`` uses ``messengers.Messengers`` (``SubagentsAPI.send``, ``runner_for(...).send``); its
  ``Undelivered`` reason becomes the question. Executor's default None: "no messenger for <run kind> run".
* ``ask(conn, task, question, tick) -> None`` - approval, human and "cannot act" decisions. KH-ASK builds it
  (``block_task(kind="needs_input")`` plus a store row). Default ``journal_only``: the journal row ``apply``
  writes is the only record of the question.

Run kinds come from the stock ``assignee`` only (``run_kind``): none; ``ultracode-*`` control-plane lanes ->
harness run (harness-runner); ``team-*`` lanes -> subagent run (hermes-subagents team mode); any other
assignee -> stock profile worker, nudged with the comment plus ``reclaim_task`` so the dispatcher restarts it
with the comment in context, keeping its assignee.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Callable, Protocol

import hermes_cli.kanban_db as kb

from . import messengers, policy, probe, questions
from .detect import fingerprint

AUTHOR = "heartbeat"
HARNESS_LANES = ("ultracode-",)
SUBAGENT_LANES = ("team-",)
NEXT = {"reclaim": "the dispatcher starts a new run", "reassign_same_role": "the dispatcher starts the same role again",
        "assign": "the dispatcher starts the assignee", "nudge": "the executor goes on with this note",
        "continue": "the executor goes on with this note", "stop": "a human decides whether the run resumes",
        "ask_human": "a human answers with /kanban-ask", "archive": "the task is archived",
        "reassign_other": "the dispatcher starts the new assignee", "cancel": "the task is cancelled",
        "git": "the git change is made"}
#: the stock call behind each approval action (REQ-012); None = there is none
APPROVED = {"archive": "archive_task", "reassign_other": "reassign_task", "cancel": "cancel_task", "git": None}


class Messenger(Protocol):
    def nudge(self, task, text: str) -> bool: ...

    def continue_(self, task) -> bool: ...


def journal_only(conn, task, question: str, tick) -> None:
    """Default ask port until KH-ASK: nothing beyond the journal row ``apply`` already wrote."""
    return None


def run_kind(task) -> str:
    assignee = task.assignee or ""
    if not assignee:
        return "none"
    if assignee.startswith(HARNESS_LANES):
        return "harness"
    if assignee.startswith(SUBAGENT_LANES):
        return "subagent"
    return "profile"


def _stock(conn, task, decision) -> str | None:
    """Run the stock call of an automatic action; None on success, else why it did not happen."""
    action, reason = decision.action, "heartbeat: %s" % decision.reason
    if action in ("reclaim", "nudge", "continue"):
        done = kb.reclaim_task(conn, task.id, reason=reason)
    elif action == "reassign_same_role":
        if not task.assignee:
            return "no assignee to reassign within its role"
        done = kb.reassign_task(conn, task.id, task.assignee, reclaim_first=True, reason=reason)
    elif action == "assign":
        if not decision.finding.lane:
            return "no profile to assign"
        done = kb.assign_task(conn, task.id, decision.finding.lane)
    else:  # stop: block_task alone never signals a live worker; reclaim_task terminates it
        row = conn.execute("SELECT worker_started_at FROM tasks WHERE id = ?", (task.id,)).fetchone()
        # stock refuses to signal an unverified PID yet still releases the claim: a running orphan
        if task.status == "running" and row and row[0] == "unverified":
            return "the running worker has no verified fingerprint and cannot be signalled"
        if task.status == "running" and not kb.reclaim_task(conn, task.id, reason=reason):
            return "the running worker could not be reclaimed"
        done = kb.block_task(conn, task.id, reason=reason, kind="capability")
    return None if done else "the stock %s call did not apply" % action


@dataclass(frozen=True)
class Executor:
    messenger: Messenger | None = None
    ask: Callable[..., None] = journal_only

    def act(self, conn, decisions: list, tick) -> None:
        """The tick's act port: the first decision that settles a task wins; the rest of its tick is skipped."""
        settled = set()
        # an owner's needs_input text beats the generic stock stuck_in_blocked line; the sort is stable
        for decision in sorted(decisions, key=lambda d: d.finding.kind != "needs_input"):
            key = (decision.finding.board, decision.finding.task_id)
            if key not in settled and self.apply(conn, decision, tick) != "unclassified":
                settled.add(key)

    def apply(self, conn, decision, tick) -> str:
        """``dropped`` | ``unclassified`` | ``asked`` | ``applied`` | ``not_carried_out`` (a user approval)."""
        f = decision.finding
        task = kb.get_task(conn, f.task_id)
        now = fingerprint(task, kb.list_events(conn, f.task_id)) if task is not None else "gone"
        if now != f.fingerprint:
            tick.store.journal(f.board, f.task_id, "drop", "task changed since detection (%s -> %s)"
                               % (f.fingerprint, now), decision.source)
            return "dropped"
        if decision.klass == "unclassified":  # left for the supervisor harness (Task 7)
            return "unclassified"
        refused = decision.action in policy.FORBIDDEN
        vetted = policy.classify(f, decision.action, tick.store.actions_since(f.board, f.task_id),
                                 decision.source, decision.reason)
        if (vetted.action, vetted.klass) != (decision.action, decision.klass):
            decision = vetted
        decision = dataclasses.replace(decision, reason=tick.ctx.redact_text(decision.reason, force=True))
        if decision.klass == "approved":
            return self._approved(conn, task, decision, tick)
        if decision.klass != "auto":
            return self._question(conn, task, decision, decision.reason, tick, refused)
        if decision.action in tick.disabled:
            return self._question(conn, task, decision, "the stock call behind %s changed (contract probe): %s"
                                  % (decision.action, decision.reason), tick)
        # reserve the slot first: an unwritable store means no action, never an uncounted one
        tick.store.count_action(f.board, f.task_id)
        why = self._run(conn, task, decision)
        if why:
            return self._question(conn, task, decision, "%s, %s not taken: %s" % (why, decision.action,
                                                                                   decision.reason), tick)
        self._record(conn, decision, decision.action, decision.reason, tick)
        return "applied"

    def _run(self, conn, task, decision) -> str | None:
        kind = run_kind(task)
        if decision.action not in ("nudge", "continue") or kind == "profile":
            try:
                return _stock(conn, task, decision)
            except Exception as exc:  # a changed or refusing stock call must not abort the tick
                return "the stock %s call failed (%s)" % (decision.action, type(exc).__name__)
        if kind == "none":
            return "no executor to %s" % decision.action
        if self.messenger is None:
            return "no messenger for %s run" % kind
        try:
            with kb.scoped_current_board(decision.finding.board):  # a messenger's reads see the card's own board
                if decision.action == "nudge":
                    done = self.messenger.nudge(task, "[heartbeat] nudge — %s" % decision.reason)
                else:
                    done = self.messenger.continue_(task)
        except messengers.Undelivered as exc:  # the messenger names why the run was not reached
            return str(exc)
        except Exception as exc:  # a broken messenger must not stop the tick
            return "the %s messenger failed (%s)" % (kind, type(exc).__name__)
        return None if done else "the %s messenger did not deliver" % kind

    def _approved(self, conn, task, decision, tick) -> str:
        """``applied``, or ``not_carried_out`` with a comment saying why (the question stays open)."""
        from hermes_cli.profiles import normalize_profile_name  # the name stock reassign_task would store

        action, name = decision.action, APPROVED.get(decision.action)
        fn, target = getattr(kb, name, None) if name else None, decision.finding.lane
        if fn is None:
            why = "kanban_db has no %s" % name if name else "there is no stock call for %s" % action
        elif action in tick.disabled:
            why = "the stock call behind %s changed (contract probe)" % action
        elif action == "reassign_other" and (not target or normalize_profile_name(target) == (
                normalize_profile_name(task.assignee) if task.assignee else None)):
            why = "no other profile to reassign to"
        else:
            try:
                if action == "reassign_other":
                    done = fn(conn, task.id, target, reclaim_first=True, reason="heartbeat: %s" % decision.reason)
                else:
                    done = fn(conn, task.id)
                why = None if done else "the stock %s call did not apply" % name
            except Exception as exc:  # a changed or refusing stock call must not abort the answer
                why = "the stock %s call failed (%s)" % (name, type(exc).__name__)
        if why is None:
            self._record(conn, decision, action, decision.reason, tick)
            return "applied"
        if not probe.ACTIONS <= tick.disabled:
            kb.add_comment(conn, task.id, AUTHOR, "[heartbeat] %s — not carried out: %s; next: %s"
                           % (action, why, NEXT["ask_human"]))
        tick.store.journal(decision.finding.board, task.id, "not_carried_out", "%s: %s" % (action, why), decision.source)
        return "not_carried_out"

    def _question(self, conn, task, decision, reason: str, tick, refused: bool = False) -> str:
        """A question for the human; a task already waiting on one only reaches the ask port (it keeps one
        open question per task), unless the decision was a refused one."""
        waiting = task.status == "blocked" and task.block_kind == "needs_input"
        reason = tick.ctx.redact_text(reason, force=True)  # a messenger's reason can carry a harness's error text
        if refused or not waiting:
            self._record(conn, decision, "ask_human", reason, tick)
        self.ask(conn, task, tick.ctx.redact_text("%s: %s" % (task.title, reason), force=True), tick)
        return "asked"

    @staticmethod
    def _record(conn, decision, action: str, reason: str, tick) -> None:
        f = decision.finding
        # add_comment is behind every action name in the probe; all of them disabled = add_comment changed
        if not probe.ACTIONS <= tick.disabled:
            kb.add_comment(conn, f.task_id, AUTHOR, "[heartbeat] %s — %s; next: %s" % (action, reason, NEXT[action]))
        tick.store.journal(f.board, f.task_id, action, reason, decision.source)


DEFAULT = Executor(messenger=messengers.Messengers(), ask=questions.ask)


def apply(conn, decision, tick) -> str:
    return DEFAULT.apply(conn, decision, tick)


def act(conn, decisions: list, tick) -> None:
    """The tick's act port with the default messenger and ask port."""
    DEFAULT.act(conn, decisions, tick)
