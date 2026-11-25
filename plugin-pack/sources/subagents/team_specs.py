"""A team's task list validated and its lane id chosen (Task 10, REQ-023; split from team.py for size)."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Sequence

from .team_files import normalized

LANE_PREFIX = "team-"


class TeamRefused(ValueError):
    """The team, claim or change request is not allowed; the message says why."""


def specs_of(tasks: Sequence[dict], workspace: Path) -> list[dict]:
    """Validated tasks: unique titles, known dependencies with no cycle, files inside the workspace."""
    titles = [str((t or {}).get("title") or "").strip() for t in tasks]
    if not titles or not all(titles) or len(set(titles)) != len(titles):
        raise TeamRefused("a team needs tasks with unique, non-empty titles")
    specs = []
    for task, title in zip(tasks, titles):
        if any(not isinstance(v, (list, tuple)) or not all(isinstance(x, str) and x.strip() for x in v)
               for v in (task.get("files") or [], task.get("depends_on") or [])):
            raise TeamRefused("task %s: files and depends_on are lists of strings, each non-empty" % title)
        deps = [str(d) for d in task.get("depends_on") or ()]
        unknown = [d for d in deps if d not in titles]
        if unknown:
            raise TeamRefused("task %s depends on unknown task %s" % (title, ", ".join(unknown)))
        try:
            files = sorted({normalized(workspace, str(f)) for f in task.get("files") or ()})
        except ValueError as exc:
            raise TeamRefused(str(exc)) from None
        specs.append({"title": title, "depends_on": deps, "files": files, "verify": str(task.get("verify") or "")})
    done: set[str] = set()
    while len(done) < len(specs):
        free = [s["title"] for s in specs if s["title"] not in done and set(s["depends_on"]) <= done]
        if not free:
            raise TeamRefused("task dependencies form a cycle")
        done.update(free)
    return specs


def lane() -> str:
    """A new team id that is no Hermes profile, so the stock dispatcher never spawns a worker for its cards."""
    from hermes_cli.profiles import profile_exists

    while True:
        lane_id = LANE_PREFIX + secrets.token_hex(4)
        if not profile_exists(lane_id):
            return lane_id
