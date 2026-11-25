"""The live progress card of a workflow run (REQ-023): a pure consumer of the runner's progress entries.

``Card(post, run_id="", clock=time.monotonic, interval=3.0, mask=None, timer=threading.Timer)``: ``post(text)``
sends a new message and returns its handle; ``handle.edit(text)`` updates it in place (Hermes
``ctx.post_message``, wired by WE-WIRE). ``mask`` defaults to the engine's stock redactor (``engine._masked``).
  ``feed(entries)`` folds one vendored WorkflowEntry batch (``workflow_agent`` / ``workflow_phase`` /
  ``workflow_log``) into the card. The first feed posts at once; later changes are written no closer than
  ``interval`` after the previous update, through one trailing timer. A ``post`` that returns no handle with
  ``edit`` gets those updates as new posts, still throttled.
  ``close(status)`` cancels the timer and writes the final card at once (the only update that may come
  sooner than ``interval``). ``render() -> str`` is the card text; ``entries`` keeps every entry fed (the
  diagram, workflows.diagram, is built from it).
``uncut(text)`` drops the cut last word of a runner-clipped text before it is masked (card, diagram, board).
``fold(entries) -> (phases, agents, logs)`` is the shared fold: phase titles in order, the last entry per
agent index (with its resolved ``phase``), every log message.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger("workflows.progress")

CARD_CHARS, LABEL_CHARS, LOG_CHARS, LOG_LINES = 3500, 120, 300, 5
NULL_STATES = {"failed", "skipped"}


def fold(entries: list) -> tuple[list[str], dict[int, dict], list[str]]:
    titles: dict[Any, str] = {}
    phases: list[str] = []
    agents: dict[int, dict] = {}
    logs: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("type")
        if kind == "workflow_phase":
            title = str(entry.get("title") or "")
            titles[entry.get("index")] = title
            if title and title not in phases:
                phases.append(title)
        elif kind == "workflow_log":
            logs.append(str(entry.get("message") or ""))
        elif kind == "workflow_agent" and isinstance(entry.get("index"), int):
            phase = entry.get("phaseTitle") or titles.get(entry.get("phaseIndex"))
            if phase and phase not in phases:
                phases.append(str(phase))
            agents[entry["index"]] = {**entry, "phase": str(phase) if phase else None}
    return phases, dict(sorted(agents.items())), logs


def state(agent: dict) -> str:
    if agent.get("state") == "error":
        return "skipped" if agent.get("skipped") else "failed"
    return "done" if agent.get("state") == "done" else "running"


def tokens(agent: dict) -> int:
    value = agent.get("tokens")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def uncut(text: str) -> str:
    """A text the runner clipped (it ends with "…") without its last, cut word: a credential cut before its
    delimiter (a URL before its '@') is no longer recognisable to the redactor, so it never reaches it."""
    if not text.endswith("…"):
        return text
    head = text[:-1]
    return head[:max(head.rfind(c) for c in " \t\r\n") + 1] + "…"


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


class Card:
    def __init__(self, post: Callable[[str], Any], run_id: str = "", clock: Callable[[], float] = time.monotonic,
                 interval: float = 3.0, mask: Callable[[str], str] | None = None, timer: Any = threading.Timer):
        if mask is None:
            from .engine import _masked as mask
        self._post, self.run_id, self._clock, self._interval = post, run_id, clock, interval
        self._mask, self._timer_cls = mask, timer
        self.entries: list = []
        self.status = "running"
        self._handle: Any = None
        self._posted = self._dirty = self._closed = False
        self._last = 0.0
        self._timer: Any = None
        self._lock = threading.RLock()

    def feed(self, entries: list) -> None:
        with self._lock:
            if self._closed or not isinstance(entries, list):
                return
            self.entries.extend(entries)
            if not self._posted:
                self._update()
                return
            self._dirty = True
            if self._timer is not None:
                return
            wait = self._last + self._interval - self._clock()
            if wait <= 0:
                self._update()
                return
            self._timer = self._timer_cls(wait, self._flush)
            if hasattr(self._timer, "daemon"):
                self._timer.daemon = True
            self._timer.start()

    def close(self, status: str) -> None:
        """Write the final card now; it is the one update that may come sooner than ``interval``."""
        with self._lock:
            if self._closed:
                return
            self._closed, self.status = True, status
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._update()

    def render(self) -> str:
        phases, agents, logs = fold(self.entries)
        head = ["workflow %s %s" % (self.run_id, self.status)]
        rows: list[str] = []
        for phase in [None, *phases]:
            members = [a for a in agents.values() if a["phase"] == phase]
            if phase is not None:
                rows.append("[%s]" % phase)
            rows += [self._row(a) for a in members]
        tail = ["tokens: %d" % sum(tokens(a) for a in agents.values())]
        tail += logs[-LOG_LINES:]
        # masked before clipping: a cut never leaves the head of a secret the redactor no longer recognises
        head, rows, tail = ([_clip(self._mask(line), LOG_CHARS) for line in part] for part in (head, rows, tail))
        room = CARD_CHARS - len("\n".join(head + tail)) - 1
        shown: list[str] = []
        for n, row in enumerate(rows):
            more = "… %d more" % (len(rows) - n)
            if len("\n".join(shown + [row, more])) > room:
                shown.append(more)
                break
            shown.append(row)
        return "\n".join(head + shown + tail)[:CARD_CHARS]

    @staticmethod
    def _row(agent: dict) -> str:
        shown = state(agent)
        parts = [uncut(str(agent.get("label") or agent.get("agentId") or "agent")),
                 shown + (" (null)" if shown in NULL_STATES else ""), "%d tokens" % tokens(agent)]
        if agent.get("model"):
            parts.append(str(agent["model"]))
        return " · ".join(parts)

    def _flush(self) -> None:
        with self._lock:
            self._timer = None
            if self._dirty and not self._closed:
                self._update()

    def _update(self) -> None:
        self._posted, self._dirty, self._last = True, False, self._clock()
        try:
            text = self.render()
            edit = getattr(self._handle, "edit", None)
            if callable(edit):
                edit(text)
            else:
                self._handle = self._post(text)
        except Exception:  # noqa: BLE001 - a card never fails a run
            logger.warning("workflows: the progress card of %s could not be updated", self.run_id, exc_info=True)
