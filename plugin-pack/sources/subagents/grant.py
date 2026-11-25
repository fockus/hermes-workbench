"""Whether a main-agent turn's text is the user's own "do it yourself" (REQ-003, REQ-021; Astra review round 1).

Stock gives no positive mark of typed text: the CLI input queue also carries process notifications, goal-loop
continuations (hermes_cli/goals.py, with command output and goal text verbatim) and plugin injections. So a text
never lifts when it carries a header stock or this plugin puts on non-user text (``NOT_USER_MARKS``), and only an
affirmative directive counts: one not negated in its clause and not quoted (``"…"``, ``«…»``, backticks, a
``>`` quote line).
"""

from __future__ import annotations

import re

LIFT_RE = re.compile(r"\bdo (?:it|this|that) (?:yourself|directly)\b|\bсделай(?:те)?(?: это)? сам|\bсам сделай",
                     re.IGNORECASE)
AGENT_MARK = " - an agent, not the user."  # messages.py marks every agent message with it
# Headers of text that is not the user's: process notifications (tools/process_registry_notifications.py:375,
# 524-588), async delegation results, the agent marker, and every stock goal-loop continuation (hermes_cli/goals.py:
# "[Continuing toward your standing goal…", "[Continuing toward this kanban task…", the finalize nudge, the kick).
NOT_USER_MARKS = ("[IMPORTANT: ", "[Background process ", "Handed off to you by a subagent", "Started by subagent ",
                  "[ASYNC DELEGATION ", AGENT_MARK, "[Continuing toward ",
                  "[The work looks complete, but the task is still open]", "[Goal set] ")
NEGATION_RE = re.compile(r"\b(?:don'?t|do not|never|not|no need to|не|нельзя|не надо|не нужно)\b", re.IGNORECASE)
CLAUSE_END_RE = re.compile(r"[.!?;,\n]")


def _quoted(line: str, start: int) -> bool:
    before = line[:start]
    return line.lstrip().startswith(">") or any(before.count(mark) % 2 for mark in ('"', "`")) or (
        before.count("“") > before.count("”")) or (before.count("«") > before.count("»"))


def affirmative(text: str) -> bool:
    """True when ``text`` holds an unquoted, unnegated "do it yourself" directive."""
    for match in LIFT_RE.finditer(text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        line = text[line_start:line_end if line_end != -1 else len(text)]
        clause = CLAUSE_END_RE.split(text[line_start:match.start()])[-1]
        if not _quoted(line, match.start() - line_start) and not NEGATION_RE.search(clause):
            return True
    return False


def not_user_text(text: str) -> bool:
    return any(mark in text for mark in NOT_USER_MARKS)
