"""The keep-going watchdog's side effects for one Hermes session (loaded by path from ``wiring``).

``HermesEffects`` stops the task (the report is stored, posted to the person, and pending in the task
record until a post or a veto shows it), blocks a kanban worker's task, writes the Memory Bank handoff and
marks the run-budget checkpoint. ``persist(session_id)`` stores the session's task record.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("keep-going.wiring")

HANDOFF_SCRIPT = "scripts/mb-handoff.sh"  # under $MB_SKILLS_ROOT, which the memory-bank plugin exports
FALLBACK_SKILLS_ROOT = "~/.claude/skills/memory-bank"
HANDOFF_TIMEOUT = 60
REPORT = "keep-going watchdog stopped this task. {report} Send a message to start again."


class HermesEffects:
    """The watchdog's side effects for one Hermes session. ``session_id`` follows a compaction's rotation."""

    def __init__(self, ctx, session_id: str, handoff_script: str | None, core: Any, persist: Callable[[str], None]):
        self._ctx, self.session_id, self._script, self._core = ctx, session_id, handoff_script, core
        self._persist = persist  # stores the session's task record

    def stop(self, report: str) -> None:
        report = self._core._redacted(report)  # one final pass over what is logged, stored, posted and shown
        logger.warning("keep-going watchdog: session %s: %s", self.session_id, report)
        with self._core._lock:  # pending until a post or a veto shows it to the person
            st = self._core._state(self.session_id)
            st["halted"] = st["undelivered"] = report
        self._persist(self.session_id)
        post, handle = getattr(self._ctx, "post_message", None), None  # to the person, not only the model
        try:
            handle = post(REPORT.format(report=report)) if callable(post) else None
        except Exception as exc:  # noqa: BLE001 -- the report then goes out as the next turn's veto
            logger.info("keep-going watchdog: posting the stop report failed: %s", exc)
        if not getattr(handle, "ok", False):
            logger.info("keep-going watchdog: stop report not posted to session %s (%s); the next turn shows it",
                        self.session_id, getattr(handle, "error", None))
            return
        with self._core._lock:
            st = self._core._state(self.session_id)
            if st.get("undelivered") == report:  # a newer stop's report stays pending
                st.pop("undelivered", None)
        self._persist(self.session_id)

    def block_task(self, task_id: str, report: str) -> None:
        """Block this worker's kanban task as needs_input; kanban-heartbeat delivers the report (the same
        sanitized report ``stop`` stores, posts and shows)."""
        report = self._core._redacted(report)
        try:
            outcome = self._core._dispatch_tool("kanban_block", {"reason": report, "kind": "needs_input"})
        except Exception as exc:  # noqa: BLE001 -- the stop already happened; only the block failed
            outcome = '"error": %r' % exc
        if '"error"' in outcome:
            logger.warning("keep-going watchdog: could not block kanban task %s: %s", task_id, outcome[:300])

    def _handoff_script(self) -> Path:
        """The configured script, else ``$MB_SKILLS_ROOT``'s, read now: the variable may be exported after load."""
        if self._script:
            return Path(os.path.expanduser(self._script))
        root = os.environ.get("MB_SKILLS_ROOT") or FALLBACK_SKILLS_ROOT
        return Path(os.path.expanduser(root)) / HANDOFF_SCRIPT

    def write_handoff(self) -> None:
        failure = self._run_handoff()
        if failure:
            logger.warning("keep-going watchdog: no handoff written: %s", failure)
        with self._core._lock:
            self._core._state(self.session_id)["handoff_failure"] = failure  # the continuation names it

    def _run_handoff(self) -> str | None:
        """None when the handoff script ran cleanly, else why it did not."""
        script = self._handoff_script()
        if not script.is_file():
            return "handoff script %s not found" % script
        try:
            cwd = self._ctx.session_cwd() or None
            proc = subprocess.run(["bash", str(script), "--actualize"], cwd=cwd, capture_output=True, text=True,
                                  timeout=HANDOFF_TIMEOUT, env=dict(os.environ, MB_SESSION_ID=self.session_id))
        except (OSError, subprocess.SubprocessError) as exc:
            return "handoff failed: %s" % exc
        return "handoff exited %d: %s" % (proc.returncode, proc.stderr[-300:].strip()) if proc.returncode else None

    def continue_in_new_run(self) -> None:
        with self._core._lock:
            self._core._state(self.session_id)["checkpoint"] = True
        self._persist(self.session_id)
