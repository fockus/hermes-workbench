"""Call-time enforcement keyed by the caller (REQ-002..004, REQ-011, REQ-013..015; SPIKE.md §§ 1-3).

Spawned children inherit the parent's toolsets, so restrictions are checked per call in one
`pre_tool_call` hook: the main agent obeys the orchestrator mode, a typed child its own tools and
disallowedTools. The same hook puts the definition body into a typed task's `context` and
downgrades `role: orchestrator` for types that may not spawn. Callers are told apart through the
caller map (callers.py). Any session not bound is the main agent.

Only user input lifts the main agent's restriction (REQ-003, REQ-021): a turn at local interactive ingress
(``platform`` is the CLI or TUI, sender empty or a local ``uid:``/``sid:`` owner), never one stock marked synthesized,
one whose text carries a header of non-user text (``NOT_USER_MARKS``) or one holding a text this plugin injected
(a CLI watch notice or ``inject_message`` reaches ``pre_llm_call`` as a user turn). The grant is pinned to its session
and to one non-empty turn id: the turn's own, else the first one a tool call names.
"""

from __future__ import annotations

import collections
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .callers import CallerMap, Child, Spawn, active_parent, agent_ref, task_has_schema
from .calls import DENIED, Call, Calls, Slot, action_of
from .enforce_meta import BRIDGE_CALL, DISCOVERY, command_allowed, judge_bridge_call
from .enforce_script import SCRIPT_TOOL, script_run
from .grant import AGENT_MARK, NOT_USER_MARKS, affirmative, not_user_text
from .registry import SPAWN_TOOL, AgentDef, Registry, UnknownAgentType
from .workspaces import Registries

__all__ = ["AGENT_MARK", "NOT_USER_MARKS", "CallerMap", "Enforcer", "command_allowed", "parse_mode"]
logger = logging.getLogger("subagents.enforce")

MODES = ("strict", "balanced", "off")
DEFAULT_MODE = "balanced"
BODY_LIMIT = 24_000  # characters of body + caller context (SPIKE.md § 1)
READ_BUDGET = 3      # read-only calls the main agent makes itself per user turn in balanced mode
INJECTED_KEPT = 64   # texts this plugin injected into a main session, remembered so they never lift

ORCHESTRATION = frozenset({SPAWN_TOOL, "send_message", "todo_list", "memory", "clarify"})
INTERACTIVE = ("cli", "tui")  # local surfaces a person types into; never a chat, bot, webhook, cron or API turn
# the stock kanban tools (tools/kanban_tools.py); any other kanban_* name is an ordinary, unclassified tool
KANBAN = frozenset({"kanban_attach", "kanban_attach_url", "kanban_attachments", "kanban_block", "kanban_comment",
                    "kanban_complete", "kanban_create", "kanban_heartbeat", "kanban_link", "kanban_list",
                    "kanban_request_changes", "kanban_request_review", "kanban_schedule", "kanban_show",
                    "kanban_unblock"})
READ_ONLY = frozenset({"read_file", "search_files", "web_search", "web_extract", "session_search", "skills_list",
                       "skill_view", "vision_analyze"})
NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
NAME_RULE = "1-64 characters from A-Za-z0-9_.- (letters, digits, '_', '.', '-')"

DELEGATE_HINT = ("Orchestrator mode (%s): the main agent does not run %s itself. Delegate it with delegate_task "
                 "(agent_type picks a typed agent); the user can say \"do it yourself\" to lift this for one task.")
READ_HINT = ("Orchestrator mode (balanced): the main agent has used its %d read-only calls for this turn. Delegate "
             "further investigation with delegate_task.")


@dataclass
class _Turn:
    reads: int = 0
    runs: set[str] = field(default_factory=set)  # codemode runs that already used their one read this turn
    lifted: bool = False
    turn_id: str = ""  # the user turn the grant was given in (else the first tool call's); that turn only


def _allowed(agent: AgentDef, tool: str) -> bool:
    return (agent.tools is None or tool in agent.tools) and tool not in agent.disallowed


def _block(message: str) -> dict[str, str]:
    return {"action": "block", "message": message}


def _text(message: Any) -> str:
    if isinstance(message, list):
        return " ".join(str(p.get("text", "")) for p in message if isinstance(p, dict))
    return message if isinstance(message, str) else ""


def _tool_name(entry: Any) -> str | None:
    function = entry.get("function") if isinstance(entry, dict) else None
    return function.get("name") if isinstance(function, dict) else None


def parse_mode(value: Any) -> str:
    mode = "off" if value is False else str(value or DEFAULT_MODE).strip().lower()  # YAML reads bare off as False
    if mode in MODES:
        return mode
    logger.warning("subagents: unknown orchestrator_mode %r; using %s (one of %s)", value, DEFAULT_MODE,
                   ", ".join(MODES))
    return DEFAULT_MODE


class Enforcer:
    """The pre_tool_call / pre_llm_call / subagent_start handlers and the code-mode tool filter."""

    def __init__(self, registry: Registry | Registries, callers: CallerMap, mode: Any = DEFAULT_MODE,
                 unattended: Callable[[], bool] = lambda: False, calls: Calls | None = None,
                 children: Any = None, routed_here: Callable[[Any], bool] = lambda agent: True):
        self.registry = registry
        self.callers = callers
        self.calls = calls if calls is not None else Calls()
        self.children = children  # children.Children: managed children's policy across a restart
        self.routed_here = routed_here  # whether this plugin routes an agent's spawns (callers.routed_here)
        self.mode = parse_mode(mode)
        self.unattended = unattended  # a kanban worker or cron run: no orchestrator limits on its main agent
        self._turns: dict[str, _Turn] = {}
        self._injected: collections.deque[str] = collections.deque(maxlen=INJECTED_KEPT)

    def note_injected(self, text: str) -> None:
        """A text this plugin queues into a main session as a user-role turn: it is never the user."""
        if isinstance(text, str) and text.strip():
            self._injected.append(text.strip())

    def pre_llm_call(self, session_id: str = "", user_message: Any = None, conversation_history: Any = None,
                     turn_id: str = "", platform: Any = "", sender_id: Any = "", **_: Any) -> None:
        """A new user turn of the main agent: fresh read budget; the user's "do it yourself" lifts the restriction."""
        if self.caller(session_id) is None:
            text = _text(user_message)
            local = not sender_id or str(sender_id).startswith(("uid:", "sid:"))  # stock: sender_id=agent._user_id
            lifted = platform in INTERACTIVE and local and affirmative(text) and not self._not_user(text, conversation_history)
            self._turns[session_id] = _Turn(lifted=lifted, turn_id=str(turn_id or ""))

    def _not_user(self, text: str, history: Any) -> bool:
        row = history[-1] if isinstance(history, list) and history else None
        meta = row.get("display_metadata") if isinstance(row, dict) else None
        synthesized = isinstance(row, dict) and row.get("role") == "user" and bool(
            row.get("display_kind") or (isinstance(meta, dict) and meta.get("notification_category")))
        return synthesized or not_user_text(text) or any(sent in text for sent in list(self._injected))

    def subagent_start(self, parent_session_id: str = "", child_session_id: str = "",
                       child_subagent_id: str = "", child_goal: str = "", **_: Any) -> None:
        """A fresh start takes the slot its executed route matched, even over a historical child's subagent id
        (judge-A G8); else a continued or resumed child keeps its own policy (in memory, else stored); else DENIED
        when this plugin routes the parent's spawns, and stock behaviour (the parent's limits) when stock's own
        backend does. Only a fresh spawn's policy is saved: a restored or denied one never overwrites a stored record."""
        sid = child_subagent_id or ""
        slot = self.calls.start(parent_session_id, child_goal or "", sid, child_session_id)
        fresh = isinstance(slot, Slot)
        if fresh:
            spawn = slot.spawn
        elif slot is None and sid and self.callers.known(sid) is not None:
            spawn = None  # continued in this process: bind keeps its policy
        else:
            spawn = self.children.restore(sid) if slot is None and sid and self.children is not None else None
            if spawn is None and (slot is not None or self.routed_here(active_parent())):
                logger.warning("subagents: child %s of %s matches no single routed task and has no stored policy; "
                               "it runs with no tools", sid, parent_session_id)
                spawn = Spawn(DENIED, None, True)
        child = self.callers.bind(parent_session_id, child_session_id, sid, spawn)
        if fresh and self.children is not None:
            self.children.save(child)

    def caller(self, session_id: str) -> Child | None:
        """The child making this call. One the caller map never saw start (a plugin reload while it ran) is
        recognised by stock's active agent and gets its stored policy back, else DENIED; None is the main agent."""
        child = self.callers.get(session_id)
        agent = active_parent() if child is None and session_id else None
        if agent is None or str(getattr(agent, "session_id", "") or "") != session_id:
            return child
        try:
            depth = int(getattr(agent, "_delegate_depth", 0) or 0)
        except (TypeError, ValueError):
            depth = 1  # an unreadable depth is never taken for the main agent
        if depth <= 0:
            return None
        sid = str(getattr(agent, "_subagent_id", "") or "")
        spawn = self.children.restore(sid) if sid and self.children is not None else None
        if spawn is None:
            logger.warning("subagents: running child %s (session %s) has no known policy; it runs with no tools",
                           sid, session_id)
        return self.callers.bind(str(getattr(agent, "_parent_session_id", "") or ""), session_id, sid,
                                 spawn or Spawn(DENIED, None, True))

    def pre_tool_call(self, tool_name: str = "", args: Any = None, session_id: str = "", turn_id: str = "",
                      tool_call_id: str = "", **_: Any) -> dict[str, Any] | None:
        child = self.caller(session_id)
        judge = (lambda tool, a: self._child_verdict(child, tool, a)) if child else (
            lambda tool, a: self._main_verdict(session_id, tool, turn_id or "", tool_call_id or ""))
        turn = self._turns.setdefault(session_id, _Turn()) if child is None else None
        saved = (turn.reads, set(turn.runs)) if turn is not None else None
        bridge = tool_name == BRIDGE_CALL and (child is not None or self.mode == "balanced")  # strict/off: the wrapper
        verdict = judge_bridge_call(args, judge, bool(child and child.agent)) if bridge else judge(tool_name, args)
        if verdict is not None and turn is not None and bridge:
            turn.reads, turn.runs = saved  # a refused batch runs nothing: its earlier entries use no read
        if verdict is None and tool_name == SPAWN_TOOL and isinstance(args, dict):
            return self._spawn(args, session_id, turn_id, tool_call_id or "")
        return verdict

    def tool_filter(self, session_id: str = "", tools: Any = None) -> Any:
        """The tool list a session may see: a typed child's forbidden tools removed, any other list as is."""
        child = self.caller(session_id)
        if child is None or child.agent is None or not isinstance(tools, list):
            return tools
        return [t for t in tools if _tool_name(t) is None or _allowed(child.agent, _tool_name(t))]

    def _child_verdict(self, child: Child, tool: str, args: Any) -> dict[str, str] | None:
        agent = child.agent
        if agent is not None and tool == "terminal" and agent.command_groups and _allowed(agent, tool):
            command = args.get("command") if isinstance(args, dict) else None
            if not all(command_allowed(group, command) for group in agent.command_groups):
                return _block("Agent type '%s' may run only terminal commands that match each of these scopes "
                              "(no chaining, pipes or redirection): %s." % (
                                  agent.name, "; ".join(", ".join(group) for group in agent.command_groups)))
        if agent is None or _allowed(agent, tool):
            return None
        tools = ", ".join(sorted(agent.tools - agent.disallowed)) if agent.tools is not None else "all but %s" % (
            ", ".join(sorted(agent.disallowed)))
        return _block("Agent type '%s' may not call %s. Its tools: %s." % (agent.name, tool, tools))

    def _main_verdict(self, session_id: str, tool: str, turn_id: str, call_id: str = "") -> dict[str, str] | None:
        turn = self._turns.setdefault(session_id, _Turn())
        if turn.lifted and turn_id and not turn.turn_id:
            turn.turn_id = turn_id  # a grant stock gave no turn id is pinned to the first turn that uses it
        lifted = turn.lifted and bool(turn_id) and turn.turn_id == turn_id
        if self.mode == "off" or lifted or self.unattended() or tool in ORCHESTRATION or tool in KANBAN:
            return None
        if self.mode == "strict":
            return _block(DELEGATE_HINT % ("strict", tool))
        if tool in DISCOVERY:  # the Tool Search catalog: runs no tool, uses no read
            return None
        run = script_run(call_id)  # a codemode script's own call; the script run itself is judged call by call
        if tool == SCRIPT_TOOL and run is None:
            return None
        if tool not in READ_ONLY:  # edit, shell and every unclassified tool (computer use, browsers, MCP) may mutate
            return _block(DELEGATE_HINT % ("balanced", tool))
        if run is None or run not in turn.runs:  # one script's reads, however many, use one read
            turn.reads += 1
            if turn.reads > READ_BUDGET:
                return _block(READ_HINT % READ_BUDGET)
            if run:
                turn.runs.add(run)
        return None

    def _spawn(self, args: dict[str, Any], session_id: str, turn_id: str, call_id: str) -> dict[str, Any] | None:
        """Stages the call (calls.py): one slot per task of the final arguments, at its stock position."""
        if action_of(args) not in ("", "spawn"):
            return None
        tasks = args.get("tasks")
        if isinstance(tasks, str):
            try:
                tasks = json.loads(tasks)
            except ValueError:
                return None
        owner = active_parent()  # the spawning agent; subagent_start binds it to the child (callers.py)
        if not isinstance(tasks, list) or not tasks:
            if isinstance(args.get("goal"), str):
                task = {"goal": args["goal"], "context": args.get("context")}
                spawn = Spawn(None, None, task_has_schema(args, 0), agent_ref(owner))
                self.calls.stage(Call(session_id, turn_id, call_id, [Slot(0, task, spawn)]))
            return None
        bad = [t.get("name") for t in tasks if isinstance(t, dict) and t.get("name") is not None
               and not (isinstance(t.get("name"), str) and NAME_RE.fullmatch(t["name"]))]
        if bad:
            return _block("Not started: agent name %r is invalid; a name is %s." % (bad[0], NAME_RULE))
        prepared, slots = [], []
        for i, task in enumerate(tasks):
            agent = self._lookup(task.get("agent_type")) if isinstance(task, dict) else None
            final = self._prepare(task, agent)
            prepared.append(final)
            if isinstance(final, dict):
                name = final.get("name") if isinstance(final.get("name"), str) else None
                schema = task_has_schema({**args, "tasks": tasks}, i)
                slots.append(Slot(i, final, Spawn(agent, name, schema, agent_ref(owner))))
        self.calls.stage(Call(session_id, turn_id, call_id, slots))
        return {"action": "modify", "args": {"tasks": prepared}} if prepared != args.get("tasks") else None

    def _prepare(self, task: Any, agent: AgentDef | None) -> Any:
        if not isinstance(task, dict) or agent is None:
            return task
        prepared = dict(task)
        if agent.body.strip():
            prepared["context"] = self._with_body(agent, task.get("context"))
        if not agent.may_spawn and prepared.get("role") == "orchestrator":
            prepared["role"] = "leaf"
        return prepared

    def _lookup(self, agent_type: Any) -> AgentDef | None:
        """The definition, or None for an untyped task or an unknown type (route_task refuses a typed task whose
        slot holds no definition, provider.py)."""
        if not isinstance(agent_type, str) or not agent_type.strip():
            return None
        try:
            return self.registry.find(agent_type.strip())
        except UnknownAgentType:
            return None

    @staticmethod
    def _with_body(agent: AgentDef, context: Any) -> str:
        text = agent.body.strip("\n")
        if isinstance(context, str) and context.strip():
            text = "%s\n\n%s" % (text, context)
        if len(text) > BODY_LIMIT:
            logger.warning("subagents: agent type %s: definition body plus context is %d characters; clipped to %d",
                           agent.name, len(text), BODY_LIMIT)
            text = text[:BODY_LIMIT]
        return text

