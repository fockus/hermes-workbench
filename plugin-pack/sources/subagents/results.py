"""Short results (REQ-019, SPIKE.md § 5): a finished child's answer reaches its parent as one line —
who, the status, the answer's lead and the path of a step file that starts with ``## Summary`` and
keeps the full answer.

Stock renders background completions with no plugin hook, so the child's own final answer is
shortened in ``transform_llm_output`` (it becomes the stock summary of both foreground and background
runs); ``transform_tool_result`` shortens any long entry left in a foreground delegate_task result.
A task with an ``output_schema`` is left intact: its JSON contract must reach the parent as is. Whether
a child has one is known from its parent's spawn (callers.py); a child whose spawn was never seen is
left intact too. Step files keep the shared spill folder's policy: 14 days, then the oldest go first
above the size cap.

REQ-018 caveat (accepted): the shortened line replaces the child's own persisted final answer; the full
answer stays in the 0600 step file it names.
"""

from __future__ import annotations

import collections
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping

from .callers import CallerMap, task_has_schema

LEAD_LIMIT = 200      # characters of the answer's lead in the one line
SUMMARY_LIMIT = 1200  # characters of the step file's Summary section (~300 tokens)
MARK = "full answer: "
GENERATED_KEPT = 1024  # lines this plugin generated, remembered so a shortened line is never shortened again
STEPS_RETENTION_S = 14 * 86400  # the shared spill folder's policy (shared-mechanisms.md)
STEPS_CAP_BYTES = 2 * 1024 ** 3


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _paragraph(answer: str) -> str:
    return next((p.strip() for p in re.split(r"\n\s*\n", answer) if p.strip()), "")


def _is_short(text: str, generated: Any = ()) -> bool:
    """Already one line: short by itself, or a line this plugin generated (a pointer an answer merely ends
    with proves nothing: the model may write it). Measured on the exact text the caller forwards."""
    return text in generated or (len(text) <= LEAD_LIMIT and "\n" not in text.strip())


def _step_file(label: str, status: str, goal: Any, answer: str) -> Path:
    from hermes_constants import get_hermes_home

    folder = Path(get_hermes_home()) / "subagents" / "steps"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / ("%s-%d.md" % (re.sub(r"[^A-Za-z0-9_.-]", "_", label)[:64], time.time_ns()))
    body = "## Summary\n%s\n\n- agent: %s\n- status: %s\n- goal: %s\n\n## Full answer\n%s\n" % (
        _clip(_paragraph(answer), SUMMARY_LIMIT), label, status, _clip(" ".join(str(goal or "").split()), LEAD_LIMIT),
        answer)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
    # ponytail: every new step file scans the folder; switch to an index past ~100k files.
    prune_steps(folder, keep=path)
    return path


def prune_steps(folder: Path, keep: Path) -> None:
    """Step files older than 14 days go; above the cap the oldest go first. ``keep`` (the new one) stays."""
    now = time.time()
    files = []
    for path in folder.glob("*.md"):
        try:
            info = path.lstat()
        except OSError:
            continue
        files.append((info.st_mtime, info.st_size, path))
    files.sort()
    total = sum(size for _, size, _ in files)
    for mtime, size, path in files:
        if now - mtime <= STEPS_RETENTION_S and total <= STEPS_CAP_BYTES:
            break
        if path == keep:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:  # one stuck file must not cost the result; the next write retries it
            continue
        total -= size


def shorten_result(entry: Mapping[str, Any], label: str | None = None) -> str:
    """One line for a finished task entry (``subagent_id``, ``status``, ``summary`` or ``error``, ``goal``);
    the full answer goes to a new step file."""
    label = label or str(entry.get("subagent_id") or "task %s" % entry.get("task_index", 0))
    status = str(entry.get("status") or "completed")
    answer = str(entry.get("summary") or entry.get("error") or "")
    lead = _clip(" ".join(_paragraph(answer).split()), LEAD_LIMIT) or "(no answer)"
    path = _step_file(label, status, entry.get("goal"), answer)
    return "%s %s: %s (%s%s)" % (label, status, lead, MARK, path)


class Shortener:
    """Hook handlers: transform_llm_output, transform_tool_result."""

    def __init__(self, callers: CallerMap):
        self.callers = callers
        self._generated: collections.deque[str] = collections.deque(maxlen=GENERATED_KEPT)

    def _shortened(self, entry: Mapping[str, Any], label: str | None = None) -> str:
        line = shorten_result(entry, label)
        self._generated.append(line)
        return line

    def transform_llm_output(self, response_text: Any = None, session_id: str = "", **_: Any) -> str | None:
        child = self.callers.get(session_id)
        if child is None or child.schema is not False or not isinstance(response_text, str) \
                or _is_short(response_text, self._generated):
            return None  # an output contract, or an unknown one, is never cut
        entry = {"subagent_id": child.subagent_id, "status": "completed", "summary": response_text}
        return self._shortened(entry, label=child.name)

    def transform_tool_result(self, tool_name: str = "", args: Any = None, result: Any = None, **_: Any) -> str | None:
        if tool_name != "delegate_task" or not isinstance(result, str):
            return None
        try:
            data = json.loads(result)
        except ValueError:
            return None
        entries = data.get("results") if isinstance(data, dict) else None
        changed = False
        for entry in entries if isinstance(entries, list) else ():
            if not isinstance(entry, dict) or not isinstance(entry.get("summary"), str):
                continue
            index = entry["task_index"] if isinstance(entry.get("task_index"), int) else -1
            if not _is_short(entry["summary"], self._generated) and not task_has_schema(args, index):
                entry["summary"] = self._shortened(entry)
                changed = True
        return json.dumps(data, ensure_ascii=False) if changed else None
