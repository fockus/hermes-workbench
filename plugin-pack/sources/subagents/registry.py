"""Agent definitions in Claude Code format (REQ-008..011).

Lookup order, first match by name winning: project ``.hermes/agents`` → project ``.claude/agents`` →
``$HERMES_HOME/agents`` → ``~/.claude/agents`` → Memory Bank agents rendered by ``mb-agent-render.py`` →
built-in types (a project definition may shadow any of them, as in Claude Code). A malformed entry (no closed
frontmatter, no name, not a regular file, bad permission fields) is skipped with a warning and refuses its name
rather than falling through to a broader definition of it (REQ-009); unknown frontmatter fields are warned about and
ignored; an unknown name raises :class:`UnknownAgentType` carrying the closest known names.
"""

from __future__ import annotations

import difflib
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("subagents.registry")

# Claude Code tool names → Hermes tool names; any other name (a Hermes or MCP tool) passes unchanged.
CLAUDE_TOOLS = {"read": "read_file", "write": "write_file", "edit": "patch", "multiedit": "patch",
                "notebookedit": "patch", "bash": "terminal", "grep": "search_files", "glob": "search_files",
                "ls": "search_files", "task": "delegate_task", "agent": "delegate_task", "webfetch": "web_extract",
                "websearch": "web_search", "todowrite": "todo_list"}
SPAWN_TOOL = "delegate_task"
# Frontmatter keys this plugin reads or knowingly leaves to Claude Code; any other key is warned about.
KNOWN_FIELDS = frozenset({"name", "description", "tools", "disallowedTools", "model", "effort", "background",
                          "color", "permissionMode", "maxTurns", "skills", "mcpServers", "hooks", "memory",
                          "isolation", "initialPrompt", "partial"})
RENDER_TIMEOUT_S = 30


@dataclass(frozen=True)
class AgentDef:
    name: str
    description: str
    body: str                      # injected into the child's delegate_task context
    tools: frozenset[str] | None   # None = full set
    disallowed: frozenset[str]
    model: str | None              # may carry claude-code:/codex:/pi:
    effort: str | None             # route reasoning_effort; a pipeline role or the call may override
    background: bool
    may_spawn: bool                # orchestrator role allowed
    source: Path
    commands: tuple[str, ...] = ()  # terminal limited to these Claude Code Bash(...) patterns; () = unlimited
    scopes: tuple[tuple[str, ...], ...] = ()  # the Bash pattern groups of every scoped spawner above it
    refused: bool = False  # a malformed definition: it holds its name and grants nothing

    @property
    def command_groups(self) -> tuple[tuple[str, ...], ...]:
        """Every Bash pattern group a terminal command must match (one pattern of each)."""
        return tuple(group for group in (self.commands, *self.scopes) if group)


class UnknownAgentType(LookupError):
    def __init__(self, name: str, suggestions: list[str]):
        self.name, self.suggestions = name, suggestions
        hint = ("Closest known types: %s." % ", ".join(suggestions)) if suggestions else "No known type is close."
        super().__init__("Unknown agent type '%s'. %s Use one of those names or define the agent." % (name, hint))


class RefusedAgentType(UnknownAgentType):
    """The first source's definition of the name is refused: never taken from a broader source below."""

    def __init__(self, agent: "AgentDef"):
        LookupError.__init__(self, "Agent type '%s' is refused: its definition %s %s. Fix that file."
                             % (agent.name, agent.source, agent.description))
        self.name, self.suggestions = agent.name, []


def _builtin(name: str, description: str, body: str, tools: set[str] | None) -> AgentDef:
    tools_set = frozenset(tools) if tools is not None else None
    return AgentDef(name, description, body, tools_set, frozenset(), None, None, False,
                    tools_set is None or SPAWN_TOOL in tools_set, Path(__file__))


BUILTINS = {a.name: a for a in (
    _builtin("general-purpose", "General-purpose agent for multi-step research and implementation work.", "", None),
    _builtin("Explore", "Read-only agent that finds files and answers questions about the codebase.",
             "You are a read-only exploration agent. Find the relevant files, read the code and answer the question "
             "with paths and line numbers. Do not modify anything.", {"read_file", "search_files"}),
    _builtin("Plan", "Read-only agent that researches the codebase and writes an implementation plan.",
             "You are a planning agent. Research the code and produce an implementation plan: steps, files to "
             "change, risks and tests. Do not modify anything.",
             {"read_file", "search_files", "web_search", "web_extract"}),
    _builtin("verifier", "Agent that independently checks claims by reading code and running checks.",
             "You are a verifier. Check each claim you are given independently: read the code and run the tests "
             "or commands that prove or disprove it, then report every claim as verified or failed with the "
             "evidence. Do not fix anything.", {"read_file", "search_files", "terminal"}),
)}


def _split(text: str) -> tuple[str, str] | None:
    """(frontmatter, body) of a definition, line endings normalized; None without a closed frontmatter."""
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 3)
    if end == -1:
        return (text[4:-4], "") if text.endswith("\n---") else None
    return text[4:end], text[end + 5:]


def refusal(name: str, path: Path, reason: str) -> AgentDef:
    """A definition that holds its name and grants nothing."""
    return AgentDef(name, reason, "", frozenset(), frozenset(), None, None, False, False, path, refused=True)


class Malformed(ValueError):
    """Frontmatter that is not a YAML mapping, or a permission field of the wrong type."""


def _fields(front: str) -> dict[str, object]:
    """The frontmatter as YAML (the installed safe parser), a mapping or :class:`Malformed`."""
    import yaml

    try:
        fields = yaml.safe_load(front) if front.strip() else {}
    except yaml.YAMLError as exc:
        raise Malformed("frontmatter is not valid YAML: %s" % str(exc).splitlines()[0]) from exc
    if not isinstance(fields, dict):
        raise Malformed("frontmatter is not a mapping")
    return {str(k): v for k, v in fields.items()}


def _entries(value: object, key: str) -> list[tuple[str, str, str]]:
    """``(entry, Hermes tool, scope)`` per listed tool; ``Bash(git status:*)`` has the scope ``git status:*``.
    A list of names or one comma-separated string; anything else is :class:`Malformed`."""
    if isinstance(value, str):
        raw: list[object] = value.split(",")
    elif isinstance(value, list) and all(isinstance(n, str) for n in value):
        raw = list(value)
    else:
        raise Malformed("%s must be a list of tool names or a comma-separated string, not %r" % (key, value))
    found = []
    for item in (str(n).strip().strip("\"'") for n in raw):
        name, scope = (item[:-1].split("(", 1) if "(" in item and item.endswith(")") else (item, ""))
        if name.strip():
            found.append((item, CLAUDE_TOOLS.get(name.strip().lower(), name.strip()), scope.strip()))
    return found


def _tool_names(value: object) -> frozenset[str]:
    """disallowedTools: a scoped entry forbids the whole tool (stricter, never wider); omitted = none."""
    return frozenset(tool for _, tool, _ in _entries(value, "disallowedTools")) if value is not None else frozenset()


def _tools(value: object) -> tuple[frozenset[str] | None, tuple[str, ...], list[str]]:
    """tools: the allowed set (None = omitted: all; empty = none), the terminal command patterns of scoped Bash
    entries, and the scoped entries no check here can enforce. Those are withheld: a scope never widens."""
    if value is None:
        return None, (), []
    entries = _entries(value, "tools")
    whole = {tool for _, tool, scope in entries if scope in ("", "*")}
    commands = tuple(scope for _, tool, scope in entries if scope not in ("", "*") and tool == "terminal")
    withheld = [item for item, tool, scope in entries if scope not in ("", "*") and tool != "terminal"]
    if "terminal" in whole:
        commands = ()
    elif commands:
        whole.add("terminal")
    return frozenset(whole), commands, withheld


def _optional(value: object) -> str | None:
    text = str(value or "").strip()
    return None if not text or text.lower() == "inherit" else text


class Registry:
    """Agent definitions as seen from one project; built once per plugin load."""

    def __init__(self, project: Path | None, *, hermes_home: Path | None = None, home: Path | None = None,
                 skills_root: Path | None = None):
        if hermes_home is None:
            from hermes_constants import get_hermes_home

            hermes_home = get_hermes_home()
        home = home or Path.home()
        own = (Path(project) / ".hermes" / "agents", Path(project) / ".claude" / "agents") if project else ()
        self.folders = (*own, Path(hermes_home) / "agents", home / ".claude" / "agents")  # no workspace: no project
        self.skills_root = Path(skills_root or os.environ.get("MB_SKILLS_ROOT")
                                or home / ".claude" / "skills" / "memory-bank")
        self._warned: set[str] = set()
        self._rendered: dict[tuple, AgentDef | None] = {}

    def find(self, name: str) -> AgentDef:
        """The definition named ``name`` from the first source that has it."""
        found = next((f for f in (self._folder(folder).get(name) for folder in self.folders) if f is not None), None)
        found = found or self._memory_bank(name) or BUILTINS.get(name)
        if found is None:
            raise UnknownAgentType(name, difflib.get_close_matches(name, self.names(), n=3, cutoff=0.5))
        if found.refused:
            raise RefusedAgentType(found)
        return found

    def names(self) -> list[str]:
        """Every known type name, sorted."""
        names = set(BUILTINS)
        for folder in self.folders:
            names.update(self._folder(folder))
        agents = self.skills_root / "agents"
        if agents.is_dir():
            names.update(p.stem for p in agents.glob("*.md") if p.is_file() and not self._is_partial(p))
        return sorted(names)

    def _warn(self, message: str) -> None:
        if message not in self._warned:
            self._warned.add(message)
            logger.warning(message)

    def _folder(self, folder: Path) -> dict[str, AgentDef]:
        if not folder.is_dir():
            return {}
        found: dict[str, AgentDef] = {}
        for path in sorted(folder.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8") if path.is_file() else None
            except (OSError, UnicodeDecodeError):
                text = None
            if text is None:  # a directory or an unreadable entry still holds its name
                self._warn("subagents: refusing agent type %s: %s is not a readable file" % (path.stem, path))
            agent = self._parse(path, text) if text is not None else refusal(path.stem, path, "is not a readable file")
            if agent is not None:
                found.setdefault(agent.name, agent)
        return found

    def _is_partial(self, path: Path) -> bool:
        try:
            split = _split(path.read_text(encoding="utf-8", errors="replace"))
            return bool(split) and str(_fields(split[0]).get("partial", "")).lower() == "true"
        except (Malformed, OSError):
            return False

    def _memory_bank(self, name: str) -> AgentDef | None:
        src = self.skills_root / "agents" / ("%s.md" % name)
        render = self.skills_root / "scripts" / "mb-agent-render.py"
        if not (src.is_file() and render.is_file()):
            return None
        # the source and the compose partials the renderer reads beside it; a dangling link is none of them
        inputs = sorted(p for p in src.parent.glob("*.md") if p.is_file())
        key = (name, *((p, p.stat().st_mtime_ns, p.stat().st_size) for p in (render, *inputs)))
        if key not in self._rendered:
            self._rendered[key] = self._render(src, render, name)
        return self._rendered[key]

    def _render(self, src: Path, render: Path, name: str) -> AgentDef | None:
        argv = [sys.executable, "-I", str(render), str(src), "--skill-dir", str(self.skills_root), "--host", "claude"]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=RENDER_TIMEOUT_S, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            self._warn("subagents: cannot render Memory Bank agent %s: %s" % (src, exc))
            return None
        if proc.returncode != 0:
            self._warn("subagents: cannot render Memory Bank agent %s: %s" % (src, proc.stderr.strip()[:300]))
            return None
        agent = self._parse(src, proc.stdout)
        return agent if agent is not None and agent.name == name else None

    def _parse(self, path: Path, text: str) -> AgentDef | None:
        """The definition (None for a compose partial); a malformed one is a refusal of its name (the file's stem
        when no name is readable)."""
        split, name = _split(text), path.stem
        try:
            if split is None:
                raise Malformed("has no closed frontmatter")
            fields = _fields(split[0])
            name = fields["name"].strip() if isinstance(fields.get("name"), str) and fields["name"].strip() else name
            return self._definition(path, split, fields)
        except Malformed as exc:
            self._warn("subagents: refusing agent type %s from %s: %s" % (name, path, exc))
            return refusal(name, path, "is malformed (%s)" % exc)

    def _definition(self, path: Path, split: tuple[str, str], fields: dict[str, object]) -> AgentDef | None:
        if str(fields.get("partial", "")).lower() == "true":
            return None
        name = fields.get("name")
        if not isinstance(name, str) or not name.strip():
            raise Malformed("its frontmatter names no agent")
        name = name.strip()
        unknown = sorted(k for k in fields if k not in KNOWN_FIELDS)
        if unknown:
            self._warn("subagents: agent %s (%s): ignoring unknown frontmatter field(s) %s"
                       % (name, path, ", ".join(unknown)))
        # an explicit blank or null `tools:` is no tools; only an omitted field means all of them
        tools, commands, withheld = _tools(fields["tools"] if fields.get("tools") is not None else (
            "" if "tools" in fields else None))
        if withheld:
            self._warn("subagents: agent %s (%s): tool scope(s) %s cannot be enforced here; withheld (list the "
                       "tool without a scope to allow it whole)" % (name, path, ", ".join(withheld)))
        disallowed = _tool_names(fields.get("disallowedTools"))
        return AgentDef(
            name=name, description=str(fields.get("description") or ""), body=split[1].strip("\n") + "\n",
            tools=tools, disallowed=disallowed, model=_optional(fields.get("model")),
            effort=_optional(fields.get("effort")), background=str(fields.get("background", "")).lower() == "true",
            may_spawn=(tools is None or SPAWN_TOOL in tools) and SPAWN_TOOL not in disallowed, source=path,
            commands=commands)
