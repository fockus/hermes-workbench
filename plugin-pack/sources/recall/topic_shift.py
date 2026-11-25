"""When the plugin searches on its own after the first message (spec Task 8; REQ-007, REQ-008).

``TopicShift.decide`` answers per user message, in this order: under 8 words -> no; the last search of the
session fewer than 3 turns ago -> no; a memory marker ("помнишь", "last time", ...) -> yes; else embedding
drift: the message against the mean of the last 5 user messages, cosine below the threshold -> yes. The
embeddings come from the service (``Client.embed``); an unavailable service, a missing embedding or the 1 s
budget of the whole decision give no search, never an error.

The threshold is ``$HERMES_HOME/recall/drift.json`` (written by ``hermes_recall.eval_calibrate``); without a
valid file ``DEFAULT_DRIFT`` applies."""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any

from .client import Unavailable

MARKERS = [re.compile(p, re.I) for p in (
    r"помнишь", r"помните", r"вспомни", r"напомни", r"мы (?:уже )?(?:обсуждали|решили|договорились|говорили)",
    r"как мы (?:решили|делали|договорились)", r"в прошл\w+ раз", r"на прошл\w+ (?:неделе|встрече|сессии)",
    r"remember when", r"do you remember", r"last time", r"we (?:already )?(?:decided|discussed|agreed)",
    r"as we (?:decided|discussed|agreed)", r"earlier we")]
MIN_WORDS, COOLDOWN, HISTORY, BUDGET, MAX_CHARS, CACHE = 8, 3, 5, 1.0, 2000, 256
# ponytail: an uncalibrated guess (cosine of an unrelated message to the recent topic is usually below it);
# the pairs of RC-BAKEQ calibrate the real value into drift.json (`python -m hermes_recall.eval --calibrate`).
DEFAULT_DRIFT = 0.5
_SECTIONS = re.compile(r"^## (?:Historical Task Snapshot|Goal|Next Steps?)\s*\n(.*?)(?=^## |\Z)", re.M | re.S | re.I)


class TopicShift:
    def __init__(self, client: Any, profile: str, drift_path: Path):
        self.client, self.profile, self.drift_path = client, profile, Path(drift_path)
        self._last: dict[str, int] = {}  # session id -> turn index of its last search
        self._vectors: dict[str, list[float]] = {}

    def mark_searched(self, session_id: str, turn_index: int) -> None:
        self._last[session_id] = turn_index

    def threshold(self) -> float:
        try:
            value = json.loads(self.drift_path.read_text(encoding="utf-8"))["threshold"]
        except (OSError, ValueError, KeyError, TypeError):
            return DEFAULT_DRIFT
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value < 1
        return float(value) if ok else DEFAULT_DRIFT

    def decide(self, session_id: str, message: str, history_user_texts: list[str], turn_index: int) -> bool:
        text = message.strip()
        if len(text.split()) < MIN_WORDS:
            return False
        if session_id in self._last and turn_index - self._last[session_id] < COOLDOWN:
            return False
        if any(marker.search(text) for marker in MARKERS):
            return True
        past = [t.strip() for t in history_user_texts if t and t.strip()][-HISTORY:]
        return bool(past) and self._drifted(text, past)

    def _drifted(self, text: str, past: list[str]) -> bool:
        deadline = time.monotonic() + BUDGET
        try:
            vectors = [self._embed(t, deadline) for t in past]
            current = self._embed(text, deadline)
        except Unavailable:
            return False
        if current is None or any(v is None or len(v) != len(current) for v in vectors):
            return False
        mean = [sum(column) / len(vectors) for column in zip(*vectors)]
        norms = math.sqrt(sum(x * x for x in current)) * math.sqrt(sum(x * x for x in mean))
        return norms > 0 and sum(a * b for a, b in zip(current, mean)) / norms < self.threshold()

    def _embed(self, text: str, deadline: float) -> list[float] | None:
        text = text[:MAX_CHARS]
        if text not in self._vectors:
            left = deadline - time.monotonic()
            if left <= 0:
                raise Unavailable("the topic-shift budget is spent")
            vector = self.client.embed(self.profile, text, timeout=left)
            if vector is None:
                return None
            if len(self._vectors) >= CACHE:
                self._vectors.clear()
            self._vectors[text] = vector
        return self._vectors[text]


def compaction_query(summary: str) -> str:
    """The pending-task snapshot, goal and next-step sections of a compaction summary as one query; the summary's
    start without them. Hermes keeps the unfinished request in the snapshot, and not every summary has a next step."""
    found = " ".join(" ".join(part.split()) for part in _SECTIONS.findall(summary))
    return found[:MAX_CHARS] if found else " ".join(summary.split())[:500]
