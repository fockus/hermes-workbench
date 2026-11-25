"""On mode, the exposure map, the prompt section and the stable codemode description (spec Task 9;
REQ-020, REQ-021, REQ-022, REQ-026).

on mode (the default) keeps every declared tool and adds codemode. The exposure map sets a tool to
direct, code, deferred or hidden; without the only-mode hook code and hidden degrade to deferred,
which Hermes does through ``tools.tool_search.defer``. Doctor prints that setting; the plugin never
writes the Hermes configuration. Every text here is a constant or built from inputs fixed at
register time, so the prompt cache keeps its bytes when tools arrive mid-session.

Pure module: no Hermes import, no file access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from . import sdk
from .limits import CEILING, DEFAULT_LIMITS
from .tokens import estimate

MODES = ("on", "only", "optin")
EXPOSURES = ("direct", "code", "deferred", "hidden")
SECTION_ID = "codemode"
TOOLSET = "codemode"  # the toolset the codemode tool registers under (Task 15)
PATCH_COMMAND = "hermes codemode patch"

IMPORTABLE = (
    "os", "sys", "json", "re", "math", "asyncio", "datetime", "collections", "itertools", "functools",
    "typing", "dataclasses", "pathlib", "random", "time", "base64", "hashlib", "csv", "tomllib",
    "decimal", "copy", "bisect", "fnmatch", "binascii",
)  # the modules Monty imports (SPIKE.md probe); hints.py keeps its own copy
CATALOG_HEADING = "\n\nTools not in your request (deferred and MCP; call the others directly):\n"

_PROMPT_HEAD = (
    "## Code mode\n"
    "The codemode tool runs Python in a sandbox whose async functions reach your other tools.\n"
    "Several independent calls go direct, all in ONE parallel request: not through codemode.\n"
    "Write code with it only for:\n"
    "- 3 or more dependent or bulk calls: each needs the one before, or one call per item of a long list;\n"
    "- loops over items, pages or files, and pagination;\n"
    "- filtering a large result, so only the answer reaches you.\n"
    "Keep a single call direct: call that tool itself, not through codemode.\n"
)
_PROMPT_BULK = (
    "Write loops of writes through bulk(items, write, verify, key): it re-reads every item "
    "and never writes one twice.\n"
)
_PROMPT_OPTIN = (
    "The codemode tool (Python scripts over your tools) exists: call it only when asked or when a "
    "script clearly beats direct calls.\n"
)

_DESCRIPTION = (
    "Run Python in a sandbox: no network or environment. Imports, only these: %s. open(), glob and "
    "os file or env access do not exist: list a folder with search_files or sh('ls'); read_file "
    "returns text with line numbers. Tools are async functions: await tools.<name>(...), await "
    "mcp.<server>.<tool>(...); tools.terminal(...) and tools.delegate_task(...) run under their usual "
    "approvals, and if the user refuses one the whole script stops: ask the user what to do next. "
    "Helpers: await sh(cmd) returns terminal output and raises on a non-zero exit (check=False returns "
    'it anyway), await edit(path, old, new) patches a file, await settle(call, ...) gives one {"ok": '
    'value} or {"error": text} per call. Run independent calls together with asyncio.gather '
    "(return_exceptions=True is not supported: use settle to keep going). The last expression comes "
    "back as JSON and print() output as logs; nothing else reaches you. Variables persist across "
    "codemode calls of this session. Limits: %ds of sandbox time, %d tool calls; a first line "
    "'# codemode: seconds=<n> calls=<n>' changes them up to %ds and %d calls. Tools not listed: "
    'search_tools("words"), describe_tool("tools.name"), ALL_TOOLS. Ask the user (clarify) directly, '
    "not from code.\n"
    "Bulk writes (verify must never raise; want is the exact read_file content after the edit, with "
    'line numbers, e.g. "1|new"):\n'
    "async def ok(it):\n"
    "    try: return \"applied\" if (await tools.read_file(it[\"path\"]))[\"content\"] == it[\"want\"] else \"not_applied\"\n"
    "    except Exception: return \"not_applied\"\n"
    "await bulk(items, lambda it, k: edit(it[\"path\"], \"old\", \"new\"), ok, lambda it: it[\"path\"])"
    % (", ".join(IMPORTABLE), DEFAULT_LIMITS.seconds, DEFAULT_LIMITS.calls, CEILING.seconds, CEILING.calls)
    + CATALOG_HEADING
)


@dataclass(frozen=True)
class Settings:
    mode: str = "on"
    exposure: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    problems: tuple[str, ...] = ()


def read_settings(get_config: Callable[[str, Any], Any]) -> Settings:
    """``mode`` and ``exposure`` from the plugin settings (``ctx.get_config``); a bad value is
    dropped and named in ``problems`` instead of failing the plugin."""
    problems: list[str] = []
    mode = get_config("mode", "on")
    if mode not in MODES:
        problems.append(
            "setting mode=%r is not one of %s; codemode runs in on mode"
            % (mode, ", ".join(MODES))
        )
        mode = "on"
    raw = get_config("exposure", {}) or {}
    exposure: dict[str, str] = {}
    if not isinstance(raw, Mapping):
        problems.append(
            "setting exposure must map tool names to %s; it was ignored"
            % "/".join(EXPOSURES)
        )
        raw = {}
    for name, value in raw.items():
        if isinstance(name, str) and value in EXPOSURES:
            exposure[name] = value
        else:
            problems.append(
                "exposure %r: %r was ignored (values: %s)"
                % (name, value, ", ".join(EXPOSURES))
            )
    return Settings(mode, MappingProxyType(exposure), tuple(problems))


def active_mode(settings: Settings, hook_present: bool = False) -> str:
    """only needs the tool-list hook of the core patch; without it codemode runs in on mode. optin
    keeps the tool registered and only drops the prompt's push."""
    if settings.mode == "only" and hook_present:
        return "only"
    return "optin" if settings.mode == "optin" else "on"


def _defer_setting(settings: Settings, current_defer: Iterable[str]) -> list[str]:
    """The Hermes defer list matching the exposure map in on mode (code and hidden act as deferred)."""
    direct = {n for n, v in settings.exposure.items() if v == "direct"}
    wanted = {n for n, v in settings.exposure.items() if v != "direct"}
    return sorted((set(current_defer) - direct) | wanted)


def doctor_lines(
    settings: Settings,
    *,
    current_defer: Iterable[str],
    hook_present: bool = False,
    declared: bool = True,
) -> list[str]:
    """Doctor's mode and exposure report. ``current_defer`` is the defer list Hermes uses now (its
    curated default when config.yaml sets none): an explicit list replaces that default wholesale,
    so the printed setting keeps it. ``declared`` is False when codemode could not be kept out of
    tool_search's deferral."""
    current = sorted(set(current_defer))
    mode = active_mode(settings, hook_present)
    head = "mode: %s" % mode
    if settings.mode == "only" and mode != "only":
        head += (
            " (only is configured, but the tool-list hook is missing: run `%s`)"
            % PATCH_COMMAND
        )
    lines = [head, *settings.problems]
    lines.append(
        "exposure: "
        + (
            ", ".join("%s=%s" % kv for kv in sorted(settings.exposure.items()))
            or "none (every tool keeps its Hermes default)"
        )
    )
    wanted = _defer_setting(settings, current)
    lines.append("tools.tool_search.defer: [%s]" % ", ".join(wanted))
    if wanted == current:
        lines.append("config.yaml already has this deferral list")
    else:
        lines.append(
            "set the line above in config.yaml yourself: codemode never writes the Hermes configuration"
        )
    if not declared:
        lines.append(
            "warning: codemode itself is deferrable by tool_search in this Hermes version; "
            "the model reaches it only through the tool_search bridge"
        )
    return lines


def prompt_section(*, bulk: bool, mode: str = "on") -> str:
    """The system prompt rule; ``bulk`` only when the SDK offers the bulk() helper. optin: one line."""
    if mode == "optin":
        return _PROMPT_OPTIN
    return _PROMPT_HEAD + (_PROMPT_BULK if bulk else "")


def tool_schema(session_sdk: sdk.Sdk, usage: Mapping[str, int], direct: Iterable[str] = ()) -> dict:
    """The codemode tool schema: fixed instructions plus the catalog of the tools held at register
    time, in name order; ``usage`` is accepted and unused (it made the bytes differ per session).
    ``direct``: registry tools whose schemas the request already carries; the catalog leaves them out.
    Built once; tools added later are reached through search_tools and the SDK."""
    budget = sdk.CATALOG_TOKENS - estimate(_DESCRIPTION) - 1
    return {
        "name": "codemode",
        "description": _DESCRIPTION + session_sdk.catalog(usage, budget_tokens=budget, direct=direct),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "The Python script to run."}
            },
            "required": ["code"],
        },
    }
