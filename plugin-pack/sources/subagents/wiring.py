"""Phase 2 wiring (spec Task 13; REQ-001, REQ-033): the team gates go last in ``register``, and what a part that
fails midway already registered is named in the one warning instead of hidden."""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger("subagents")

Registration = tuple[Callable[..., Any], tuple[Any, ...]]
RECORDED = ("register_delegation_provider", "register_hook", "register_tool", "register_command",
            "register_cli_command", "on_unload")


class Recorder:
    """The plugin context as the parts see it: every registration they make through it is named in ``made``."""

    def __init__(self, ctx: Any):
        self._ctx = ctx
        self.made: list[str] = []

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._ctx, name)
        if name not in RECORDED or not callable(attr):
            return attr

        def recorded(*args: Any, **kw: Any) -> Any:
            out = attr(*args, **kw)
            first = args[0] if args else ""
            self.made.append("%s %s" % (name, first if isinstance(first, str) else type(first).__name__))
            return out

        return recorded


def phase2_parts(ctx: Any) -> list[Registration]:
    """The delegation.backend check at the first session start, then the team gates, ``/team``, the owner contract and
    the lead's claim renewal (team_gates.install) after every phase 1 part. A host that cannot stop the renewal thread
    at unload gets no team mode."""
    from . import team_gates

    parts: list[Registration] = [(ctx.register_hook, ("on_session_start", BackendCheck().on_session_start))]
    if not callable(getattr(ctx, "on_unload", None)):
        logger.info("subagents: this Hermes has no PluginContext.on_unload, so team mode is off (its claim renewal "
                    "could not be stopped)")
        return parts
    return parts + [(team_gates.install, (ctx,))]


class BackendCheck:
    """Agent types, names and limits act only while stock routes delegate_task through this plugin: the first session
    of a load is told once (a plugin load itself stays quiet when every seam matches)."""

    def __init__(self) -> None:
        self.done = False

    def on_session_start(self, **_: Any) -> None:
        if not self.done:
            self.done = True
            warn_backend()


def warn_backend() -> None:
    try:
        from agent.delegation_backends import saved_backend

        backend = saved_backend()
    except Exception:  # noqa: BLE001 - an unreadable config is doctor's to explain, not a load failure
        return
    if backend != "subagents":
        logger.warning("subagents: delegation.backend is %r, so agent types, names and limits stay off and stock "
                       "delegate_task runs unchanged; run `hermes subagents setup`", backend)
