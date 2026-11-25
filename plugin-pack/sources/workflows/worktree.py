"""Worktree isolation of agents (workflow-engine Task 13, REQ-027): an ``AgentHost`` decorator, so agents.py
stays as it is.

``Isolated(inner, cwd, root=None)`` wraps any AgentHost. ``cwd`` is the workspace, a path or a callable that
returns it (WE-WIRE binds the session's; never this process's cwd); ``root`` holds the worktrees, by default
``store.runs_root().parent / "worktrees"``. ``abort``, ``gate`` and ``load`` pass through, and so does ``spawn``
of a request without ``isolation == "worktree"``. An isolated spawn:
  the repo is ``git -C <cwd> rev-parse --show-toplevel`` (none: ``{ok: False, error}``, no spawn);
  ``git worktree add -b wf/<run id>/<slug> <root>/<run id>/<slug> HEAD`` (``slug``: the label reduced to
  ``[A-Za-z0-9._-]``, at most SLUG_CHARS, else ``agent-<index>``; a taken branch gets ``-2``, ``-3``);
  ``inner.spawn({**req, "cwd": <worktree>})``; then, whatever the agent's result, changes are committed on that
  branch and the branch is reported (also when the agent committed them itself); a branch still at its start is
  deleted. A request ``gate`` runs through ``inner.gate`` in the worktree and its outcome is the result's ``gate``.
  The worktree is then removed, unless saving the work failed: then it is kept and reported (``kept``, ``error``).
  One JSON line ``{label, branch, changed}`` is appended (0600) to ``<run folder>/worktrees.jsonl`` and the
  spawn result gets ``cwd`` and, when kept, ``branch``. The main checkout is never written. The label passes
  through the stock redactor before it names a branch, a commit or a report line.
Git calls run with GIT_TIMEOUT and under one lock per repository (keyed on its shared git dir: parallel agents
and linked worktrees share its branches); the agent itself runs outside that lock.
``resume`` of an isolated agent is refused (its worktree is gone); other resumes pass through.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, NamedTuple

from .agents import WITHHELD, _redacted
from .store import FILE_MODE, RUN_ID, RunStore, runs_root

log = logging.getLogger(__name__)

GIT_TIMEOUT = 60.0
SLUG_CHARS = 40
NO_RESUME = "resume of a worktree-isolated agent is not supported yet"
# The kept branch is a scratch result: the repo's hooks and signing must not block or prompt.
COMMIT = ("commit", "-q", "--no-verify", "--no-gpg-sign", "-m")
IDENTITY = ("-c", "user.name=hermes-workflow", "-c", "user.email=workflow@localhost")
_REPO_LOCKS: dict[str, threading.Lock] = {}
_GUARD = threading.Lock()


class GitError(RuntimeError):
    pass


class _Spot(NamedTuple):
    repo: str
    key: str
    path: Path
    branch: str
    start: str
    message: str


class Isolated:
    def __init__(self, inner: Any, cwd: str | Path | Callable[[], Any], root: Path | None = None):
        self._inner, self._cwd, self._root = inner, cwd, root
        self._isolated: set[str] = set()

    def spawn(self, req: dict) -> dict:
        if req.get("isolation") != "worktree":
            self._isolated.discard(str(req.get("agentId") or ""))
            return self._inner.spawn(req)
        where = self._cwd() if callable(self._cwd) else self._cwd
        try:
            repo = _git(where, "rev-parse", "--show-toplevel").strip() if where else ""
            key = _repo_key(repo) if repo else ""
        except GitError:
            repo = ""
        if not repo:
            return {"ok": False, "error": "worktree isolation needs a git repository: %s" % where}
        run = str(req.get("runId") or "")
        run = run if RUN_ID.fullmatch(run) else "adhoc"
        label = _redacted(str(req["label"])) if req.get("label") else None
        label = None if label == WITHHELD else label
        base = _slug(label or "") or "agent-%s" % req.get("index", 0)
        root = Path(self._root or runs_root().parent / "worktrees") / run
        try:
            with _lock(key):
                branch, path = _add(repo, root, run, base)
                start = _git(repo, "rev-parse", "refs/heads/" + branch).strip()
        except GitError as exc:
            return {"ok": False, "error": "worktree isolation failed: %s" % exc}
        self._isolated.add(str(req.get("agentId") or ""))
        spot = _Spot(repo, key, path, branch, start, "workflow %s %s" % (run, label or base))
        try:
            result = self._inner.spawn({**req, "cwd": str(path)})
        except BaseException:
            self._finish(spot, run, label, None, None)
            raise
        return self._finish(spot, run, label, result, req.get("gate"))

    def _finish(self, spot: "_Spot", run: str, label: str | None, result: Any, gate: Any) -> Any:
        """Save the agent's work on its branch, run its gate in the worktree, then remove the worktree; a save
        that fails keeps the worktree (its edits are nowhere else) and reports its path."""
        try:
            with _lock(spot.key):
                changed, error = _save(spot), None
        except GitError as exc:
            changed, error = None, str(exc)
        if gate and isinstance(result, dict) and result.get("ok"):
            result = {**result, "gate": self._inner.gate(str(gate), str(spot.path))}
        if error is None:
            with _lock(spot.key):
                _drop(spot, changed)
            row = {"label": label, "branch": spot.branch if changed else None, "changed": changed}
        else:
            log.warning("worktree %s kept: saving the agent's work failed: %s", spot.path, error)
            row = {"label": label, "branch": spot.branch, "changed": None, "kept": str(spot.path), "error": error}
        _report(run, row)
        if not isinstance(result, dict):
            return result
        result = {**result, "cwd": str(spot.path)}
        return {**result, "branch": spot.branch} if changed or error else result

    def resume(self, agent_id: str, prompt: str) -> dict:
        if agent_id in self._isolated:
            # ponytail: the worktree is removed when the spawn returns, so there is nothing to resume in; upgrade:
            # keep an isolated agent's worktree until its run ends and resume it there.
            return {"ok": False, "error": NO_RESUME}
        return self._inner.resume(agent_id, prompt)

    def abort(self, agent_id: str) -> None:
        return self._inner.abort(agent_id)

    def gate(self, command: str, cwd: str | None = None) -> dict:
        return self._inner.gate(command, cwd)

    def load(self, ref: Any) -> dict:
        return self._inner.load(ref)


def _slug(label: str) -> str:
    slug = re.sub(r"\.{2,}", ".", re.sub(r"[^A-Za-z0-9._-]+", "-", label))
    slug = slug[:SLUG_CHARS].strip("-._")
    return slug[:-5] if slug.endswith(".lock") else slug


def _add(repo: str, root: Path, run: str, base: str) -> tuple[str, Path]:
    n = 1
    while True:
        name = base if n == 1 else "%s-%d" % (base, n)
        branch, path = "wf/%s/%s" % (run, name), root / name
        if not path.exists() and not _has_branch(repo, branch):
            break
        n += 1
    root.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", "-b", branch, str(path), "HEAD")
    return branch, path


def _save(spot: "_Spot") -> bool:
    """Commit the worktree's changes on its branch; True when the branch now differs from where it started (the
    agent may have committed its work itself)."""
    if _git(spot.path, "status", "--porcelain").strip():
        _git(spot.path, "add", "-A")
        _git(spot.path, *(() if _has_identity(spot.repo) else IDENTITY), *COMMIT, spot.message)
    return _git(spot.repo, "rev-parse", "refs/heads/" + spot.branch).strip() != spot.start


def _drop(spot: "_Spot", changed: bool) -> None:
    _git(spot.repo, "worktree", "remove", "--force", str(spot.path))
    _git(spot.repo, "worktree", "prune")
    if not changed:
        _git(spot.repo, "branch", "-q", "-D", spot.branch)


def _repo_key(repo: str) -> str:
    """The repository's shared git dir: linked worktrees of one repo share its branches, so they share one lock."""
    common = _git(repo, "rev-parse", "--git-common-dir").strip()
    return str((Path(repo) / common).resolve())


def _has_branch(repo: str, branch: str) -> bool:
    try:
        _git(repo, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch)
    except GitError:
        return False
    return True


def _has_identity(repo: str) -> bool:
    try:
        return all(_git(repo, "config", key).strip() for key in ("user.name", "user.email"))
    except GitError:
        return False


def _report(run: str, row: dict) -> None:
    folder = RunStore().folder(run)
    if folder is None or folder.is_symlink() or not folder.is_dir():
        return
    fd = os.open(folder / "worktrees.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, FILE_MODE)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        os.fchmod(fh.fileno(), FILE_MODE)
        fh.write(json.dumps(row) + "\n")


def _lock(repo: str) -> threading.Lock:
    with _GUARD:
        return _REPO_LOCKS.setdefault(repo, threading.Lock())


def _git(where: Any, *args: str) -> str:
    try:
        done = subprocess.run(["git", "-C", str(where), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError("git %s: %s" % (args[0], exc)) from exc
    if done.returncode:
        raise GitError("git %s failed: %s" % (args[0], done.stderr.strip()[-300:]))
    return done.stdout
