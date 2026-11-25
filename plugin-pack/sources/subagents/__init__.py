"""hermes-subagents: typed agents, orchestrator mode, limits, names, short results and messaging on
top of stock delegate_task (design.md). Hermes loads this package and calls ``register(ctx)``."""

from __future__ import annotations

import logging
import os
from types import SimpleNamespace
from typing import Any, Callable

logger = logging.getLogger("subagents")

TOOLSET = "subagents"
DOCTOR_HELP = "Check or write the delegation settings agent types need (doctor | setup)"
AGENTS_HELP = "Message one of your named agents: send <name> <text>"

Registration = tuple[Callable[..., Any], tuple[Any, ...]]


def register(ctx: Any) -> None:
    """Hermes plugin entry point (phase 1 wiring). A changed Hermes seam or a part that fails to build
    registers nothing but a warning, so stock delegate_task keeps working and a Hermes start never
    breaks (REQ-033). No second delegation tool is ever registered (REQ-001)."""
    try:
        from .seams import missing_seams, private_drift

        missing = missing_seams(ctx)
        if missing:
            logger.warning("subagents: Hermes changed the seams this plugin builds on (%s); its extensions are off "
                           "and stock delegate_task keeps working unchanged", ", ".join(missing))
            return
        for drift in private_drift():
            logger.warning("subagents: a private Hermes surface changed, using the fallback: %s", drift)
        from .wiring import Recorder

        recorder = Recorder(ctx)
        registrations = _build(recorder)
    except Exception as exc:  # the plugin boundary: whatever broke, Hermes starts with stock delegation
        logger.warning("subagents: extensions are off, stock delegate_task keeps working: %s: %s",
                       type(exc).__name__, exc)
        return
    try:
        for register_fn, args in registrations:
            register_fn(*args)
    except Exception as exc:  # a part failed midway (the team gates go last): Hermes keeps what it already got
        logger.warning("subagents: registering failed, %s: %s; already registered: %s", type(exc).__name__, exc,
                       ", ".join(recorder.made) or "nothing")
        return


def __getattr__(name: str) -> Any:
    """``render_soul_brick`` (soul-composer's brick provider protocol), imported only when asked for."""
    if name == "render_soul_brick":
        from .orchestrator import render_soul_brick

        return render_soul_brick
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


def _cwd(ctx: Any) -> Callable[[], str]:
    """The calling session's workspace; "" for a gateway session with none (stock's classic CLI context already
    answers its own working directory): then only the profile and user definition sources are read."""
    session_cwd = getattr(ctx, "session_cwd", None)
    return lambda: (session_cwd() if callable(session_cwd) else "") or ""


def _unattended(ctx: Any) -> Callable[[], bool]:
    """A kanban worker, a cron run, or a code-mode eval session (its agent carries ``orchestrator_exempt = True``,
    codemode/eval/exempt.py): nobody to converse with, so no orchestrator limits."""
    cron = getattr(ctx, "current_cron_execution", None)

    def check() -> bool:
        from .callers import active_parent

        if os.environ.get("HERMES_KANBAN_TASK") or getattr(active_parent(), "orchestrator_exempt", False) is True:
            return True
        try:
            return callable(cron) and cron() is not None
        except Exception:  # no cron runtime here means no cron run
            return False

    return check


def _chain(*handlers: Callable[..., Any]) -> Callable[..., Any]:
    """One pre_tool_call: each handler sees the arguments the previous ones modified (stock merges
    separate hooks from the original arguments, so the spawn gate must follow enforcement here)."""

    def pre_tool_call(tool_name: str = "", args: Any = None, **kw: Any) -> dict[str, Any] | None:
        current, changed = args, {}
        for handler in handlers:
            verdict = handler(tool_name=tool_name, args=current, **kw)
            if not isinstance(verdict, dict):
                continue
            if verdict.get("action") == "block":
                return verdict
            if verdict.get("action") == "modify" and isinstance(verdict.get("args"), dict):
                changed.update(verdict["args"])
                current = {**(current if isinstance(current, dict) else {}), **verdict["args"]}
        return {"action": "modify", "args": changed} if changed else None

    return pre_tool_call


def _build(ctx: Any) -> list[Registration]:
    """Every part is built first and the registrations returned, so a failure registers nothing."""
    from hermes_constants import get_hermes_home

    from . import doctor, enforce, kanban_store, limiter, messages, names, orchestrator, provider
    from . import api_hooks, results
    from .callers import active_parent, routed_here
    from .children import Children
    from .calls import Calls
    from .launch import SPAWN, Launcher
    from .toolfilter import CodeModeLink
    from .workspaces import Registries

    cwd = _cwd(ctx)
    agents = Registries(cwd)  # the spawning session's workspace and profile, read at each spawn
    store = kanban_store.get_store()

    def persisted_root(session_id: str) -> str:
        rows = store.read("SELECT root FROM subagents_roots WHERE session = ?", (session_id,))
        return str(rows[0][0]) if rows else ""

    callers, calls = enforce.CallerMap(persisted_root), Calls()
    unattended = _unattended(ctx)
    enforcer = enforce.Enforcer(agents, callers, ctx.get_config("orchestrator_mode"), unattended=unattended,
                                calls=calls, children=Children(kanban_store.get_store()), routed_here=routed_here)
    orch = orchestrator.Orchestrator(cwd, is_child=lambda session_id: callers.get(session_id) is not None,
                                     unattended=unattended, enabled=enforcer.mode != "off")
    named = names.Names(kanban_store.get_store(), callers)
    shortener = results.Shortener(callers)
    codemode = CodeModeLink(enforcer.tool_filter)
    host_inject = getattr(ctx, "inject_message", None)

    def recorded_inject(text: str, session_key: str = "") -> Any:
        """A user-role turn the plugin queues into a main session (a chat's key when one was bound, else the
        calling session's); the enforcer knows it is not the user."""
        enforcer.note_injected(text)
        return host_inject(text, role="user", session_key=session_key) if session_key else host_inject(text, role="user")

    inject = recorded_inject if callable(host_inject) else None
    def settle(child_session: str) -> None:
        """A child stock closed unrun: its name goes, then its slot and its messages as at a stop."""
        named.discard(child_session)
        launcher.subagent_stop(child_session_id=child_session)

    launcher = Launcher(limiter.get_limiter(), callers, calls, inject, settle)
    messenger = messages.Messenger(named, callers, inject=inject, slots=launcher)
    def staged(task: Any) -> Any:
        """The staged slot of the task stock is routing now (None: it matches no staged spawn)."""
        parent = active_parent()
        return calls.peek(str(getattr(parent, "session_id", "") or ""), str(getattr(parent, "_current_turn_id", "")
                          or ""), task.get("index"), task) if parent is not None else None

    route = provider.SubagentsProvider(agents, admit=launcher.admit, staged=staged)

    def tool_result(tool_name: str = "", args: Any = None, result: Any = None, session_id: str = "",
                    tool_call_id: str = "", **kw: Any):
        """One transform: a child's result shortened, then a delegate_task call's queued note (stock keeps the first
        string a hook returns, so this plugin returns one)."""
        shortened = shortener.transform_tool_result(tool_name=tool_name, args=args, result=result, **kw)
        final = shortened if shortened is not None else result
        noted = launcher.tool_result(final if isinstance(final, str) else "", session_id, tool_call_id or "") \
            if tool_name == SPAWN else None
        return noted if noted is not None else shortened

    def post_compaction(session_id: str = "", old_session_id: str = "", **_: Any) -> None:
        """Stock rotating compaction gives a session a new id: a child's caller, ancestry and slot follow it; a
        main session keeps its conversation root (names, descendants, quota), persisted for a restart."""
        if callers.rename(old_session_id, session_id) is not None:
            launcher.rebind(old_session_id, session_id)
        elif session_id and old_session_id and session_id != old_session_id:
            root = callers.root_of(old_session_id)
            callers.alias(session_id, root)
            store.write("INSERT OR REPLACE INTO subagents_roots (session, root) VALUES (?, ?)", (session_id, root))

    hooks = [
        ("pre_tool_call", _chain(enforcer.pre_tool_call, named.pre_tool_call, messenger.pre_tool_call,
                                 launcher.pre_tool_call)),
        ("post_tool_call", launcher.post_tool_call), ("pre_llm_call", enforcer.pre_llm_call),
        ("pre_llm_call", orch.pre_llm_call),
        ("pre_llm_call", launcher.pre_llm_call),
        ("pre_llm_call", named.pre_llm_call), ("pre_llm_call", codemode.hook),
        # the caller map binds a child first; names, the limiter and the console read it
        ("subagent_start", enforcer.subagent_start), ("subagent_start", named.subagent_start),
        ("subagent_start", launcher.subagent_start), ("subagent_start", orch.subagent_event),
        ("subagent_start", codemode.hook),
        ("subagent_stop", named.subagent_stop), ("subagent_stop", launcher.subagent_stop),
        ("subagent_stop", orch.subagent_event), ("subagent_stop", api_hooks.stopped(callers)),
        ("on_session_start", orch.on_session_start), ("on_session_start", named.on_session_start),
        ("post_compaction", post_compaction), ("post_compaction", orch.post_compaction),
        ("transform_llm_output", shortener.transform_llm_output), ("transform_tool_result", tool_result),
    ]
    registrations: list[Registration] = [(ctx.register_delegation_provider, (route,))]
    registrations += [(ctx.register_hook, hook) for hook in hooks]
    registrations += [
        (ctx.register_tool, ("send_message", TOOLSET, messages.SCHEMA, messenger.send_message)),
        # stock Hermes has a built-in /agents and skips a plugin's; /subagents send reaches the same handler there
        (ctx.register_command, ("agents", messenger.agents_command, AGENTS_HELP)),
        (ctx.register_command, ("subagents", messenger.agents_command, AGENTS_HELP)),
        (ctx.register_cli_command, ("subagents", DOCTOR_HELP, doctor.setup_parser, doctor.handle)),
        (codemode.attach, ()),
        # the parts SubagentsAPI() uses by default (api.py PluginParts), one per Hermes home
        (limiter.process_shared("api_parts", dict).__setitem__,
         (str(get_hermes_home()), SimpleNamespace(messenger=messenger, launcher=launcher, cwd=cwd, pre_tool_call=_chain(
             enforcer.pre_tool_call, named.pre_tool_call)))),
    ]
    from .wiring import phase2_parts

    registrations += phase2_parts(ctx)  # last: the team exists only when every other part registered
    orch.on_load()  # after every part is built: a failed build leaves SOUL.md alone
    return registrations
