"""The Hermes seams register() checks before it wires code-mode (spec Task 15; hardening G3).

1. ``STOCK``: ``model_tools.handle_function_call`` and every private stock helper the nested path
   calls (nested.py, bridge.py, caller.py), by presence and parameter names; any mismatch
   registers nothing. The plugin loader runs inside model_tools' own import (model_tools.py:151-164
   discovers plugins before the function is defined), so a module that is not imported yet, or
   half-imported, is read from its source; importing it from register() would wait on the import
   lock past Hermes' load deadline.
2. ``toolsets._HERMES_CORE_TOOLS``: tool_search defers every tool that is not core, plugin tools
   included (tools/tool_search.py:152-165), and reads that list on each call. Appending
   "codemode" in-process keeps it declared to the model; no Hermes file or setting changes.
3. ``ToolEntry.deadline_floor``: the agent cuts a tool call at its generic deadline (420 s by
   default) unless the tool declares how long one call may block; the executor then raises the
   deadline to that floor (``agent.tool_executor_deadline``). ``PluginContext.register_tool`` does
   not forward it, so register() sets it on codemode's own entry (``declare_deadline``).
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import os
import sys

TOOL = "codemode"
CALL = "handle_function_call"
CALL_PARAMETERS = (
    "function_name",
    "function_args",
    "task_id",
    "tool_call_id",
    "session_id",
    "turn_id",
    "api_request_id",
    "user_task",
    "conversation_id",
)
# Every private stock helper the nested path calls (U4, upstream.md): module, name, the leading
# positional parameters it is called with, the keyword parameters it must take.
STOCK = (
    ("model_tools", CALL, CALL_PARAMETERS[:2], CALL_PARAMETERS[2:]),
    ("model_tools", "suppress_post_tool_call_hook", ("suppressed",), ()),
    ("agent.tool_executor", "_run_agent_tool_execution_middleware", ("agent",),
     ("function_name", "function_args", "effective_task_id", "tool_call_id", "execute", "scope_block",
      "begin_execution", "authorization_gate")),
    ("agent.tool_executor", "_ConcurrentToolAuthorizationGate", (), ()),
    ("agent.tool_executor", "_BatchAbandoned", (), ()),
    ("agent.tool_executor", "_ToolCallRef", ("name", "args", "task_id", "call_id", "trace"), ()),
    ("agent.tool_executor", "_ToolCallRef.emit_post", ("self", "agent", "result"), ("trace",)),
    ("agent.tool_executor", "_interrupt_worker_tids", ("agent", "tids"), ()),
    ("agent.tool_executor", "_max_workers_for_tool_batch", ("runnable_calls",), ()),
    ("agent.tool_executor", "_resolve_sequential_tool_timeout", (), ()),
    ("agent.tool_executor", "_resolve_concurrent_tool_timeout", (), ()),
    ("agent.tool_dispatch_helpers", "_plan_tool_batch_segments", ("tool_calls",), ("execution_cwd",)),
    ("agent.display", "_detect_tool_failure", ("tool_name", "result"), ()),
    ("agent.agent_runtime_helpers", "invoke_tool",
     ("agent", "function_name", "function_args", "effective_task_id", "tool_call_id"),
     ("pre_tool_block_checked", "skip_tool_request_middleware", "skip_tool_execution_middleware")),
    ("run_agent", "AIAgent._append_guardrail_observation",
     ("self", "tool_name", "function_args", "function_result"), ("failed", "tool_call_id")),
    ("agent.turn_explainers", "TurnExplainersMixin._record_file_mutation_result",
     ("self", "tool_name", "args", "result", "is_error"), ("task_id",)),
    ("tools.tool_search_validation", "validate_deferred_call_args", ("name", "args"), ()),
    ("tools.tool_search", "scoped_deferrable_names", ("tool_defs",), ()),
    ("agent.tool_executor_deadline", "declared_deadline_floor", ("name", "args"), ()),
    ("agent.tool_executor_deadline", "deadline_with_floor", ("generic", "floor"), ()),
    ("agent.tool_dispatch_helpers", "_extend_tool_timeout", ("tool_name", "default"), ()),
)


def _defined(body: list, name: str) -> ast.AST | None:
    return next((n for n in body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == name), None)


def _node_parameters(node: ast.AST) -> list[str]:
    if isinstance(node, ast.ClassDef):  # its constructor's parameters, or a dataclass's fields
        init = _defined(node.body, "__init__")
        if init is not None:
            return _node_parameters(init)[1:]
        return [n.target.id for n in node.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)]
    if not isinstance(node, ast.FunctionDef):
        return []
    a = node.args
    return [p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)]


def _source_parameters(module: str, dotted: str) -> list[str] | None:
    """Parameters read from the module's source: it may be half-imported (the plugin loader runs
    inside model_tools' import), and importing it from register() would wait on its import lock."""
    try:
        spec = importlib.util.find_spec(module)
        with open(spec.origin, encoding="utf-8") as fh:  # type: ignore[union-attr, arg-type]
            body = ast.parse(fh.read()).body
    except (AttributeError, ImportError, OSError, SyntaxError, TypeError, ValueError):
        return None  # not importable, no source file, or unreadable
    node = None
    for part in dotted.split("."):
        node = _defined(body, part)
        if node is None:
            return None
        body = getattr(node, "body", [])
    return _node_parameters(node) if node is not None else None


def _parameters(module: str, dotted: str) -> list[str] | None:
    """From the loaded module; from its source when it is not imported yet or still importing."""
    loaded = found = sys.modules.get(module)
    for part in dotted.split("."):
        found = getattr(found, part, None)
    if callable(found):
        try:
            return list(inspect.signature(found).parameters)
        except (TypeError, ValueError):
            return _source_parameters(module, dotted)
    if loaded is None or getattr(getattr(loaded, "__spec__", None), "_initializing", False):
        return _source_parameters(module, dotted)
    return None


def host_problem() -> str | None:
    """Why codemode must not run in this process: the stock plugin-host child of
    ``plugins.isolation: host`` (recognised as stock ``isolation_mode()`` does). A tool call reaches
    that process over RPC without the calling agent, so no grant, approval or turn identity."""
    try:
        from hermes_cli.plugin_isolation import HOST_PROCESS_ENV
    except ImportError:
        HOST_PROCESS_ENV = "HERMES_PLUGIN_HOST_PROCESS"
    if os.environ.get(HOST_PROCESS_ENV) != "1":
        return None
    return ("plugins.isolation is host, where a tool call reaches this plugin without the calling agent's "
            "tool grant, approvals and turn identity; use plugins.isolation: in_process for codemode")


def stock_problem() -> str | None:
    """Why the stock helpers cannot carry codemode's nested calls, or None when they can."""
    for module, dotted, positional, keywords in STOCK:
        names = _parameters(module, dotted)
        if names is None:
            return "%s.%s was not found" % (module, dotted)
        missing = [p for p in keywords if p not in names]
        if tuple(names[: len(positional)]) != positional or missing:
            return "%s.%s%s no longer takes %s" % (
                module, dotted, tuple(names), ", ".join(missing) or ", ".join(positional))
    return None


def _core_tools() -> list | None:
    try:
        import toolsets
    except ImportError:
        return None
    core = getattr(toolsets, "_HERMES_CORE_TOOLS", None)
    return (
        core
        if isinstance(core, list) and all(isinstance(n, str) for n in core)
        else None
    )


def keep_declared() -> bool:
    """Make codemode a core tool so tool_search never defers it; False when the seam changed."""
    core = _core_tools()
    if core is None:
        return False
    if TOOL not in core:
        core.append(TOOL)
    return True


def declared() -> bool:
    core = _core_tools()
    return core is not None and TOOL in core


def owns(handler: object) -> bool:
    """False when the registry's codemode entry runs another handler (a context that does not
    publish to the registry has none)."""
    from tools.registry import registry

    entry = registry.get_entry(TOOL)
    return entry is None or entry.handler == handler  # a bound method: equal, not identical


def declare_deadline(handler: object, floor: object) -> bool | None:
    """Set ``deadline_floor`` on the registry entry that runs ``handler``. None when no codemode
    entry is in the registry (a context that does not publish there); False when the entry is
    another one or lacks the field (Hermes then keeps its generic deadline)."""
    try:
        from tools.registry import registry

        entry = registry.get_entry(TOOL)
        if entry is None:
            return None
        if entry.handler != handler or not hasattr(entry, "deadline_floor"):  # a bound method: equal, not identical
            return False
        entry.deadline_floor = floor
        return True
    except Exception:  # a changed registry shape leaves the generic deadline
        return False
