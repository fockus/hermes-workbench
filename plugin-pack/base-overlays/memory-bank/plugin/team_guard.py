"""MB Team orchestrator guard for Hermes: the host adapter over scripts/mb_team_guard_core.py.

``pre_tool_call`` maps Hermes's file and terminal tools to a core event and turns a deny into Hermes's block
directive. ``pre_llm_call`` hands the orchestrator rules to the model once per session (a ``pre_tool_call``
cannot inject context, so the core's rules for a session that just became an orchestrator wait in ``_pending``).
Only the main agent is guarded; a child agent, or a caller that cannot be told apart, passes.
Every failure (no core, crash, timeout, bad arguments) allows: a raising ``pre_tool_call`` would block the call.
Opt-out: MB_TEAM_GUARD=off (the core also reads ``team_guard=off`` in the bank's .mb-config).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

REPO_SKILL_ROOT = Path(__file__).resolve().parents[2]
CORE = Path("scripts") / "mb_team_guard_core.py"
TIMEOUT = 20  # seconds the core may take for one decision
REMINDER = (
    "Orchestrator: update console and bank before you reply."  # Hermes has no blocking Stop hook
)
OFF = ("off", "0", "false", "no")
TOOLS = {
    "write_file": "write",
    "write": "write",
    "edit": "edit",
    "edit_file": "edit",
    "patch": "edit",
    "terminal": "shell",
}
PATCH_FILE = re.compile(
    r"^\*\*\* (?:Update|Add|Delete) File:\s*(.+?)\s*$|^\*\*\* Move to:\s*(.+?)\s*$", re.MULTILINE
)
ALLOW = {"decision": "allow", "message": ""}
_pending: dict[
    str, str
] = {}  # session -> rules the core handed out during a tool call, not yet delivered


def active_parent() -> Any:
    """The agent whose turn is running on this context (stock Hermes), None outside one or when unavailable."""
    try:
        from agent.subagent_lifecycle import get_active_subagent_parent
    except ImportError:
        return None
    return get_active_subagent_parent()


def _is_main(session_id: str) -> bool:
    """True only when stock's active agent is this session's own top-level agent (delegate depth 0)."""
    agent = active_parent()
    if agent is None or str(getattr(agent, "session_id", "") or "") != session_id:
        return False
    try:
        return int(getattr(agent, "_delegate_depth", 0) or 0) <= 0
    except (TypeError, ValueError):
        return False


def _core() -> Path | None:
    for root in (os.environ.get("MB_SKILLS_ROOT"), REPO_SKILL_ROOT):
        if root and (Path(root) / CORE).is_file():
            return Path(root) / CORE
    return None


def _decide(event: dict) -> dict:
    """The core's decision for one event; anything unexpected is allow."""
    core = _core()
    if core is None:
        return ALLOW
    try:
        done = subprocess.run(
            [sys.executable, str(core)],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
        out = json.loads(done.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return ALLOW
    ok = (
        isinstance(out, dict)
        and out.get("decision") in ("deny", "context")
        and isinstance(out.get("message"), str)
    )
    return out if ok else ALLOW


def _paths(tool: str, args: dict) -> list[str]:
    path = args.get("path") or args.get("file_path")
    if isinstance(path, str) and path:
        return [path]
    patch = args.get("patch")
    if tool == "edit" and isinstance(patch, str):
        return [p for found in PATCH_FILE.findall(patch) for p in found if p]
    return []


def _events(tool_name: str, args: Any, session_id: str, cwd: str) -> list[dict]:
    tool = TOOLS.get(tool_name)
    if tool is None or not isinstance(args, dict):
        return []
    base = {"session_id": session_id, "phase": "pre_tool", "is_subagent": False}
    cwd = next(
        (str(args[k]) for k in ("workdir", "cwd") if isinstance(args.get(k), str) and args[k]), cwd
    )
    if tool == "shell":
        command = args.get("command")
        return (
            [{**base, "tool": tool, "command": command, "cwd": cwd}]
            if isinstance(command, str)
            else []
        )
    return [{**base, "tool": tool, "path": path, "cwd": cwd} for path in _paths(tool, args)]


def _enabled(session_id: str) -> bool:
    return (
        bool(session_id)
        and os.environ.get("MB_TEAM_GUARD", "on").lower() not in OFF
        and _is_main(session_id)
    )


def _handlers(session_cwd: Callable[[], str]) -> tuple[Callable[..., Any], Callable[..., Any]]:
    def cwd() -> str:
        return session_cwd() or os.getcwd()

    def pre_tool_call(
        tool_name: str = "", args: Any = None, session_id: str = "", **kw: Any
    ) -> dict | None:
        try:
            if not _enabled(session_id):
                return None
            for event in _events(tool_name, args, session_id, cwd()):
                verdict = _decide(event)
                if verdict["decision"] == "deny":
                    return {"action": "block", "message": verdict["message"]}
                if verdict["decision"] == "context" and verdict["message"]:
                    _pending[session_id] = verdict["message"]
        except Exception as error:
            logger.warning("memory-bank team guard: allowed after %s", type(error).__name__)
        return None

    def pre_llm_call(session_id: str = "", **kw: Any) -> dict | None:
        try:
            if not _enabled(session_id):
                return None
            text = _pending.pop(session_id, "")
            if not text:
                verdict = _decide(
                    {"session_id": session_id, "phase": "prompt", "tool": "none", "cwd": cwd()}
                )
                text = verdict["message"] if verdict["decision"] == "context" else ""
            return {"context": f"{text}\n\n{REMINDER}"} if text else None
        except Exception as error:
            logger.warning("memory-bank team guard: no rules after %s", type(error).__name__)
            return None

    return pre_tool_call, pre_llm_call


def register(ctx: Any) -> None:
    """Register the guard on a Hermes plugin context; the session's own workspace is its default cwd."""
    session_cwd = getattr(ctx, "session_cwd", None)
    pre_tool_call, pre_llm_call = _handlers(
        lambda: (session_cwd() if callable(session_cwd) else "") or ""
    )
    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("pre_llm_call", pre_llm_call)
