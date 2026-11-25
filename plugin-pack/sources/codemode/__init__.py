"""Hermes plugin "codemode": the model writes Python that runs in a Monty sandbox (pinned
pydantic-monty in ``codemode/.deps/cp<XY>``, see ``deps.py``) and reaches every suitable tool
through a typed SDK; only the script's result, short logs and errors reach the context (design.md).

Importing the package loads nothing else: no Monty, no Hermes module. The plugin never registers,
overrides or wraps the stock execute_code tool (REQ-005).
"""

from __future__ import annotations

import functools
import logging

__all__ = ["register", "register_result_schema", "register_tool_filter", "unregister_tool_filter"]

logger = logging.getLogger(__name__)
SESSION_END_HOOKS = ("on_session_finalize", "on_session_reset", "on_session_delete")


def register_result_schema(tool: str, schema: dict) -> None:
    """For other plugins: declare the JSON schema of a tool's result, which becomes the return
    type of its SDK stub (REQ-010)."""
    from .sdk import register_result_schema as declare

    declare(tool, schema)


def register_tool_filter(fn, owner=None) -> bool:
    """For other plugins: add a per-session filter ``fn(session_id, tools) -> tools`` that may remove tools
    from the declared list and from the raw tool_search catalog (REQ-036). Needs the core tool-list hook
    (``hermes codemode patch``); False when the running Hermes has none, so the filter does nothing.
    ``owner``: the registering plugin's context; its unload removes the filter (else call
    ``unregister_tool_filter`` from the owner's own unload)."""
    from .patcher import register_tool_filter as attach

    return attach(fn, owner)


def unregister_tool_filter(fn) -> None:
    """For other plugins: remove a filter added by ``register_tool_filter`` and its sessions' answers."""
    from .patcher import unregister_tool_filter as detach

    detach(fn)


def _prompt_section(get_config, info) -> str:
    from . import modes
    from .eval.live import direct_baseline

    if direct_baseline(info.get("session_id")):
        return ""
    mode = modes.active_mode(modes.read_settings(get_config))
    return modes.prompt_section(bulk=True, mode=mode)


def _direct_tools(entries) -> tuple[str, ...]:
    """Registry tools whose full schemas every request already carries (tool_search does not defer
    them: neither on its defer list nor an ordinary plugin or MCP tool); the codemode catalog leaves
    them out. Unknown defer list: none, so nothing is hidden."""
    from tools.tool_search import is_deferrable_tool_name

    from .cli import _current_defer

    try:
        deferred = frozenset(_current_defer())
        return tuple(
            e.name for e in entries
            if not e.name.startswith("mcp_") and not is_deferrable_tool_name(e.name, deferred)
        )
    except Exception:
        return ()


def register(ctx) -> None:
    """Spec Tasks 14 and 15: the codemode tool (bulk in its SDK), its on-mode prompt section, the
    session hooks, ``hermes codemode doctor|eval|patch``, the start check of the core tool-list hook
    and ``codemode.api`` for ultracode. Never opens the Monty pool: a plugin-host process or a
    changed stock helper (seams.STOCK) registers nothing, missing Monty registers only the CLI, each
    with one warning. A refused mandatory registration (prompt section, session-end hook, the tool:
    ``register_tool`` returning None or another handler holding the name) raises,
    so the stock loader disposes everything this plugin had published: no prompt is left telling
    the model to use a tool it cannot call. Snapshot resume is deferred (Task 13) and only mode is
    Task 18."""
    _register(ctx)


def _register(ctx) -> None:
    from . import api, cli, deps, modes, seams

    api.withdraw()
    problem = seams.host_problem() or seams.stock_problem()
    if problem:
        logger.warning("codemode is not registered: %s", problem)
        return
    _hook_check()
    found = deps.activate()
    ctx.register_cli_command(
        "codemode",
        "code mode: doctor, eval and patch",
        cli.setup,
        functools.partial(cli.run, ctx.get_config),
    )
    if not found.ok:
        logger.warning(
            "codemode is not registered: %s; %s", found.problem, cli.install_hint(found)
        )
        return
    from . import sdk, usage

    from tools.registry import registry

    # the stable description every session shares: registry tools only, since every agent-bound
    # script is refused plugin commands (host calls still find them in their SDK)
    entries = registry.get_all_entries()
    schema = modes.tool_schema(sdk.build_sdk(entries, {}), usage.load(), direct=_direct_tools(entries))
    runtime = api.LimitedRuntime(found.binary)
    ctx.on_unload(runtime.close)
    # what a callable tool needs comes first: a refusal here leaves no tool without its prompt
    # section or session-end cleanup
    ctx.register_system_prompt_section(modes.SECTION_ID, functools.partial(_prompt_section, ctx.get_config))
    for hook in SESSION_END_HOOKS:
        ctx.register_hook(hook, runtime.end_session)
    handle = ctx.register_tool("codemode", modes.TOOLSET, schema, runtime.handle, emoji="🐍")
    if handle is None or not seams.owns(runtime.handle):  # refused: the stock loader disposes the rest
        raise RuntimeError("codemode is not registered: another tool already holds the name codemode")
    runtime.publish()
    _optional(ctx, runtime, seams)


def _hook_check() -> bool:
    """Start check: is the core tool-list hook present? On mode needs no patch, so its absence is info only."""
    from .patcher import hook_present

    present = hook_present()
    logger.info("codemode: the core tool-list hook is %s", "present" if present else
                "absent (other plugins' tool filters stay inactive; `hermes codemode patch` installs it)")
    return present


def _optional(ctx, runtime, seams) -> None:
    """What codemode works without: each refusal is one warning, never a lost registration."""
    try:
        from .api import deadline_floor

        declared = seams.declare_deadline(runtime.handle, deadline_floor)
    except Exception:
        declared = False
    if declared is False:
        logger.warning("codemode: its tool deadline could not be declared; Hermes's generic tool deadline "
                       "may cut a long script")
    if not seams.keep_declared():
        logger.warning(
            "codemode: toolsets._HERMES_CORE_TOOLS changed shape, so tool_search may defer codemode "
            "behind its bridge; `hermes codemode doctor` shows it"
        )
    try:
        ctx.register_hook("on_session_start", runtime.warm)  # the first call opens the pool itself
    except Exception as exc:
        logger.warning("codemode: the session-start warm-up was not registered: %s", exc)
