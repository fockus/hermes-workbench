"""The ``workflow`` tool (workflow-engine Task 16: REQ-004, REQ-005, REQ-012).

``make_handler(ctx, wiring)`` returns the handler Hermes calls with ``(args, task_id=, session_id=, ...)``. It
answers one JSON line: ``{status: refused | error | failed | done | stopped, ...}`` and never raises. A model
call needs the user's opt-in for the latest turn (``OptIn``); a cron run and the trusted entries need none.
A request with only a ``task`` runs the ``route`` workflow with its classifier on the router model.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from . import optin
from .router import RouterHost, route_request

# ponytail: no pre-run agent count exists (runner/prescan.mjs only refuses banned constructs), so every run is
# priced as one agent and the over-10 approval (optin.size_check, REQ-018) is not asked for a tool run. Ceiling: a
# large script is not asked about; upgrade: a prescan op that counts the planned agents, then size_check here.
PLANNED_AGENTS = 1
KEYS = ("script", "name", "task")
SCHEMA = {
    "name": "workflow",
    "description": optin.TOOL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "script": {"type": "string", "description": "Workflow script source (JavaScript)."},
            "name": {"type": "string", "description": "A saved workflow's name."},
            "task": {"type": "string", "description": "A request without a named pattern: the route workflow "
                                                       "picks the pattern."},
            "args": {"type": "object", "description": "Arguments the script reads as `args`."},
            "approved": {"type": "boolean", "description": "True once the user approved a run over the agent "
                                                            "guideline."},
        },
    },
}


def _reply(status: str, **fields: Any) -> str:
    return json.dumps({"status": status, **fields}, ensure_ascii=False, default=str)


def _entry(ctx: Any) -> str:
    try:
        return "cron" if ctx.current_cron_execution() else "user"
    except Exception:  # noqa: BLE001 - a Hermes without cron support is a plain user entry
        return "user"


def make_handler(ctx: Any, wiring: Any) -> Callable[..., str]:
    def handler(args: dict, task_id: str = "", session_id: str = "", **_: Any) -> str:
        try:
            return _handle(ctx, wiring, args if isinstance(args, dict) else {}, session_id or task_id)
        except Exception as exc:  # noqa: BLE001 - a tool answers, it does not raise into the agent loop
            from .engine import _clip, _masked

            return _reply("failed", message=_clip(_masked("%s: %s" % (type(exc).__name__, exc)), 500))

    return handler


def _handle(ctx: Any, wiring: Any, args: dict, session: str) -> str:
    given = [key for key in KEYS if isinstance(args.get(key), str) and args[key].strip()]
    if len(given) != 1 or any(key in args and key not in given and args[key] not in (None, "") for key in KEYS):
        return _reply("error", message="give exactly one of script, name or task, each a non-empty string")
    entry = _entry(ctx)
    if not wiring.optin.allowed(session, entry):
        tokens = optin.estimate(PLANNED_AGENTS)
        return _reply("refused", message=optin.refusal(PLANNED_AGENTS, tokens), tokens=tokens)
    from .engine import NAME

    kind, value = given[0], args[given[0]]
    if kind == "name" and not NAME.fullmatch(value):
        return _reply("error", message="name is not a workflow name: letters, digits, '.', '_' and '-' only, at "
                                       "most 128 characters, starting with a letter or digit")
    if kind == "script" and NAME.fullmatch(value.strip()):  # the engine would read it as a saved workflow's name
        return _reply("error", message="script holds one word, which is a name: pass it as name, or give script "
                                       "source")
    host = wiring.host()
    run_args = args.get("args")
    if kind == "task":
        call = route_request(value, run_args)
        source, run_args, host = call["name"], call["args"], RouterHost(host, wiring.router_model())
    else:
        source = value
    summary = wiring.engine(host).run(source, run_args, wiring.budget(session), entry=entry, approved=args.get("approved") is True)
    return _reply(summary.status, run_id=summary.run_id, text=summary.text)
