"""Which agent runs a team card, and stopping it (Task 11; REQ-026, REQ-030; review round 2). A claim made in this
process records its run and the subagent it was made for (team.py ``held``); nudges, renewals and stops act only on
that agent, never on a newer one that took the same name. A stop never frees a card while its teammate may still write:
stock's stop only requests an interrupt, so the stop is recorded for that agent before stock is called and the run's
card goes back to the board, with its file locks, when that agent's ``subagent_stop`` arrives (team_gates.py ``idle``).
A refused stop keeps the claim, and so does a teammate whose state cannot be read. A dependent whose card a returning
parent took back keeps its file locks the same way until its teammate exits. Split from team_gates_ops.py for size.
"""

from __future__ import annotations

import json
import logging
import threading
from types import SimpleNamespace
from typing import Any

from . import team
from .limiter import process_shared
from .names import LIVE

logger = logging.getLogger("subagents.team")


def runner(task_id: str, run_id: Any) -> str | None:
    """The subagent id the card's claim ``run_id`` was made for in this process, or None when unknown."""
    state = team._state()
    with state.lock:
        entry = state.held.get(task_id)
    return entry[3] if entry and run_id is not None and entry[2] == run_id else None


def claimant(task_id: str, run_id: Any, name: str, lead: str) -> Any:
    """The agent ``name`` of the lead's conversation when the card's claim ``run_id`` was made for it, else None."""
    names = team._parts().messenger.names
    agent = names.resolve(name, names.root_of(lead))
    sid = runner(task_id, run_id)
    return agent if agent is not None and sid is not None and agent.subagent_id == sid else None


def stop_teammate(team_id: str, name: str, lead: str, reason: str) -> str | None:
    """Stops teammate ``name``; the running cards of ``team_id`` claimed for it go back to the board once it has
    exited (at once when it runs no more). The error, or None; on an error its claims stay."""
    names = team._parts().messenger.names
    agent = names.resolve(name, names.root_of(lead))
    if agent is None:  # no record, or the names store could not be read: it may still run
        return "cannot tell whether %s still runs" % name
    claim = "%s:%s" % (team_id, name)
    kb, closing = team._kanban()
    with closing as conn:
        cards = [c for c in kb.list_tasks(conn, assignee=team_id) if c.status == "running" and c.claim_lock == claim]
    runs = [(c.id, c.current_run_id, team_id, reason) for c in cards
            if runner(c.id, c.current_run_id) == agent.subagent_id]
    if agent.state not in LIVE:  # this agent runs no more: nothing it could still write
        for run in runs:
            _put_back(*run)
        return None
    return _stop_runs(names, agent, runs)


def halt(task_id: str, entry: tuple | None, lane: str, lead: str, reason: str) -> None:
    """A running dependent ``drain`` took back: its teammate is stopped and its file locks go when it has exited; they
    stay to their lease when it cannot be told who runs it."""
    if entry is None:  # claimed in another process: its locks are that process's
        return
    teammate, _, run_id, sid = entry
    names = team._parts().messenger.names
    agent = names.resolve(teammate, names.root_of(lead))
    run = (task_id, run_id, lane, reason)
    if agent is None or sid is None or agent.subagent_id != sid:
        logger.warning("subagents: team card %s was taken back; who runs it is unknown, its locks stay", task_id)
    elif agent.state not in LIVE:
        _put_back(*run)
    elif _stop_runs(names, agent, [run]) is not None:
        logger.warning("subagents: team card %s was taken back; %s did not stop, its locks stay", task_id, teammate)


def drain(state: Any, task_id: str) -> tuple | None:
    """Moves the card's held entry aside (caller holds ``state.lock``) so no claim frees its locks; the entry."""
    entry = state.held.pop(task_id, None)
    if entry is not None:
        _drained()[(task_id, entry[2])] = entry
    return entry


def finish_stop(subagent_id: str) -> bool:
    """The stopped agent ``subagent_id`` exited: its runs' cards go back; False when it was not stopped."""
    stops = _stops()
    with stops["lock"]:
        pending = stops["pending"].pop(subagent_id, None)
    for run in pending or ():
        _put_back(*run)
    return pending is not None


def _stop_runs(names: Any, agent: Any, runs: list) -> str | None:
    """Records the stop of ``agent`` and its ``runs`` before stock's stop (its exit may come first); a refusal
    withdraws them. The error, or None."""
    stops, sid = _stops(), agent.subagent_id
    with stops["lock"]:
        stops["pending"].setdefault(sid, []).extend(runs)
    error = _stop_call(names, agent)
    if error is not None:
        with stops["lock"]:
            left = [run for run in stops["pending"].get(sid, []) if run not in runs]
            if left:
                stops["pending"][sid] = left
            else:
                stops["pending"].pop(sid, None)
        logger.warning("subagents: stopping %s failed: %s", agent.name, error)
    return error


def _stop_call(names: Any, agent: Any) -> str | None:
    """Stock ``delegate_task(action="stop")`` for ``agent``, called as its owner (messages.py); the error, or None."""
    owner = (agent.owner_ref() if agent.owner_ref else None) or names.main_agent(agent.owner_session)
    import tools.delegate_tool as delegate_tool

    try:
        outcome = delegate_tool.delegate_task(action="stop", subagent_id=agent.subagent_id,
                                              parent_agent=owner or SimpleNamespace(session_id=agent.owner_session))
    except Exception as exc:  # stock refused for this owner: reported, the rest of the team still stops
        return "%s: %s" % (type(exc).__name__, exc)
    if "error" not in str(outcome):
        return None
    try:
        return str(json.loads(outcome).get("error") or outcome)
    except (TypeError, ValueError, AttributeError):
        return str(outcome)


def _put_back(task_id: str, run_id: Any, lane: str, reason: str) -> None:
    """The card back on the board while it still runs claim ``run_id``, with that run's file locks (held or
    drained); a card another run claimed meanwhile is left alone."""
    guard, state = team._guard(team._team(lane)), team._state()
    kb, closing = team._kanban()
    with state.lock, closing as conn:
        card = kb.get_task(conn, task_id)
        entry = state.held.get(task_id)
        if card is not None and card.status == "running" and card.current_run_id == run_id:
            kb.reclaim_task(conn, task_id, reason=reason)
            if entry is not None and entry[2] == run_id:
                team._free(state, guard, task_id)
        drained = _drained().pop((task_id, run_id), None)
        if drained and drained[1]:
            guard.release(drained[1])
            guard.note(drained[0], "%s releases %s" % (drained[0], ", ".join(h.path for h in drained[1])))


def _stops() -> dict:
    """Stops waiting for their agent's exit: ``subagent id -> [(task id, run id, lane, reason)]``."""
    return process_shared("team_stops", lambda: {"lock": threading.Lock(), "pending": {}})


def _drained() -> dict:
    """File locks of runs taken back while their teammate may still write: ``(task id, run id) -> held entry``
    (guarded by team.py's ``state.lock``)."""
    return process_shared("team_drained", dict)
