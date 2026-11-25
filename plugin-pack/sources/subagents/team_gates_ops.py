"""What the lead and kanban-heartbeat do to a team (Task 11; REQ-026, REQ-027, REQ-030): ``/team status|send|stop``,
the progress line of the lead's session card, and the team lane's owner contract (nudge, continue, stop, escalate)
registered with kanban-heartbeat's ``heartbeat.api.register_owner`` when that module is importable. Expired claims are
heartbeat's (its claim rule over stock ``claim_expires``); nothing here sweeps them. Stops: team_gates_stop.py. Split
from team_gates.py for size.
"""

from __future__ import annotations

import importlib
import json
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any

from . import team
from .callers import active_parent
from .messages import COMMAND, MAIN
from .team_gates_stop import claimant, stop_teammate
from .team_specs import LANE_PREFIX

logger = logging.getLogger("subagents.team")

LANE_MATCH = LANE_PREFIX + "*"  # heartbeat's lane glob over the assignee
ORDER = ("running", "review", "ready", "todo", "blocked", "done", "archived")
USAGE = "Usage: /team status | /team send <name> <text> | /team stop"


def send(name: str, text: str, lead: str, sender: str = MAIN) -> bool:
    """``text`` to teammate ``name`` of the lead's conversation through the agent API; False (logged) when undelivered."""
    from .api import SubagentsAPI

    try:
        SubagentsAPI().send(name, text, sender, session=lead)
    except RuntimeError as exc:
        logger.warning("subagents: team message to %s not delivered: %s", name, exc)
        return False
    return True


@dataclass(frozen=True)
class Board:
    """The open team a lead's conversation leads (``team_id`` "" when none)."""

    team_id: str

    @classmethod
    def of(cls, root: str) -> "Board":
        kb, closing = team._kanban()
        with closing as conn:
            row = conn.execute("SELECT assignee FROM tasks WHERE created_by = ? AND assignee LIKE ? AND status NOT IN "
                               "(?, ?) ORDER BY created_at DESC LIMIT 1",
                               (root, LANE_PREFIX + "%", *team.CLOSED)).fetchone() if root else None
        return cls(str(row["assignee"]) if row else "")

    def cards(self) -> list[Any]:
        kb, closing = team._kanban()
        with closing as conn:
            cards = kb.list_tasks(conn, assignee=self.team_id)
        return sorted(cards, key=lambda c: json.loads(c.body or "{}").get("index", 0))

    def progress(self) -> str:
        """One line: the team and its cards by status (kanban-gantt shows the same lane)."""
        if not self.team_id:
            return ""
        counts = Counter(c.status for c in self.cards())
        parts = ["%d %s" % (counts[s], s) for s in ORDER if counts[s]]
        return "[team %s] %d tasks: %s" % (self.team_id, sum(counts.values()), ", ".join(parts))

    def status(self) -> str:
        if not self.team_id:
            return "No open team in this conversation."
        lines = [self.progress()]
        for card in self.cards():
            who = team.teammate_of(card.id) if card.status in ("running", "review") else None
            lines.append("- [%s] %s (%s)%s" % (card.status, card.title, card.id, " - %s" % who if who else ""))
        return "\n".join(lines)

    @staticmethod
    def command(raw_args: str = "", session_id: Any = "", platform: Any = "", session_key: Any = "",
                source: Any = None, **_: Any) -> str:
        """``/team``: the board of the calling conversation's team, a message to a teammate, or stop them all. A
        surface that names a chat but no session is refused, never guessed (as ``/subagents send``)."""
        names = team._parts().messenger.names
        agent = active_parent()
        session = str(getattr(agent, "session_id", "") or "") if agent is not None else str(session_id or "")
        if not session and (platform or session_key or source is not None):
            return "This chat has no conversation with a team yet."
        board = Board.of(names.root_of(session) if session else names.sole_root())
        verb, _, rest = (raw_args or "status").strip().partition(" ")
        if verb not in ("status", "send", "stop"):
            return USAGE
        if verb == "status":
            return board.status()
        if not board.team_id:
            return "No open team in this conversation."
        crew = team._team(board.team_id)
        if verb == "send":
            name, _, text = rest.strip().partition(" ")
            if name not in crew.crew or not text.strip():
                return "Usage: /team send <teammate> <text>; teammates: %s" % ", ".join(crew.crew)
            return ("Sent to %s." % name) if send(name, text, crew.lead, COMMAND) else "Not sent to %s." % name
        errors = {name: stop_teammate(board.team_id, name, crew.lead, "the lead stopped the team") for name in crew.crew}
        stopped = [name for name, error in errors.items() if error is None]
        reply = "Stopping %s; their running cards go back to the board when they exit." % (", ".join(stopped) or
                                                                                          "no teammate")
        failed = ["%s (%s)" % (name, error) for name, error in errors.items() if error is not None]
        return reply + (" Not stopped, their cards stay claimed: %s." % "; ".join(failed) if failed else "")


class TeamOwner:
    """kanban-heartbeat's owner contract for team lanes (heartbeat design: OwnerContract); nudge, continue and stop
    act on the agent the card's running claim was made for, and are False when there is none."""

    def _card(self, task_id: str) -> Any:
        kb, closing = team._kanban()
        with closing as conn:
            card = kb.get_task(conn, task_id)
        return card if card is not None and str(card.assignee or "").startswith(LANE_PREFIX) else None

    def _of(self, task_id: str) -> tuple[Any, str] | None:
        card = self._card(task_id)
        lane, _, name = str(getattr(card, "claim_lock", "") or "").partition(":")
        if card is None or card.status != "running" or lane != card.assignee or not name:
            return None
        crew = team._team(card.assignee)
        return (crew, name) if claimant(card.id, card.current_run_id, name, crew.lead) is not None else None

    def nudge(self, task_id: str, text: str) -> bool:
        found = self._of(task_id)
        return bool(found) and send(found[1], text, found[0].lead)

    def continue_(self, task_id: str) -> bool:
        found = self._of(task_id)
        return bool(found) and send(found[1], "Continue your team task %s and report it done." % task_id,
                                    found[0].lead)

    def stop(self, task_id: str, reason: str) -> bool:
        found = self._of(task_id)
        return bool(found) and stop_teammate(found[0].team_id, found[1], found[0].lead,
                                             reason or "stopped by kanban-heartbeat") is None

    def escalate(self, task_id: str, signal: str, detail: str) -> bool:
        """The user decides: the card is blocked with needs_input, which heartbeat delivers as one question."""
        if self._card(task_id) is None:
            return False
        kb, closing = team._kanban()
        with closing as conn:
            blocked = kb.block_task(conn, task_id, kind="needs_input", reason="%s: %s" % (signal, detail))
        team.release(task_id)
        return bool(blocked)


def register_owner(contract: TeamOwner) -> bool:
    """Registers the team lane with kanban-heartbeat; quietly nothing while its phase 2 API is not importable."""
    try:
        api = importlib.import_module("heartbeat.api")
    except ImportError:
        return False
    register = getattr(api, "register_owner", None)
    if not callable(register):
        return False
    register(LANE_MATCH, contract)
    return True
