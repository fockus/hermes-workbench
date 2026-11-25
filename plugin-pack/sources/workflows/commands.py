"""``/workflow``, the saved-workflow commands and ``hermes workflow`` (workflow-engine Task 16: REQ-006, REQ-025).

``register_commands(ctx, wiring)`` adds ``/workflow list | show | stop | skip | resume`` (``Controls.command``), one
``/<name>`` per saved workflow of the session's workspace and the ``hermes workflow`` CLI (``Controls.cli``).
Saved names are read at registration and again whenever a session opens (``on_session_open``), so a project opened
later gets its commands. A slash command runs before any agent turn, and a workflow's agents need a parent agent
turn (SubagentsAPI), so a saved command queues a user turn of the same session that calls the ``workflow`` tool
(``ctx.inject_message``; the turn says "workflow", so the opt-in holds). A context that cannot queue a turn (the
frozen contract's fake) runs it in the command call on the trusted entry ``command``. The plugin's own templates
(chain, route, ...) get no command, and a name that clashes with a stock command is refused by Hermes itself.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from . import saved
from .controls import USAGE, cli
from .engine import NAME

HINT = "list|show <run>|stop <run>|skip <run> <label>|resume <run>"
CLI_USAGE = "run <file|name> [--args JSON] | list | show <id> | stop <id> | resume <id>"


def register_commands(ctx: Any, wiring: Any) -> None:
    ctx.register_command("workflow", lambda raw_args="": str(wiring.controls().command(raw_args)),
                         description="Workflow runs: " + USAGE.removeprefix("usage: "), args_hint=HINT)
    known = {"workflow"}  # lowercase: Hermes lowercases a command name, so `Workflow.js` would replace /workflow
    _add_saved(ctx, wiring, known)
    ctx.register_hook("on_session_open", lambda **_: _add_saved(ctx, wiring, known))
    ctx.register_cli_command("workflow", help="Run and control workflows", setup_fn=_setup,
                             handler_fn=lambda ns: cli(list(ns.words), sys.stdout, wiring.controls(os.getcwd())),
                             description="hermes workflow " + CLI_USAGE)


def _add_saved(ctx: Any, wiring: Any, known: set[str]) -> None:
    for name in _saved_names(ctx.session_cwd() or str(Path.home())):
        if name.lower() not in known:
            known.add(name.lower())
            ctx.register_command(name, _saved_handler(ctx, wiring, name), description="Run the saved workflow " + name,
                                 args_hint="[args]")


def _saved_names(cwd: str) -> list[str]:
    """Saved workflows with a command: not the plugin's templates, a name the engine resolves."""
    own = []
    for name in saved.names(cwd):
        path = saved.find(name, cwd)
        if NAME.fullmatch(name) and path is not None and path.parent != saved.TEMPLATES:
            own.append(name)
    return own


def _saved_handler(ctx: Any, wiring: Any, name: str) -> Callable[..., str]:
    def run(raw_args: str = "", session_key: str = "", **_: Any) -> str:
        args = _args(raw_args)
        inject = getattr(ctx, "inject_message", None)
        if inject is None:
            return wiring.engine().run(name, args, None, entry="command").text
        call = {"name": name} if args is None else {"name": name, "args": args if isinstance(args, dict)
                                                    else {"text": args}}
        if inject("Run the saved workflow %s: call the workflow tool with exactly %s." % (name, json.dumps(call)),
                  session_key=session_key or None):
            return "queued workflow %s as a turn of this session" % name
        return ("workflow %s not started: this Hermes did not accept a queued turn (a gateway session needs "
                "plugins.entries.%s.allow_gateway_injection: true)" % (name, getattr(ctx, "plugin_id", "workflows")))

    return run


def _args(raw: str) -> Any:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def _setup(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("words", nargs=argparse.REMAINDER, help=CLI_USAGE)
