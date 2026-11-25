"""The Hermes surfaces hermes-subagents builds on (design.md § Contract) and the register-time check
that names every one that changed (REQ-033). Hermes modules are imported only inside the check, so
importing the plugin never pulls in the agent runtime."""

from __future__ import annotations

import inspect
from typing import Any, Callable

CONTEXT_METHODS = ("register_delegation_provider", "register_hook", "register_tool", "register_command",
                   "register_cli_command")
DELEGATE_SIGNATURE = ("goal", "context", "tasks", "role", "background", "output_schema", "action",
                      "subagent_id", "message", "parent_agent")
DELEGATE_SCHEMA = ("tasks", "action", "subagent_id", "message")
DELEGATE_TASK_ITEM = ("goal", "context", "output_schema")
DELEGATE_ACTIONS = ("spawn", "continue", "steer", "stop")
PROVIDER_METHODS = {"task_parameters": ("self", "config"), "route_task": ("self", "task", "config")}
ROUTE_KEYS_USED = ("model", "provider", "reasoning_effort")
OWN_TASK_PARAMETERS = ("agent_type", "name")
KANBAN_FUNCTIONS = ("create_task", "list_tasks", "claim_task", "heartbeat_claim", "complete_task", "link_tasks",
                    "add_comment", "block_task", "reopen_review_task", "reclaim_task", "get_task", "list_events",
                    "latest_run", "write_txn", "invalidate_descendants_for_parent_reopen", "_append_event")
KANBAN_PID_FUNCTIONS = ("_terminate_reclaimed_worker",)  # team_gates_review stops a reclaimed worker by pid
HOOKS = ("pre_tool_call", "pre_llm_call", "post_compaction", "subagent_start", "subagent_stop",
         "kanban_task_completed", "kanban_task_claimed", "on_kanban_worker_stale_claim")


def _parameters(fn: Any) -> list[str]:
    try:
        return list(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        return []


def _var_parameters(fn: Any) -> set[str]:
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return set()
    return {p.name for p in params if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD)}


def _delegate_task() -> list[str]:
    import tools.delegate_tool as dt

    signature = set(_parameters(getattr(dt, "delegate_task", None)))
    if signature and not (signature - _var_parameters(getattr(dt, "delegate_task", None))):
        signature = set(DELEGATE_SIGNATURE)  # a (*args, **kwargs) wrapper hides its parameters: the schema decides
    schema = getattr(dt, "DELEGATE_TASK_SCHEMA", None) or {}
    props = (schema.get("parameters") or {}).get("properties") or {}
    item = ((props.get("tasks") or {}).get("items") or {}).get("properties") or {}
    actions = set((props.get("action") or {}).get("enum") or ())
    gone = {n for n in DELEGATE_SIGNATURE if n not in signature}
    gone |= {n for n in DELEGATE_SCHEMA if n not in props} | {n for n in DELEGATE_TASK_ITEM if n not in item}
    gone |= {"action=%s" % a for a in DELEGATE_ACTIONS if a not in actions}
    return ["delegate_task.%s" % n for n in sorted(gone)]


def _provider() -> list[str]:
    from agent import delegation_provider as dp
    from agent import delegation_registry

    provider = getattr(dp, "DelegationProvider", None)
    missing = ["DelegationProvider.%s" % name for name, params in PROVIDER_METHODS.items()
               if tuple(_parameters(getattr(provider, name, None))) != params]
    missing += ["ROUTE_KEYS.%s" % k for k in ROUTE_KEYS_USED if k not in getattr(dp, "ROUTE_KEYS", ())]
    missing += ["RESERVED_TASK_PARAMETERS.%s" % k for k in OWN_TASK_PARAMETERS
                if k in getattr(dp, "RESERVED_TASK_PARAMETERS", ())]
    if not callable(getattr(delegation_registry, "configured_backend", None)):
        missing.append("delegation_registry.configured_backend")
    return missing


def _kanban() -> list[str]:
    from hermes_cli import kanban_db

    first = {**{name: "conn" for name in KANBAN_FUNCTIONS}, **{name: "pid" for name in KANBAN_PID_FUNCTIONS}}
    return ["kanban_db.%s" % name for name, param in first.items()
            if _parameters(getattr(kanban_db, name, None))[:1] != [param]]


def _hooks() -> list[str]:
    from hermes_cli.plugins import VALID_HOOKS

    return ["hook %s" % name for name in HOOKS if name not in VALID_HOOKS]


CHECKS: tuple[Callable[[], list[str]], ...] = (_delegate_task, _provider, _kanban, _hooks)


def missing_seams(ctx: Any) -> list[str]:
    """Names of the seams this Hermes lacks or changed; empty when every one matches."""
    missing = ["PluginContext.%s" % name for name in CONTEXT_METHODS if not callable(getattr(ctx, name, None))]
    for check in CHECKS:
        try:
            missing += check()
        except ImportError as exc:
            missing.append("module %s" % (exc.name or exc))
    return missing


def _turn_id_seam() -> list[str]:
    """subagent_start's parent_turn_id and pre_tool_call's turn_id both come from the agent's private
    ``_current_turn_id`` (tools/delegate_tool.py:381, agent/agent_runtime_helpers.py:2456); the caller map
    keys spawns by it. Fallback: callers.CallerMap matches by parent session and goal."""
    import tools.delegate_tool as dt

    try:
        source = inspect.getsource(dt._build_child_agent)
    except (AttributeError, OSError, TypeError):
        source = ""
    if "_current_turn_id" in source:
        return []
    return ["tools.delegate_tool._build_child_agent no longer passes _current_turn_id (spawns are matched by "
            "parent session and goal)"]


def _active_parent_seam() -> list[str]:
    """The spawning agent (callers.active_parent); fallback: the recorded main agent, then a session id."""
    from agent import subagent_lifecycle

    if callable(getattr(subagent_lifecycle, "get_active_subagent_parent", None)):
        return []
    return ["agent.subagent_lifecycle.get_active_subagent_parent is gone (messages to named agents run as the "
            "recorded main agent or a session id)"]


PRIVATE_CHECKS: tuple[Callable[[], list[str]], ...] = (_turn_id_seam, _active_parent_seam)


def private_drift() -> list[str]:
    """Private stock surfaces the plugin reads with a fallback; a drift is warned about, never fatal. The
    live registry and the child's ``_delegate_parent_ref``/``_delegate_output_schema`` are not read at all:
    stock sets them only after subagent_start (tools/delegate_tool.py:378-386 vs :526, delegate_tool_child_run.py:440)."""
    drift: list[str] = []
    for check in PRIVATE_CHECKS:
        try:
            drift += check()
        except ImportError as exc:
            drift.append("module %s" % (exc.name or exc))
    return drift
