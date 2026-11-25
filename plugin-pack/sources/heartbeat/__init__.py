"""kanban-heartbeat: ``hermes heartbeat tick | install | status``.

The tick is plain Python run by one stock no-agent cron job; it never imports a model client. Modules
load lazily from the handler, so registering the plugin costs a Hermes process nothing.
"""

from __future__ import annotations

HELP = "Kanban heartbeat: find stuck tasks on every board (tick), install its cron job, show its state"


def _setup(parser) -> None:
    sub = parser.add_subparsers(dest="heartbeat_command")
    sub.add_parser("tick", help="one heartbeat pass over every kanban board (run by the cron job)")
    sub.add_parser("install", help="create or update the heartbeat cron job (every 10 min, no agent)")
    sub.add_parser("status", help="show the cron job, the stock contract probe and the journal size")


def register(ctx) -> None:
    def handle(args) -> None:
        command = getattr(args, "heartbeat_command", None)
        if command == "tick":
            from . import tick

            tick.main(ctx)
        elif command == "install":
            from . import install

            print(install.install(ctx))
        elif command == "status":
            from . import install

            print(install.status())
        else:
            print("usage: hermes heartbeat tick | install | status")

    ctx.register_cli_command("heartbeat", HELP, _setup, handle)

    def slash(raw: str = "") -> str:
        from . import command

        return command.handle(ctx, raw)

    # stock Hermes skips a plugin command named like a built-in, and /heartbeat is one
    ctx.register_command("kanban-ask", slash, description="Answer a kanban heartbeat question: /kanban-ask [qid]",
                         args_hint="[qid] [action] [text]")
