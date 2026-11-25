"""Workflow agents as kanban tasks (REQ-024), through the stock ``hermes_cli.kanban_db``.

``Board(board="workflows", kb=None, mask=None)``: ``kb`` defaults to ``hermes_cli.kanban_db``, imported on first
use; when it is not importable every call is a no-op and one warning is logged. ``mask`` defaults to the
engine's stock redactor. WE-WIRE feeds it from the engine's ``on_progress``.
  ``feed(run_id, entries)``: a ``workflow_agent`` start creates one task on the board (created when missing):
  title ``<label> · <run id>``, the masked prompt preview as body, ``idempotency_key="<run id>/<agentId>"``.
  A done agent completes its task with the masked result preview; an error or skip blocks it with the
  masked error (``block_kind`` ``capability``). A continuation (a new index under an earlier call's agent
  id) gets its own task (key ``<run id>/<agentId>#<index>``). A preview or label the runner clipped loses its
  cut last word before it is masked (``progress.uncut``). Kanban errors are logged, never raised: they never fail a run.

Tasks are created ``initial_status="blocked"``: the dispatcher claims only ``ready`` tasks, and the
``blocked`` event create_task writes for it is sticky, so ``recompute_ready`` never promotes the task and no
kanban worker is ever spawned for a workflow agent (the default ``"running"`` lands as ``ready``, and
``triage`` cannot be completed). A failed agent's block is classified in place (kind ``capability``, the
error as its reason); ``needs_input`` is not used, so kanban-heartbeat never turns it into a human question.
"""

from __future__ import annotations

import contextlib
import importlib
import logging
from typing import Any, Callable

from .progress import uncut

logger = logging.getLogger("workflows.board")

TITLE_CHARS, BODY_CHARS = 200, 2000
FAILED_KIND = "capability"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


class Board:
    def __init__(self, board: str = "workflows", kb: Any = None, mask: Callable[[str], str] | None = None):
        self.board, self._kb, self._mask = board, kb, mask
        self._off = False
        self._ready = False

    def feed(self, run_id: str, entries: list) -> None:
        agents = [e for e in entries if isinstance(e, dict) and e.get("type") == "workflow_agent"
                  and e.get("state") in ("start", "done", "error")] if isinstance(entries, list) else []
        if not agents or not self._load():
            return
        try:
            with self._connect() as conn:
                for entry in agents:
                    self._apply(conn, run_id, entry)
        except Exception:  # noqa: BLE001 - the board never fails a run
            logger.warning("workflows: kanban board %s update for %s failed", self.board, run_id, exc_info=True)

    def _apply(self, conn: Any, run_id: str, entry: dict) -> None:
        """One entry; the idempotency key finds the agent's task again (no state kept in this process)."""
        kb, agent, index = self._kb, str(entry.get("agentId") or entry.get("index")), entry.get("index")
        key = "%s/%s" % (run_id, agent)
        if entry.get("agentId") and agent != "wf-agent-%s" % index:
            key += "#%s" % index  # a continuation keeps its agent's id: its own card, never the first call's
        label = uncut(str(entry.get("label") or agent))
        task = kb.create_task(conn, title=_clip(self._masked("%s · %s" % (label, run_id)), TITLE_CHARS),
                              body=_clip(self._masked(uncut(str(entry.get("promptPreview") or ""))), BODY_CHARS),
                              idempotency_key=key, initial_status="blocked",
                              created_by="workflows", board=self.board)
        if entry["state"] == "done":
            text = _clip(self._masked(uncut(str(entry.get("resultPreview") or ""))), BODY_CHARS).strip() or "done"
            moved = kb.complete_task(conn, task, result=text, summary=text, force=True)
        elif entry["state"] == "error":
            reason = _clip(self._masked(str(entry.get("error") or "Agent failed.")), BODY_CHARS)
            moved = kb.block_task(conn, task, reason=reason, kind=FAILED_KIND)
        else:
            return
        if not moved:
            logger.warning("workflows: kanban task %s of %s kept its status on %s", task, key, entry["state"])

    def _masked(self, text: str) -> str:
        if self._mask is None:
            from .engine import _masked
            self._mask = _masked
        return self._mask(text)

    def _load(self) -> bool:
        if self._off:
            return False
        if self._kb is None:
            try:
                self._kb = importlib.import_module("hermes_cli.kanban_db")
            except ImportError:
                self._off = True
                logger.warning("workflows: hermes_cli.kanban_db is not importable; agents are not put on a board")
                return False
        if not self._ready:
            try:
                if not self._kb.board_exists(self.board):
                    self._kb.create_board(self.board)
            except Exception:  # noqa: BLE001 - retried on the next feed
                logger.warning("workflows: kanban board %s could not be created", self.board, exc_info=True)
                return False
            self._ready = True
        return True

    def _connect(self) -> contextlib.AbstractContextManager:
        opener = getattr(self._kb, "connect_closing", None)
        if opener is None:
            opener = importlib.import_module("hermes_cli.kanban_db_connect").connect_closing
        return opener(board=self.board)
