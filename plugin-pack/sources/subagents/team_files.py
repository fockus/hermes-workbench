"""File protection and the team brief for team mode (REQ-028; design S5): the Memory Bank skill's own scripts.

A teammate's task names the files it edits. Before the lead side claims the card for it, every file gets a lease lock
through ``mb-lock.sh acquire`` (bank-scoped, so the lock lives in ``<bank>/.locks/res``) and the claim is recorded on
``<bank>/COORDINATION.md`` through ``mb-coord.sh append``; nothing here reimplements either. The team brief is a
plain markdown file in the bank (``teams/<team id>.md``).

The skill is installed for the OS user: ``$MB_SKILLS_ROOT``, else ``~/.claude/skills/memory-bank`` of ``$HOME`` and
then of the account's own home directory (a process may run with HOME moved, the skill stays where it was
installed). Without the scripts a team is refused: edits must not go unprotected.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pwd
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

SKILL_DIR = Path(".claude") / "skills" / "memory-bank"
SCRIPTS = ("mb-lock.sh", "mb-coord.sh")
TRACK_OK = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
LOCK_TIMEOUT = 60  # seconds one script call may take
TRY_ONCE = "0.01"  # mb-lock --wait: give up at once when the file is held (``0`` would wait forever)
WAIT_TIMEOUT = 75  # mb-lock.sh exit status: the lock is held by someone else

logger = logging.getLogger("subagents.team")


class FileGuardError(RuntimeError):
    """The skill scripts are missing or refused a call that must succeed."""


def skill_scripts() -> Path | None:
    """The skill's scripts folder holding mb-lock.sh and mb-coord.sh, or None when the skill is not installed."""
    roots = [os.environ.get("MB_SKILLS_ROOT"), Path.home() / SKILL_DIR, Path(pwd.getpwuid(os.getuid()).pw_dir) / SKILL_DIR]
    for root in roots:
        folder = Path(root) / "scripts" if root else None
        if folder and all((folder / name).is_file() for name in SCRIPTS):
            return folder
    return None


def normalized(workspace: Path, path: str) -> str:
    """``path`` relative to the workspace, normalized; ValueError when it leaves the workspace."""
    raw = Path(path)
    full = Path(os.path.normpath(raw if raw.is_absolute() else workspace / raw))
    try:
        return PurePosixPath(full.relative_to(Path(os.path.normpath(workspace)))).as_posix()
    except ValueError:
        raise ValueError("file %s is outside the workspace %s" % (path, workspace)) from None


@dataclass(frozen=True)
class Held:
    """One lease lock: the file, its mb-lock resource and the release token."""

    path: str
    resource: str
    token: str


class FileGuard:
    """Lease locks on workspace files and their claims on the board, all through the skill's scripts."""

    def __init__(self, scripts: Path, bank: Path, workspace: Path):
        self.scripts, self.bank, self.workspace = scripts, bank, workspace

    def acquire(self, owner: str, files: Sequence[str], lease: int) -> list[Held] | None:
        """Every file locked for ``lease`` seconds, or None (and nothing kept) when one is held by another task;
        on a FileGuardError the locks taken so far are released first (leases never expire on their own)."""
        held: list[Held] = []
        try:
            # one entry per lock key (two spellings of one file must not block each other) and one order for every
            # claimer, so no two claims wait on each other
            for resource, path in sorted({self._resource(p): p for p in files}.items()):
                out = self._run("mb-lock.sh", "acquire", "--owner", owner, "--lease", str(lease), "--wait", TRY_ONCE,
                                resource=resource, ok=(0, WAIT_TIMEOUT))
                if out.returncode == WAIT_TIMEOUT or not out.stdout.strip():
                    self.release(held)
                    return None
                held.append(Held(path, resource, out.stdout.strip().splitlines()[-1]))
        except FileGuardError:
            self.release(held)
            raise
        return held

    def release(self, held: Iterable[Held]) -> None:
        for lock in held:
            self._run("mb-lock.sh", "release", "--token", lock.token, resource=lock.resource, ok=(0, 1))

    def note(self, who: str, text: str) -> None:
        """One STATUS entry on COORDINATION.md (``who:`` prefixed when it is a valid track id); best effort: the
        entry is a record, the locks are the protection."""
        track = ["--track", who] if who[:1].isalpha() and len(who) <= 16 and set(who) <= TRACK_OK else []
        try:
            self._run("mb-coord.sh", "append", "--type", "STATUS", "--title", text, *track)
        except FileGuardError as exc:
            logger.warning("subagents: the board entry %r was not written: %s", text, exc)

    def _resource(self, path: str) -> str:
        # casefolded and NFC: on APFS (case- and normalization-insensitive) x/A.py and x/a.py, or an NFC and an NFD
        # spelling, are one file
        key = unicodedata.normalize("NFC", unicodedata.normalize("NFC", str(self.workspace / path)).casefold())
        return "file-%s" % hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]

    def _run(self, script: str, *args: str, resource: str = "", ok: Sequence[int] = (0,)):
        spec = None
        command = ["bash", str(self.scripts / script), *args, "--mb", str(self.bank)]
        try:
            if resource:  # inside the try: a temp-dir OSError is a FileGuardError, so acquire releases what it took
                spec = tempfile.NamedTemporaryFile("w", suffix=".json", prefix="mb-lock-", delete=False)
                json.dump({"name": resource, "kind": "mutex", "scope": "bank"}, spec)
                spec.close()
            out = subprocess.run(command + (["--resource-json", spec.name] if spec else []), capture_output=True,
                                 text=True, timeout=LOCK_TIMEOUT, cwd=str(self.workspace))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FileGuardError("%s %s failed: %s" % (script, args[0], exc)) from exc
        finally:
            if spec:
                os.unlink(spec.name)
        if out.returncode not in ok:
            raise FileGuardError("%s %s exited %d: %s" % (script, args[0], out.returncode, out.stderr.strip()[-300:]))
        return out


def write_brief(bank: Path, team_id: str, lead: str, crew: Sequence[str], tasks: Sequence[dict]) -> Path:
    """The team brief in the bank: lead, teammates, the board's tasks and the working rules."""
    lines = ["# Team %s" % team_id, "", "- Lead: %s" % lead, "- Teammates: %s" % ", ".join(crew),
             "- Board: Kanban lane `%s` (not a profile; the dispatcher skips it)" % team_id, "", "## Tasks", ""]
    for task in tasks:
        after = ", ".join(task["depends_on"]) or "nothing"
        files = ", ".join(task["files"]) or "none"
        lines.append("- %s (after: %s; files: %s)" % (task["title"], after, files))
    lines += ["", "## Rules", "", "- A teammate that finishes claims the next unassigned, unblocked task.",
              "- Files are edited only under the task's mb-lock locks; claims are on COORDINATION.md.",
              "- Teammates may launch subagents; they never start a team of their own.", ""]
    path = bank / "teams" / ("%s.md" % team_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
