"""Team gates (Task 11; REQ-025, REQ-026, REQ-027, REQ-030; design S5; SPIKE.md § 6): what happens to a team card
after it is completed, its teammate goes idle or keeps running.

Stock lifecycle hooks fire after the commit and ignore what they return, so the gate acts afterwards and fails closed.
On ``kanban_task_completed`` of a lane card the card first goes ``done -> review`` and its dependents are invalidated in
the same transaction (team_gates_review.py), then the card's ``verify`` shell command runs in the workspace
(team_gates_verify.py), one verification of a card at a time in this process; its verdict counts only for that
submission and that command, else the newer one is verified. The one completion that skips it is the lead's approval
of a card that passed its current check. A pass leaves the card in Review for the lead; a failure reopens it
(``reopen_review_task``) with the redacted reason and tells the teammate; the second failure blocks it with ``block
--kind needs_input``, the one human-question channel kanban-heartbeat delivers; a gate error blocks it the same way. A
teammate that stops (``subagent_stop``) with a running card claimed for it is nudged once per run, unless the lead
stopped it: then its card goes back to the board now that it has exited (team_gates_stop.py). While the agent a claim
was made for runs in this process (stock's live subagent registry), the lead renews that claim (``heartbeat_claim``)
at a third of the time a renewal guarantees (``claim_ttl`` less stock's whole-second rounding), so stock
``release_stale_claims`` never returns it mid-run; any other claim is left to expire and heartbeat's claim rule
returns it.

``install(ctx)`` registers the hooks, ``/team`` and, when kanban-heartbeat's ``heartbeat.api`` is importable, the
lane's owner contract (team_gates_ops.py); SA2-WIRE calls it from ``register``.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from . import team
from .names import LIVE
from .team_gates_ops import Board, TeamOwner, register_owner, send
from .team_gates_review import CHANGED, approved, current, decide, fail_closed, hold, submission
from .team_gates_stop import claimant, finish_stop, runner
from .team_gates_verify import VERIFY_TIMEOUT, verify
from .team_specs import LANE_PREFIX

logger = logging.getLogger("subagents.team")

RENEW_EVERY = team.CLAIM_TTL / 3  # longest wait between the lead's claim renewals
MIN_PERIOD = 0.25  # seconds; the shortest renewal period (team.py refuses a claim_ttl below MIN_CLAIM_TTL)


class Gates:
    """The hook handlers of team mode and the lead's claim renewal."""

    def __init__(self, verify_timeout: float = VERIFY_TIMEOUT):
        self.verify_timeout = verify_timeout
        self._lock = threading.Lock()
        self._serial: dict[str, threading.RLock] = {}  # per card: one verification at a time, a newer one waits
        self._nudged: set[tuple[str, Any]] = set()  # (task id, run id) of running cards: one nudge per run
        self._shown: dict[str, str] = {}  # lead session -> the progress line it saw last
        self._stop, self._wake = threading.Event(), threading.Event()

    def completed(self, task_id: str = "", assignee: str = "", **_: Any) -> None:
        if not str(assignee or "").startswith(LANE_PREFIX):
            return
        with self._lock:  # the finished run can never be nudged again
            self._nudged = {key for key in self._nudged if key[0] != task_id}
        try:
            self._gate(task_id, assignee)
        except Exception as exc:  # stock swallows hook errors: the card must not stay done unverified
            logger.warning("subagents: team gate of %s failed: %s: %s", task_id, type(exc).__name__, exc)
            try:
                fail_closed(task_id, assignee, "the verification gate failed (%s)" % type(exc).__name__)
            except Exception as again:  # nothing more can be done here; the log names the card
                logger.error("subagents: team card %s could not be blocked after a gate error: %s: %s", task_id,
                             type(again).__name__, again)

    def _gate(self, task_id: str, assignee: str) -> None:
        kb, closing = team._kanban()
        with closing as conn:
            card = kb.get_task(conn, task_id)
            if card is None or card.status != "done" or card.assignee != assignee:
                return
            if approved(kb.list_events(conn, task_id), card.body):  # the lead approved a verified card
                team.release(task_id)
                return
            mine = submission(conn, task_id)
        author = team.teammate_of(task_id)  # None when completed with no teammate's claim
        if not hold(task_id, assignee):  # moved by someone else
            return
        with self._lock:
            serial = self._serial.setdefault(task_id, threading.RLock())
        with serial:
            while True:  # again while the check changes under a running verification
                meta = current(task_id, mine)
                if meta is None:  # a newer submission is verified for itself, or the lead acted
                    return
                passed, detail = verify(str(meta.get("verify") or ""), str(meta.get("workspace") or "."),
                                        self.verify_timeout)
                if decide(task_id, assignee, mine, meta, passed, detail, author, card.title) != CHANGED:
                    return

    def idle(self, child_session_id: str = "", **_: Any) -> None:
        """A teammate stopped: a card the lead stopped it on goes back to the board; else the running card of its
        own team claimed for this agent gets one nudge per run."""
        parts = team._parts()
        child = parts.messenger.callers.get(child_session_id)
        if child is None or not child.name:
            return
        running = self._prune()
        if finish_stop(child.subagent_id):
            return
        root = parts.messenger.names.root_of(child_session_id)
        for card, team_id in running:
            name = card["claim_lock"].partition(":")[2]
            if name != child.name or runner(card["id"], card["current_run_id"]) != child.subagent_id:
                continue
            lead = team._team(team_id).lead
            key = (card["id"], card["current_run_id"])
            with self._lock:
                if parts.messenger.names.root_of(lead) != root or key in self._nudged:
                    continue
                self._nudged.add(key)
            send(child.name, "You stopped while team task %r (%s) is still assigned to you. Continue it and report "
                 "it done, or say what blocks you." % (card["title"], card["id"]), lead)

    def renew(self) -> int:
        """Renews every running card's claim whose agent runs in this process now; the number renewed."""
        renewed, active = 0, _active_ids()
        for card, team_id in self._prune():
            agent = claimant(card["id"], card["current_run_id"], card["claim_lock"].partition(":")[2],
                             team._team(team_id).lead)
            if agent is None or agent.state not in LIVE or agent.subagent_id not in active:
                continue
            kb, closing = team._kanban()
            with closing as conn:
                renewed += bool(kb.heartbeat_claim(conn, card["id"], ttl_seconds=_ttl(card),
                                                   claimer=card["claim_lock"]))
        return renewed

    def _prune(self) -> list[tuple[Any, str]]:
        """The running lane cards; nudge records of every other run are dropped (it can never be nudged again)."""
        running = _running()
        with self._lock:
            self._nudged &= {(card["id"], card["current_run_id"]) for card, _ in running}
        return running

    def claimed(self, assignee: str = "", **_: Any) -> None:
        """A lane card was claimed: the renewal loop reschedules for its claim_ttl."""
        if str(assignee or "").startswith(LANE_PREFIX):
            self._wake.set()

    def period(self, every: float) -> float:
        """Seconds to the next renewal, at most ``every``: a third of the shortest time a renewal of a running lane card
        guarantees (its claim_ttl less the second stock may round away from claim_expires)."""
        return min([every] + [max(MIN_PERIOD, (_ttl(card) - 1) / 3) for card, _ in _running()])

    def lead_turn(self, session_id: str = "", **_: Any) -> dict[str, str] | None:
        """The lead's session card: its team's progress before a reply, when it changed since the last one shown."""
        parts = team._parts()
        if not session_id or parts.messenger.callers.get(session_id) is not None:
            return None  # a child's turn runs inside the kanban fence and leads no team
        line = Board.of(parts.messenger.names.root_of(session_id)).progress()
        if not line or self._shown.get(session_id) == line:
            return None
        self._shown[session_id] = line
        return {"context": line}

    def loop(self, every: float) -> None:
        while not self._stop.is_set():
            try:
                wait = self.period(every)
            except Exception:  # noqa: BLE001 - the renewal below logs the same cause
                wait = min(every, 30.0)
            self._wake.wait(wait)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                self.renew()
            except Exception as exc:  # a broken pass must not end the renewals of the next ones
                logger.warning("subagents: team claim renewal failed: %s: %s", type(exc).__name__, exc)

    def close(self) -> None:
        self._stop.set()
        self._wake.set()


def _ttl(card: Any) -> int:
    return int(json.loads(card["body"] or "{}").get("claim_ttl") or team.CLAIM_TTL)


def _active_ids() -> set[str]:
    """Subagent ids stock's live registry runs in this process."""
    from tools.delegate_tool_registry import list_active_subagents

    return {str(r.get("subagent_id")) for r in list_active_subagents() if r.get("subagent_id")}


def _running() -> list[tuple[Any, str]]:
    """``(card row, team id)`` of every running lane card claimed for a teammate of its own lane."""
    kb, closing = team._kanban()
    with closing as conn:
        rows = conn.execute("SELECT id, title, body, assignee, claim_lock, current_run_id FROM tasks WHERE status = "
                            "'running' AND assignee LIKE ?", (LANE_PREFIX + "%",)).fetchall()
    found = []
    for row in rows:
        lane, _, name = str(row["claim_lock"] or "").partition(":")
        if lane == row["assignee"] and name:
            found.append((row, lane))
    return found


def install(ctx: Any, *, verify_timeout: float = VERIFY_TIMEOUT, renew_every: float = RENEW_EVERY) -> Gates:
    """Registers the gate hooks, ``/team`` and the lane's owner contract; starts the lead's claim renewal (at most
    ``renew_every`` seconds apart, none for 0), stopped when the plugin unloads."""
    gates = Gates(verify_timeout)
    ctx.register_hook("kanban_task_completed", gates.completed)
    ctx.register_hook("kanban_task_claimed", gates.claimed)
    ctx.register_hook("subagent_stop", gates.idle)
    ctx.register_hook("pre_llm_call", gates.lead_turn)
    ctx.register_command("team", Board.command, "Your team: status | send <name> <text> | stop")
    register_owner(TeamOwner())
    if renew_every > 0:
        threading.Thread(target=gates.loop, args=(renew_every,), name="subagents-team-claims", daemon=True).start()
        on_unload = getattr(ctx, "on_unload", None)
        if callable(on_unload):
            on_unload(gates.close)
    return gates
