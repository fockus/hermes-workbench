"""The tool definitions Hermes would serve the calling agent (used by sdk.py and caller.py; REQ-006,
hardening G6).

The schema steps of ``model_tools._compute_tool_definitions`` before tool_search assembly (toolset
selection, registry definitions, dynamic rewriters, sanitiser, hosted-scope filter), called one by
one: ``get_tool_definitions`` also rewrites the process-wide ``model_tools._last_resolved_tool_names``,
the default tool list standalone execute_code uses, and a codemode call must not change another
session's tools. One selection per codemode call gives both the SDK schemas and the deferred names
its ``tool_call`` bridge may reach.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any


def definitions(enabled: Any, disabled: Any) -> list[dict] | None:
    """The pre-assembly definitions stock selects for an agent of these toolsets; None when a seam
    changed or model_tools is not loaded."""
    tools = sys.modules.get("model_tools")
    try:
        from tools.hosted_tool_policy import hosted_tool_refusal
        from tools.kanban_toolset_context import scoped_kanban_toolset_selection
        from tools.registry import registry
        from tools.schema_sanitizer import sanitize_tool_schemas

        names = tools._select_tool_names(enabled, disabled, True)  # type: ignore[union-attr]
        with scoped_kanban_toolset_selection(enabled):
            defs = tools._apply_dynamic_schemas(registry.get_definitions(names, quiet=True))  # type: ignore[union-attr]
        return [d for d in sanitize_tool_schemas(defs) if hosted_tool_refusal(d["function"]["name"]) is None]
    except Exception:
        return None


def served_entries(entries: list[Any], defs: list[dict] | None) -> list[Any]:
    """The entries with the schema ``defs`` serves; an entry it does not serve (its check fails, or
    no selection) keeps its registry schema: calling it returns Hermes's own error."""
    if defs is None:
        return entries
    schemas = {d["function"].get("name"): d["function"] for d in defs if isinstance(d.get("function"), dict)}
    return [SimpleNamespace(name=e.name, toolset=e.toolset, schema=schemas[e.name]) if e.name in schemas else e
            for e in entries]
