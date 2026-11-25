"""Resume and restart of workflow runs (workflow-engine Task 10: REQ-014, REQ-015).

``request(folder, script=None) -> dict | str``: the runner request that resumes the run in ``folder``
(``WorkflowEngine.resume`` imports this module lazily, calls it under ``bridge.run_lock(folder)``, drives the
dict and stores it with ``engine.finish``). A ``str`` is the refusal: ``"run <id> is still running"`` when
another process holds the run's ``run.lock`` (or this process drives it), ``"run <id> has no script"`` without
script.js. Otherwise an edited ``script`` replaces script.js (0600, the store's write), result.json is removed
(the run is unfinished again while it runs) and the request is ``{script, args, runDir, budget, replay?}``:
``args`` and ``budget`` from request.json, ``runDir`` the run folder itself (so the engine's run lock covers
the driven run), ``replay`` the run's journal.jsonl when it is not empty.
``unfinished(store=None) -> list[str]``: run ids whose folder has script.js, no result.json and is not held,
oldest first (a run id starts with its creation stamp); a name that is not a run id, or a symlink, is skipped.
``resume_all(engine, store=None) -> list[RunSummary]``: ``engine.resume(run_id)`` for each unfinished run, one
at a time; a run that raises is logged and skipped. WE-WIRE calls it in a thread from ``on_session_start``.

Phase 1 resumes a run by prefix replay: every agent call settled in journal.jsonl comes back from cache, every
call not settled at the crash or stop is spawned again as a fresh child. It does not re-attach to a child that
is still running after its Hermes process died and does not continue a child's conversation: that child runs
to its own end (bounded by ``limits.AGENT_TIMEOUT``), its answer is lost and its work may be paid twice. A run
held by a live process is never resumed beside it. In-script ``resume`` of an agent stays HermesAgentHost's
fresh spawn.
ponytail: no re-attach to or stop of a live child; ceiling: an in-flight agent at a crash runs twice. Upgrade:
SubagentsAPI resume/stop (backlog I-050, SA2-WIRE) lets resume re-attach instead of re-spawning.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from . import bridge
from .store import RUN_ID, RunStore, _write

log = logging.getLogger(__name__)


def request(folder: Path, script: str | None = None) -> dict | str:
    folder = Path(folder)
    run_id = folder.name
    if not RUN_ID.fullmatch(run_id) or folder.is_symlink() or not folder.is_dir():
        return "%s is not a run folder" % folder
    if run_id in bridge.live() or _held_elsewhere(folder):
        return "run %s is still running" % run_id
    source = folder / "script.js"
    if source.is_symlink() or not source.is_file():
        return "run %s has no script" % run_id
    try:
        stored = json.loads((folder / "request.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = None
    if not isinstance(stored, dict):
        return "run %s has no readable request.json" % run_id
    try:
        text = source.read_text(encoding="utf-8") if script is None else script
    except (OSError, UnicodeDecodeError):
        return "run %s has no readable script" % run_id
    req = {"script": text, "args": stored.get("args"), "runDir": str(folder), "budget": stored.get("budget")}
    journal = folder / "journal.jsonl"
    if journal.is_file() and not journal.is_symlink() and journal.stat().st_size:
        req["replay"] = str(journal)
    if script is not None:
        _write(source, script)
    (folder / "result.json").unlink(missing_ok=True)  # last: a refusal above keeps the previous result
    return req


def unfinished(store: RunStore | None = None) -> list[str]:
    root = (store or RunStore()).root
    if not root.is_dir():
        return []
    found = []
    for folder in sorted(root.iterdir(), key=lambda p: p.name):
        try:
            ok = (RUN_ID.fullmatch(folder.name) and not folder.is_symlink() and folder.is_dir()
                  and (folder / "script.js").is_file() and not (folder / "result.json").exists()
                  and not bridge.held(folder))
        except OSError:  # removed by a concurrent prune
            continue
        if ok:
            found.append(folder.name)
    return found


def resume_all(engine: Any, store: RunStore | None = None) -> list:
    summaries = []
    for run_id in unfinished(store):
        try:
            summaries.append(engine.resume(run_id))
        except Exception:  # noqa: BLE001 - one broken run must not keep the others from resuming
            log.exception("resuming workflow run %s failed", run_id)
    return summaries


def _held_elsewhere(folder: Path) -> bool:
    """``run.lock`` is held, and not through a descriptor of this process: ``engine.resume`` holds it itself
    while it calls ``request``, and an flock taken through another descriptor of one process still conflicts."""
    if not bridge.held(folder):
        return False
    try:
        lock = os.stat(folder / bridge.LOCK_NAME, follow_symlinks=False)
    except OSError:
        return False
    for name in os.listdir("/dev/fd"):
        try:
            st = os.fstat(int(name))
        except (OSError, ValueError):
            continue
        if (st.st_dev, st.st_ino) == (lock.st_dev, lock.st_ino):
            return False
    return True
