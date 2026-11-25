"""keep-going: don't let a turn end while the agent's own todo list has open items and keeps moving.

Mid-turn, after file edits, the stock ``pre_verify`` stop gate continues the turn. When the turn ends
anyway (no edits, or the gate's own nudge budget is spent), ``post_llm_call`` queues one continuation
message. Both stop when the answer asks the user something or reports a blocker, after MAX_IDLE
continuations without a todo revision change, and after MAX_PER_MESSAGE per real user message.

With ``settings.watchdog.enabled: true`` a progress watchdog (``watchdog.py``, wired by ``wiring.py``) also runs. It stops
a stuck run by blocking its tool calls with a report, vetoes every later turn of the task until the person
writes, and blocks a kanban worker's task as needs_input; the stop report is posted to the session (or
shown as the next turn's veto). At 80 % of ``agent.run_budget_seconds`` it
writes a Memory Bank handoff (``handoff_script``, default ``$MB_SKILLS_ROOT/scripts/mb-handoff.sh``),
refuses the run's further tool calls and continues in a new run. It stops a task after 24 h, counted
across restarts and session-id rotations, with the stop stored beside the start (``runs.py``). Only a
genuine user message lifts a stop or starts a new task. Without the setting, nothing of it is registered.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import re
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

PREFIX = "[keep-going]"
MAX_IDLE = 3
MAX_PER_MESSAGE = 10
TODO_TOOLS = frozenset({"todo_list", "todo"})
OPEN = ("pending", "in_progress")
_STOP = re.compile(
    r"\?\s*$|\bblocked\b|\bneed (your|a) decision\b|заблокирован|нужн[оа] (тво[её]|ваше) решение|жду (ответа|решения|подтверждения)",
    re.IGNORECASE,
)

_lock = threading.Lock()
_sessions: dict[str, dict] = {}  # ponytail: one small entry per session for the gateway's lifetime
_children: set[str] = set()


def _state(session_id: str) -> dict:
    return _sessions.setdefault(session_id, {"open": [], "revision": None, "nudged": object(), "idle": 0, "count": 0})


def _on_user_turn(*, session_id="", user_message="", parent_session_id="", **kwargs):
    with _lock:
        if parent_session_id:
            _children.add(session_id)
            return
        if not str(user_message or "").startswith(PREFIX):
            st = _state(session_id)
            st["idle"] = st["count"] = 0


def _on_tool(*, tool_name="", result="", session_id="", **kwargs):
    if tool_name not in TODO_TOOLS:
        return
    try:
        data = json.loads(result)
        todos = data["todos"]
    except (TypeError, ValueError, KeyError):
        return
    with _lock:
        st = _state(session_id)
        st["open"] = [t.get("content", "") for t in todos if isinstance(t, dict) and t.get("status") in OPEN]
        st["revision"] = data.get("revision")


def _continuation(session_id: str, answer, *, new_run: bool = False) -> str | None:
    """The continuation message, or None when the turn should end. Counts the continuation it grants.
    ``new_run``: the turn has ended, so a pending watchdog checkpoint continues the task in a new run."""
    with _lock:
        if session_id in _children or session_id not in _sessions:
            return None
        st = _sessions[session_id]
        if st.get("halted") or (not new_run and st.get("checkpoint")) or _STOP.search(str(answer or "")[-300:]):
            return None  # a pending checkpoint ends this run's turn; its continuation opens the next run
        if new_run and st.get("checkpoint"):  # held until its continuation opens the new run (wiring)
            failure = st.pop("handoff_failure", None)
            if failure:
                return (f"{PREFIX} The run budget checkpoint was reached, but the Memory Bank handoff was NOT "
                        f"written ({failure}). Continue the task in this new run from your todo list.")
            return (f"{PREFIX} The run budget checkpoint was reached and a Memory Bank handoff was written. "
                    "Continue the task in this new run from the handoff and your todo list.")
        if not st["open"]:
            return None
        st["idle"] = 0 if st["revision"] != st["nudged"] else st["idle"] + 1
        if st["idle"] > MAX_IDLE or st["count"] >= MAX_PER_MESSAGE:
            return None
        st["nudged"], st["count"] = st["revision"], st["count"] + 1
        items = "\n".join(f"- [ ] {item}" for item in st["open"])
    return (f"{PREFIX} Your todo list still has open items:\n{items}\n"
            "Continue working on them now. If you are blocked or need a decision from the user, say so and stop; "
            "mark an item cancelled if it is no longer needed.")


def _on_verify(*, session_id="", final_response="", **kwargs):
    message = _continuation(session_id, final_response)
    return {"action": "continue", "message": message} if message else None


# --- Progress watchdog (opt-in: settings.watchdog.enabled) -----------------------------------------

_STOPPED = (PREFIX + " watchdog: {report} End your turn now: report this to the user and call no more tools.")
_watch: dict[str, dict] = {}  # ponytail: like _sessions, one small entry per session for the process lifetime
_now = time.time


def _watchdog_settings(ctx) -> dict:
    """``plugins.entries.keep-going.settings.watchdog``; {} (watchdog off) when absent or unreadable."""
    try:
        value = ctx.get_config("watchdog", None)
    except (AttributeError, OSError, ValueError) as exc:  # a host without plugin settings: watchdog stays off
        logger.debug("keep-going: no watchdog settings (%s)", exc)
        return {}
    return value if isinstance(value, dict) else {}


def _positive(value, default):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else default


def _run_budget_seconds():
    """``agent.run_budget_seconds`` from the Hermes config, or None (no checkpoint)."""
    try:
        from hermes_cli.config import load_config_readonly
        agent = (load_config_readonly() or {}).get("agent") or {}
    except Exception as exc:  # noqa: BLE001 -- a config read failure only disables the checkpoint
        logger.warning("keep-going watchdog: cannot read agent.run_budget_seconds (%s)", exc)
        return None
    return _positive(agent.get("run_budget_seconds"), None)


def _dispatch_tool(name: str, args: dict) -> str:
    from tools.registry import registry
    return str(registry.dispatch(name, args))


def _redacted(text: str) -> str:
    """Hermes's strict redactor on the whole result, before the watchdog quotes any of it. Fails closed:
    without the redactor only the result's digest is passed, which still tells new results from repeats."""
    try:
        from agent.redact import redact_derived_text
        return redact_derived_text(text)
    except Exception:  # noqa: BLE001 -- any redactor failure must withhold the text
        return "[tool result withheld: %s]" % hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def _succeeded(result) -> bool:
    try:
        data = json.loads(result)
    except (TypeError, ValueError):
        return isinstance(result, str)
    return not (isinstance(data, dict) and (data.get("error") or data.get("success") is False))


def _load(name: str):
    """``<name>.py`` beside this file, by path: the plugin also loads as a bare file (no package)."""
    spec = importlib.util.spec_from_file_location(__name__ + "_" + name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Core:
    """This module's names, looked up at use: what a test or a later rebinding sets is what the wiring sees."""

    def __getattr__(self, name):
        return globals()[name]


def _register_watchdog(ctx, settings: dict):
    """The watchdog's hooks; returns how a continuation is bound to the run that queues it."""
    return _load("wiring").register(ctx, settings, _Core())


def register(ctx):
    bind = []  # the watchdog's continuation binder, when the watchdog is on

    def on_turn_end(*, session_id="", assistant_response="", **kwargs):
        message = _continuation(session_id, assistant_response, new_run=True)
        if message and bind:
            message = bind[0](session_id, message)
        if message and not ctx.inject_message(message):
            logger.warning("keep-going: continuation not delivered; set "
                           "plugins.entries.keep-going.settings.allow_gateway_injection: true")

    ctx.register_hook("pre_llm_call", _on_user_turn)
    ctx.register_hook("post_tool_call", _on_tool)
    ctx.register_hook("pre_verify", _on_verify)
    ctx.register_hook("post_llm_call", on_turn_end)
    settings = _watchdog_settings(ctx)
    if settings.get("enabled") is True:
        bind.append(_register_watchdog(ctx, settings))
