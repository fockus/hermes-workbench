"""Who may start a workflow run, and when a large run needs the user's yes (REQ-004, 005, 018).

Public names for WE-WIRE: ``TOOL_DESCRIPTION`` (the ``workflow`` tool description), ``OptIn`` (its
``pre_llm_call`` hook records per session whether the latest user turn opts in; ``allowed(session_id, entry)``),
``refusal(agents, tokens)`` (REQ-005's one-line proposal), ``size_check(...)`` and ``approval_text(...)``
(REQ-018, ``GUIDELINE`` = 10 agents) and ``estimate(agents)``.

``estimate`` uses ``TOKENS_PER_AGENT`` = 100,000 tokens per agent call (AGR-074; design.md names no figure:
the user's average for a delegated child's prompt plus answer). A cron run is detected by the caller through
``ctx.current_cron_execution()``; this module only takes ``entry``.
"""

from __future__ import annotations

import re
import threading
from typing import Callable, Literal

GUIDELINE = 10
TOKENS_PER_AGENT = 100_000
TRUSTED_ENTRIES = frozenset({"command", "cli", "cron", "socket", "ultracode"})
WORKFLOW_WORDS = ("workflow", "work-flow", "воркфлоу", "ворк-флоу", "воркфлов", "flujo de trabajo",
                  "fluxo de trabalho", "工作流", "ワークフロー", "워크플로")

TOOL_DESCRIPTION = (
    "Run a JavaScript workflow script (or a saved workflow by name) that orchestrates many sub-agents. "
    "Call it only on an explicit user request to run a workflow or when the user's latest message says the word "
    "workflow or asks to run mb-team-track; runs started by a saved workflow /command, the CLI, a cron job or "
    "ultracode are always allowed. "
    "Otherwise the call is refused with a cost estimate: propose the run and let the user opt in. A run over "
    "%d agents returns approval_needed; ask the user and call again with approved: true." % GUIDELINE
)



def _pattern(word: str) -> str:
    # CJK scripts do not separate words with spaces, so their forms match as substrings; the others need
    # word boundaries (a short inflection such as "workflows" is fine, "MyWorkflowFactory" is not).
    if any(ord(c) >= 0x3000 for c in word):
        return re.escape(word)
    return r"(?<!\w)%s\w{0,3}(?!\w)" % re.escape(word)


_WORDS = re.compile("|".join(_pattern(w) for w in WORKFLOW_WORDS), re.I)
# The skill's mb-team-track.js is asked for by name: only an explicit run command counts, not a mention, a
# question about it, a negation or a longer name (mb-team-tracker).
_TEAM_RUN = re.compile(r"(?<!\w)(?<!not )(?<!n't )(?<!never )(?:run|start|launch|execute)\s+mb-team-track(?:\.js)?"
                       r"(?![\w-]|\.\w)", re.I)
_COMMAND = re.compile(r"(?:^|\s)/[A-Za-z0-9][\w.-]*(?=\s|$)")
_NON_TEXT = {"image", "image_url", "input_image", "audio", "input_audio"}


def _text(message) -> str:
    """The text of a user turn: a string, or the text parts of a multimodal list (an image-only turn is "")."""
    if isinstance(message, str):
        return message
    if not isinstance(message, list):
        return ""
    parts = []
    for part in message:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and str(part.get("type") or "").lower() not in _NON_TEXT:
            text = part.get("text")
            parts.append(text if isinstance(text, str) else "")
    return "\n".join(parts)


def opts_in(message) -> bool:
    """True when a user turn names a workflow in any listed language or invokes a saved one as /name."""
    text = _text(message)
    return bool(_WORDS.search(text) or _COMMAND.search(text) or _TEAM_RUN.search(text))


class OptIn:
    """Per-session opt-in of the latest user turn; an earlier turn's opt-in does not carry over."""

    def __init__(self) -> None:
        self._latest: dict[str, bool] = {}
        self._lock = threading.Lock()

    def pre_llm_call(self, session_id: str = "", user_message: object = "", **_) -> None:
        with self._lock:
            self._latest[session_id] = opts_in(user_message)

    def allowed(self, session_id: str, entry: str) -> bool:
        if entry in TRUSTED_ENTRIES:
            return True
        with self._lock:
            return self._latest.get(session_id, False)


def estimate(agents: int) -> int:
    return max(0, int(agents)) * TOKENS_PER_AGENT


def _tokens(tokens: int) -> str:
    return "{:,}".format(int(tokens))


def refusal(agents: int, tokens: int) -> str:
    return ("This needs a workflow run (~%d agents, ~%s tokens). Ask the user to say 'workflow' to start it."
            % (agents, _tokens(tokens)))


def approval_text(agents: int, tokens: int) -> str:
    return "approval needed: %d agents, ~%s tokens" % (agents, _tokens(tokens))


def size_check(agents: int, tokens: int, *, entry: str, approved: bool,
               ask_choice: Callable[[str, list], object] | None = None) -> Literal["run", "approval_needed", "cut"]:
    """``run`` within the guideline or once approved; a tool run over it needs approval; others ask or are cut."""
    if agents <= GUIDELINE or approved:
        return "run"
    if entry == "user":
        return "approval_needed"
    answer = ask_choice(approval_text(agents, tokens) + ". Run all of them?", ["yes", "no"]) if ask_choice else None
    return "run" if str(answer or "").strip().lower() == "yes" else "cut"
