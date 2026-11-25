"""Typed SDK over the session's tools (spec Task 5; REQ-006, REQ-008, REQ-009, REQ-010).

Scripts see three host modules (Monty ``external_modules``): ``tools.<name>`` for built-in and plugin
tools, ``mcp.<server>.<tool>`` for the registry tool ``mcp_<server>_<tool>`` of toolset ``mcp-<server>``,
and ``commands.<name>(raw_args)`` for plugin slash commands. Every function is a typed async stub
generated from the tool's JSON schema; a result schema declared with ``register_result_schema``
becomes the return type. The prelude fed into each REPL adds ``ALL_TOOLS``, ``search_tools``,
``describe_tool``, ``retry``, ``settle`` and ``bulk``, plus ``sh`` over ``tools.terminal`` and ``edit`` over
``tools.patch`` when the session has them, as plain sandbox code, so they cost no host round trip.

Tools that stay direct (STOP_LIST, codemode/SPIKE.md § stop list) never enter the SDK.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from . import stubs
from .prelude import _HELPERS, _TOOL_HELPERS
from .tokens import estimate

# terminal, process_manage and delegate_task are in the SDK (AGR-026): nested calls keep their stock
# approvals (nested.py), and delegate_task goes through the agent's own dispatcher (agent._invoke_tool).
STOP_LIST = frozenset({
    "codemode", "execute_code", "clarify", "setup_beat",  # the tool itself, its stock twin, human interaction
    "tool_search", "tool_describe", "tool_call", "workflow",
    "todo_list", "memory", "session_search",  # agent-loop tools handle_function_call refuses
    "computer_use",  # a human gate on every action
    "browser_exec",  # arbitrary host Python, gated on the terminal surface (model_tools._rewrite_browser_exec)
    "manage_connections", "manage_catalog",  # need the agent's interactive consent callback
})
CATALOG_TOKENS = 3000
CHARS_PER_TOKEN = 4
STUB_HEADER = "from typing import Any, TypedDict\n"
RESULT_SCHEMAS: dict[str, dict] = {}


def register_result_schema(tool: str, schema: dict) -> None:
    """Declare the JSON schema of a tool's result; its stub then returns a TypedDict (REQ-010)."""
    if not isinstance(tool, str) or not isinstance(schema, dict):
        raise TypeError("register_result_schema(tool: str, schema: dict)")
    RESULT_SCHEMAS[tool] = schema


def parse_result(raw: Any) -> Any:
    """A tool result as an object: JSON text is parsed, other text and objects are kept."""
    if not isinstance(raw, (str, bytes)):
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return raw


@dataclass(frozen=True)
class SdkTool:
    qualified: str  # tools.read_file / mcp.github.list_runs / commands.mb_status
    target: str  # the registry tool name or the plugin command name
    kind: str  # "tool" or "command"
    params: tuple[stubs.Param, ...]
    signature: str  # the stub that goes into the module's .pyi
    stub: str  # the stub with its description, as describe_tool shows it
    line: str  # one catalog line

    @property
    def module(self) -> str:
        return self.qualified.rsplit(".", 1)[0]

    @property
    def function(self) -> str:
        return self.qualified.rsplit(".", 1)[1]

    def arguments(self, args: tuple, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        """The tool's own argument dict for a script call (positional and stub names mapped back)."""
        if len(args) > len(self.params):
            raise TypeError("%s takes at most %d positional arguments" % (self.qualified, len(self.params)))
        out = {p.name: value for p, value in zip(self.params, args)}
        by_py = {p.py: p.name for p in self.params}
        for key, value in kwargs.items():
            name = by_py.get(key, key)
            if name in out:
                raise TypeError("%s got multiple values for argument %r" % (self.qualified, key))
            out[name] = value
        return out


def _summary(text: str) -> str:
    first = " ".join((text or "").split())
    cut = first.find(". ")
    return (first[: cut + 1] if cut > 0 else first)[:120]


def _make(qualified: str, target: str, kind: str, params: tuple, description: str, returns: str = "Any",
          typed: str = "") -> SdkTool:
    head = stubs.function_head(qualified.rsplit(".", 1)[1], params, returns)
    short = ", ".join(p.py + ("" if p.required else "?") for p in params)
    line = "%s(%s) - %s" % (qualified, short, _summary(description))
    return SdkTool(qualified, target, kind, params, typed + head + " ...\n", typed + head + stubs.docstring(description),
                   line.rstrip(" -"))


def _place(name: str, toolset: str) -> str:
    """The qualified name of a registry tool: ``mcp.<server>.<tool>`` for MCP tools, else ``tools.<name>``.

    Stock Hermes names MCP tools ``mcp__<server>__<tool>`` (``tools.mcp_tool_schema.mcp_prefixed_tool_name``,
    every char outside ``[A-Za-z0-9_]`` of each part as ``_``); the older ``mcp_<server>_<tool>`` still reads."""
    if toolset.startswith("mcp-") and name.startswith("mcp_"):
        server = toolset[4:]
        safe = re.sub(r"[^A-Za-z0-9_]", "_", server)
        for prefix in ("mcp__%s__" % safe, "mcp_%s_" % safe, "mcp__"):
            if name.startswith(prefix):
                rest = name[len(prefix):]
                break
        else:
            rest = name[4:]
        return "mcp.%s.%s" % (stubs.identifier(server), stubs.identifier(rest))
    return "tools." + stubs.identifier(name)


def _unique(qualified: str, taken: set[str]) -> str:
    """``qualified``, or with the first free ``_<n>`` suffix when another name normalised to it."""
    name, n = qualified, 2
    while name in taken:
        name, n = "%s_%d" % (qualified, n), n + 1
    taken.add(name)
    return name


def _tool(entry: Any, result_schemas: Mapping[str, dict], qualified: str) -> SdkTool:
    schema = entry.schema if isinstance(entry.schema, dict) else {}
    returns, typed = "Any", ""
    made = stubs.result_type(stubs.pascal(qualified.rsplit(".", 1)[1]) + "Result", result_schemas.get(entry.name))
    if made:
        returns, typed = made
    description = schema.get("description") or getattr(entry, "description", "") or ""
    return _make(qualified, entry.name, "tool", stubs.params_of(schema.get("parameters")), description, returns, typed)


def _command(name: str, info: Mapping[str, Any], qualified: str) -> SdkTool:
    params = (stubs.Param("raw_args", "raw_args", "str", False),)
    return _make(qualified, name, "command", params,
                 str(info.get("description") or "Plugin command /%s." % name))


class Sdk:
    """The SDK of one session: stubs, host modules, prelude and catalog."""

    def __init__(self, tools: Iterable[SdkTool]):
        self._by_name: dict[str, SdkTool] = {}
        for tool in tools:
            self._by_name.setdefault(tool.qualified, tool)
        self.all_tools = sorted(self._by_name)

    def __eq__(self, other: object) -> bool:
        """Same tools with the same stubs: a REPL built from one serves the other."""
        return isinstance(other, Sdk) and self._by_name == other._by_name

    def resolve(self, qualified: str) -> SdkTool | None:
        return self._by_name.get(qualified)

    def _grouped(self) -> dict[str, list[SdkTool]]:
        grouped: dict[str, list[SdkTool]] = {"tools": [], "commands": []}
        for name in self.all_tools:
            tool = self._by_name[name]
            grouped.setdefault(tool.module, []).append(tool)
        return grouped

    def external_modules(self, prefix: str = "") -> dict[str, Any]:
        """``tools``, ``mcp`` (one submodule per server) and ``commands`` as Monty host modules,
        each named with ``prefix`` (a reloaded REPL, repl.py ``Repl.reload``).

        Calls never reach the host functions: the executor answers every SDK call itself."""
        from .deps import client

        pm = client()

        def host(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("code-mode answers SDK calls through its bridge")

        grouped = self._grouped()

        def module(name: str) -> Any:
            items = grouped.get(name, [])
            return pm.ExternalModule({t.function: host for t in items},
                                     stubs=STUB_HEADER + "".join(t.signature for t in items))

        servers = sorted(name.split(".", 1)[1] for name in grouped if name.startswith("mcp."))
        mcp = pm.ExternalModule({s: module("mcp." + s) for s in servers},
                                stubs="".join("from . import %s as %s\n" % (s, s) for s in servers))
        return {prefix + "tools": module("tools"), prefix + "mcp": mcp, prefix + "commands": module("commands")}

    def prelude(self, prefix: str = "") -> str:
        """Sandbox source fed once into every REPL before the first script; the host modules named
        with ``prefix`` are bound to the usual names ``tools``, ``mcp`` and ``commands``."""
        servers = sorted({t.module.split(".")[1] for t in self._by_name.values() if t.module.startswith("mcp.")})
        imports = "import {0}tools as tools, {0}mcp as mcp, {0}commands as commands\n".format(prefix) if prefix \
            else "import tools, mcp, commands\n"
        imports += "".join("import %smcp.%s\n" % (prefix, s) for s in servers)
        data = "ALL_TOOLS = %r\n_CM_STUBS = %r\n" % (self.all_tools, {n: t.stub for n, t in self._by_name.items()})
        return imports + data + _HELPERS + "".join(
            helper(tool) for qualified, target, helper in _TOOL_HELPERS
            if (tool := self.resolve(qualified)) is not None and tool.target == target and helper(tool))

    def catalog(self, usage: Mapping[str, int], budget_tokens: int = CATALOG_TOKENS,
                direct: Iterable[str] = ()) -> str:
        """Catalog lines for the tool description, in name order (stable bytes: the prompt cache keeps
        them), within ``budget_tokens``. ``usage`` is accepted and unused. ``direct`` names registry
        tools whose schemas the request already carries: they are left out; MCP tools stay."""
        skip = set(direct)
        ordered = sorted((t for t in self._by_name.values()
                          if not (t.kind == "tool" and t.target in skip and not t.module.startswith("mcp."))),
                         key=lambda t: t.qualified)
        footer = '+%d more: search_tools("words"), describe_tool("tools.name"), ALL_TOOLS'
        room = budget_tokens - estimate(footer % len(ordered)) - 2
        lines: list[str] = []
        for tool in ordered:
            room -= estimate(tool.line + "\n")
            if room < 0:
                break
            lines.append(tool.line)
        if len(lines) < len(ordered):
            lines.append(footer % (len(ordered) - len(lines)))
        return "\n".join(lines)


def build_sdk(entries: Iterable[Any], commands: Mapping[str, Mapping[str, Any]],
              result_schemas: Mapping[str, dict] | None = None) -> Sdk:
    """The SDK of registry entries (name, toolset, schema) and plugin commands, minus the stop list."""
    schemas = RESULT_SCHEMAS if result_schemas is None else result_schemas
    taken: set[str] = set()  # registry names that normalise alike get distinct, deterministic SDK names
    tools = [_tool(e, schemas, _unique(_place(e.name, e.toolset or ""), taken))
             for e in sorted(entries, key=lambda e: e.name) if e.name not in STOP_LIST]
    return Sdk(tools + [_command(name, info, _unique("commands." + stubs.identifier(name), taken))
                        for name, info in sorted(commands.items())])


def session_sdk(grant: frozenset[str] | None = None, served: bool = True,
                toolsets: tuple[Any, Any] = (None, None), defs: list[dict] | None = None) -> Sdk:
    """The SDK of the tools and plugin commands Hermes holds now; with a ``grant`` (caller.py, an
    agent-bound session), only the registry tools it names and no plugin commands: phase 1 has no
    grant model for them. A host call (no grant) keeps them. ``served``: type each tool with the
    schema Hermes serves an agent of ``toolsets`` (its enabled and disabled toolsets, served.py), or
    of the selection ``defs`` already made for this call;
    register() passes False, because inside Hermes's plugin-load worker the dynamic schema
    overrides can block on imports the loader holds until its 10 s deadline."""
    from hermes_cli.plugins import get_plugin_commands
    from tools.registry import registry

    entries = registry.get_all_entries()
    if grant is not None:
        entries = [e for e in entries if e.name in grant]
    commands = get_plugin_commands() if grant is None else {}
    from .served import definitions, served_entries

    if served and defs is None:
        defs = definitions(*toolsets)
    return build_sdk(served_entries(entries, defs) if served else entries, commands)
