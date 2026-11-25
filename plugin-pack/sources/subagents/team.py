"""Team mode: the board, dependencies, locked claims and self-claim (Task 10; REQ-023, REQ-024, REQ-028, REQ-029;
design S5, D-10, D-15, D-20; SPIKE.md § 6; hermes-teammates patterns, AGR-004).

A team is the main agent (lead) and 3 to 5 named background teammates the lead has already spawned. Its board is a
Kanban lane: every card's assignee is the team id ``team-<hex>``, which is not a Hermes profile, so the stock
dispatcher never spawns a worker for it. Dependencies are stock ``link_tasks`` edges and a claim is stock
``claim_task`` (``ready -> running`` under a claim lock ``<team id>:<teammate>``). Teammates never write Kanban (they
run inside the delegate fence): the lead side claims on their behalf with ``claim_next``, which gives a free teammate
the first ready card whose parents are done and whose files it can lock (team_files.py: mb-lock.sh leases and
COORDINATION.md claims); a teammate with a running card gets nothing. Locks of a card that is no longer running are
released at the next claim, or at once with ``release`` (the gates call it). One open team per lead session; a
session that is itself a teammate or subagent cannot lead one, though its own subagents work as usual.

Each card's body carries the team, its task and ``claim_ttl``; with stock ``started_at``/``claim_expires`` that is all
kanban-heartbeat's claim rule (D-24, heartbeat REQ-019) needs to return an expired claim, so there is no sweep here.
Task validation and the lane id: team_specs.py. Public for team_gates.py: ``create_team``, ``claim_next``, ``release``, ``teammate_of``, ``request_changes``.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence

from .limiter import process_shared
from .messages import MAIN
from .names import LOST
from .orchestrator import resolve_bank
from .team_files import FileGuard, Held, skill_scripts, write_brief
from .team_specs import LANE_PREFIX, TeamRefused, lane, specs_of

logger = logging.getLogger("subagents.team")

MIN_TEAM, MAX_TEAM = 3, 5
CLAIM_TTL = 15 * 60  # seconds; stock's default claim TTL, also the lease of the card's file locks
MIN_CLAIM_TTL = 3  # stock keeps claim_expires in whole seconds: a shorter TTL leaves no time to renew it
CLOSED = ("done", "archived")


@dataclass(frozen=True)
class Team:
    team_id: str
    lead: str
    crew: tuple[str, ...]
    workspace: Path
    bank: Path  # the workspace's bank as the memory-bank plugin resolves it (a parent folder's, say)


@dataclass
class TeamState:
    """The teams; per running card ``task id -> (teammate, locks, run id, subagent id or None)`` of its claim."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    teams: dict[str, Team] = field(default_factory=dict)
    held: dict[str, tuple[str, list[Held], Any, Any]] = field(default_factory=dict)


def _state() -> TeamState:
    return process_shared("team_state", TeamState)


def _parts() -> Any:
    """The registered plugin's messenger and workspace callable (api.py ``PluginParts``) of this Hermes home."""
    from hermes_constants import get_hermes_home

    parts = process_shared("api_parts", dict).get(str(get_hermes_home()))
    if parts is None or parts.messenger is None:
        raise TeamRefused("team mode needs the subagents plugin loaded in this profile")
    return parts


def _kanban():
    import hermes_cli.kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing

    return kb, connect_closing()


def _meta(card: Any) -> dict:
    return json.loads(card.body or "{}")


def _open_team(conn: Any, lead: str) -> bool:
    row = conn.execute("SELECT 1 FROM tasks WHERE created_by = ? AND assignee LIKE ? AND status NOT IN (?, ?) LIMIT 1",
                       (lead, LANE_PREFIX + "%", *CLOSED)).fetchone()
    return row is not None


def _bank_above(root: Path) -> Path:
    """The nearest bank above (as the plugin finds it), never HOME's, above HOME or the root's; else the workspace's."""
    home = Path.home().resolve()
    for folder in root.parents:
        if folder == folder.parent or home.is_relative_to(folder):
            break
        if (folder / ".memory-bank").is_dir():
            return folder / ".memory-bank"
    return root / ".memory-bank"


def create_team(lead_session_id: str, teammates: Sequence[str], tasks: Sequence[dict], *,
                claim_ttl: int = CLAIM_TTL) -> str:
    """The team's id (its lane). ``teammates`` are names of agents the lead spawned; a task is ``{"title",
    "depends_on": [titles], "files": [workspace paths], "verify": shell command}``. Raises TeamRefused."""
    parts, lead, crew = _parts(), str(lead_session_id or ""), tuple(str(n) for n in teammates)
    if not lead or int(claim_ttl) < MIN_CLAIM_TTL:
        raise TeamRefused("claim_ttl must be %d s or more" % MIN_CLAIM_TTL if lead else "a team needs the lead's session")
    if parts.messenger.callers.get(lead) is not None:
        raise TeamRefused("nested teams are refused: %s is a teammate or subagent; it may launch subagents, "
                          "not a team" % lead)
    if not MIN_TEAM <= len(crew) <= MAX_TEAM or len(set(crew)) != len(crew):
        raise TeamRefused("a team has 3 to 5 distinct teammates, got %d" % len(set(crew)))
    names = parts.messenger.names
    owner = names.root_of(lead)  # the conversation, kept through compaction rotations of the lead's session
    unknown = [n for n in crew if n == MAIN or not _alive(names.resolve(n, owner))]
    if unknown:
        raise TeamRefused("no live named agent %s in this session (unknown or lost): spawn each teammate with a "
                          "name first" % ", ".join(unknown))
    workspace = (parts.cwd() if parts.cwd else "") or ""
    if not workspace:
        raise TeamRefused("team mode needs a workspace (the bank, locks and claims live there)")
    root = Path(workspace).resolve()
    bank = resolve_bank(str(root)) or _bank_above(root)
    team = Team("", lead, crew, root, bank)
    specs = specs_of(tasks, team.workspace)
    if skill_scripts() is None:
        raise TeamRefused("the Memory Bank skill (mb-lock.sh, mb-coord.sh) is not installed; team file edits "
                          "cannot be protected")
    kb, closing = _kanban()
    state = _state()
    with state.lock, closing as conn:
        if _open_team(conn, owner):
            raise TeamRefused("one team per session: this session already leads an open team")
        team = replace(team, team_id=lane())
        team.bank.mkdir(exist_ok=True)
        write_brief(team.bank, team.team_id, lead, crew, specs)  # before any card: a failure publishes nothing
        ids: dict[str, str] = {}
        for index, spec in enumerate(specs):
            meta = {"team": team.team_id, "lead": lead, "crew": list(crew), "workspace": str(team.workspace),
                    "bank": str(team.bank), "index": index, "claim_ttl": int(claim_ttl), **spec}
            ids[spec["title"]] = kb.create_task(conn, title=spec["title"], body=json.dumps(meta),
                                                assignee=team.team_id, created_by=owner)
        for spec in specs:
            for parent in spec["depends_on"]:
                kb.link_tasks(conn, ids[parent], ids[spec["title"]])
        state.teams[team.team_id] = team
    logger.info("subagents: team %s of %s with %s, %d tasks", team.team_id, lead, ", ".join(crew), len(specs))
    return team.team_id


def _team(team_id: str) -> Team:
    """The team from this process, else rebuilt from its cards (another process created it)."""
    state = _state()
    with state.lock:
        known = state.teams.get(team_id)
    if known is not None:
        return known
    kb, closing = _kanban()
    with closing as conn:
        cards = kb.list_tasks(conn, assignee=team_id, include_archived=True, limit=1)
    if not team_id.startswith(LANE_PREFIX) or not cards:
        raise TeamRefused("no team %s" % team_id)
    meta = _meta(cards[0])
    team = Team(team_id, meta["lead"], tuple(meta["crew"]), Path(meta["workspace"]),
                Path(meta.get("bank") or Path(meta["workspace"]) / ".memory-bank"))
    with state.lock:
        return state.teams.setdefault(team_id, team)


def _guard(team: Team) -> FileGuard:
    scripts = skill_scripts()
    if scripts is None:
        raise TeamRefused("the Memory Bank skill (mb-lock.sh, mb-coord.sh) is not installed")
    return FileGuard(scripts, team.bank, team.workspace)


def _free(state: TeamState, guard: FileGuard, task_id: str) -> None:
    """Frees the card's file locks, noted on the board (caller holds ``state.lock``); a raising release keeps them."""
    entry = state.held.get(task_id)
    if entry and entry[1]:
        guard.release(entry[1])
        guard.note(entry[0], "%s releases %s" % (entry[0], ", ".join(h.path for h in entry[1])))
    state.held.pop(task_id, None)


def _alive(agent: Any) -> bool:
    return agent is not None and agent.state != LOST


def _take(conn: Any, kb: Any, guard: FileGuard, state: TeamState, team_id: str, card: Any, teammate: str,
          agent: Any) -> bool:
    """Claims ``card`` for ``teammate`` (``agent``) under its file locks, caller holds ``state.lock``; False if lost."""
    meta = _meta(card)
    ttl = int(meta.get("claim_ttl") or CLAIM_TTL)
    held = guard.acquire(teammate, meta.get("files") or [], ttl)
    if held is None:
        return False
    try:
        claimed = kb.claim_task(conn, card.id, ttl_seconds=ttl, claimer="%s:%s" % (team_id, teammate),
                                expect_assignee=team_id)
    except Exception:
        guard.release(held)
        raise
    if claimed is None:
        guard.release(held)
        return False
    state.held[card.id] = (teammate, held, claimed.current_run_id, getattr(agent, "subagent_id", None))
    if held:
        guard.note(teammate, "%s claims %s (%s, %s)" % (teammate, ", ".join(h.path for h in held), card.title,
                                                        team_id))
    return True


def claim_next(team_id: str, teammate: str) -> str | None:
    """Claims the next ready, unblocked card whose files are free for ``teammate``; its id, or None when the
    teammate already runs one, is lost (a restart ended it) or nothing is claimable now."""
    team = _team(team_id)
    if teammate not in team.crew:
        raise TeamRefused("%s is not a teammate of %s" % (teammate, team_id))
    names = _parts().messenger.names
    agent = names.resolve(teammate, names.root_of(team.lead))
    if agent is not None and agent.state == LOST:
        return None
    claimer, guard, state = "%s:%s" % (team_id, teammate), _guard(team), _state()
    kb, closing = _kanban()
    with state.lock, closing as conn:
        cards = kb.list_tasks(conn, assignee=team_id, include_archived=True)  # an archived card's locks too
        for card in cards:
            if card.status != "running" and card.id in state.held:
                _free(state, guard, card.id)
        if any(c.status == "running" and c.claim_lock == claimer for c in cards):
            return None
        for card in sorted((c for c in cards if c.status == "ready"), key=lambda c: _meta(c).get("index", 0)):
            if _take(conn, kb, guard, state, team_id, card, teammate, agent):
                return card.id
    return None


def release(task_id: str) -> None:
    """Releases the file locks the card holds now (finished, reopened or blocked); a no-op when it holds none."""
    state = _state()
    with state.lock:
        entry = state.held.get(task_id)
    if entry is None:
        return
    kb, closing = _kanban()
    with closing as conn:
        card = kb.get_task(conn, task_id)
    guard = _guard(_team(card.assignee)) if card is not None else None
    with state.lock:
        if guard is not None:
            _free(state, guard, task_id)


def teammate_of(task_id: str) -> str | None:
    """The teammate whose claim the card's latest run carries, or None."""
    kb, closing = _kanban()
    with closing as conn:
        run = kb.latest_run(conn, task_id)
    lane, _, name = str(getattr(run, "claim_lock", "") or "").partition(":")
    return name if lane.startswith(LANE_PREFIX) and name else None


def request_changes(team_id: str, task_id: str, feedback: str) -> None:
    """The lead's change request: the card in Review reopens, goes straight back to its teammate (claim and file
    locks, so no other teammate takes it) and the teammate continues with the feedback. When it cannot take the
    card back now, the feedback is left on the card for its next claimer and TeamRefused is raised."""
    team = _team(team_id)
    if not isinstance(feedback, str) or not feedback.strip():
        raise TeamRefused("a change request needs feedback")
    author = teammate_of(task_id)
    if author is None:
        raise TeamRefused("task %s has no teammate to continue it" % task_id)
    guard, state, names = _guard(team), _state(), _parts().messenger.names
    kb, closing = _kanban()
    with state.lock, closing as conn:
        card = kb.get_task(conn, task_id)
        if card is None or card.assignee != team_id:
            raise TeamRefused("task %s is not on team %s" % (task_id, team_id))
        if card.status != "review" or not kb.reopen_review_task(conn, task_id):
            raise TeamRefused("task %s is not waiting in Review (it is %s)" % (task_id, card.status))
        kb.add_comment(conn, task_id, team.lead, feedback)
        _free(state, guard, task_id)
        agent = names.resolve(author, names.root_of(team.lead))
        busy = (agent is not None and agent.state == LOST) or any(  # one running card per teammate, as claim_next
            c.status == "running" and c.claim_lock == "%s:%s" % (team_id, author)
            for c in kb.list_tasks(conn, assignee=team_id))
        if busy or not _take(conn, kb, guard, state, team_id, card, author, agent):
            raise TeamRefused("task %s reopened, but %s cannot take it back now (it is lost or runs another card, "
                              "or a file is held); the feedback is on the card for its next claimer" % (task_id, author))
    reply = _parts().messenger.deliver(author, feedback, MAIN, team.lead)
    if "error" in reply:  # undelivered: the card goes back to the board, the feedback stays on it
        kb, closing = _kanban()
        with state.lock, closing as conn:
            kb.reclaim_task(conn, task_id, reason="feedback delivery failed")
            _free(state, guard, task_id)
        raise RuntimeError(reply["error"])
