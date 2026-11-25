"""No auxiliary model outside the eval route (AGR-059): both arms run on one toolset allowlist, and
``aux_problem`` refuses a run in which an enabled tool could send an auxiliary task elsewhere.

Stock has no per-agent auxiliary override (``agent/auxiliary_client.py`` ``_resolve_task_provider_model``
reads the process config), so the eval does not pin ``auxiliary.*``; it leaves out every toolset whose
tools call an auxiliary model (``AIAgent(enabled_toolsets=...)``) and checks the rest against the route.
An ``auto`` task is on the route only when it follows the live main runtime (the eval's agent publishes
its route per turn) and no fallback chain can take it elsewhere when that runtime fails.
Agent-level steps stay off in ``live.hermes_agent``: compression and micro-compaction, background review
(``skip_background_review``), memory (``skip_memory``), title generation (no ``session_db``).
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from ..modes import TOOLSET

# the twelve file tasks need read, write and search, nothing more; no terminal: under approvals.mode smart its
# classifier (tools/approval_smart.py:201) is its own call_llm, outside the send cap, on a route re-read per call
EVAL_TOOLSETS = ("file",)

# tool name (a trailing * is a prefix) -> the auxiliary task it calls; stock @ 4b4960c4ed3
AUX_TASKS_BY_TOOL = {
    "vision_analyze": "vision",  # tools/vision_tools.py:830 async_call_llm(task="vision")
    "browser_*": "vision",  # tools/browser_use_cli.py:298 screenshot; agent/vision_message_prep.py:112 off-vision
    "computer_use": "vision",  # tools/computer_use/tool.py:884 _route_capture_through_aux_vision
    "mcp_*": "mcp",  # tools/mcp_tool_sampling.py:223 call_llm(task="mcp")
    "text_to_speech": "tts_audio_tags",  # tools/tts_tool_providers.py:185 call_llm(task=tts_audio_tags)
    "kanban_*": "goal_judge",  # tools/kanban_tools.py:475 get_text_auxiliary_client("goal_judge")
    "terminal": "approval",  # tools/approval.py:1457 smart -> tools/approval_smart.py:201 call_llm(task="approval")
    "execute_code": "approval",  # tools/approval.py:1543 smart approval of the script run
    # run_agent.py:291 opens the session store, which arms agent/turn_context.py:223 maybe_auto_title
    "session_search": "title_generation",
}
SMART_ONLY = frozenset({"approval"})  # called only under approvals.mode smart
# auto -> agent/auxiliary_client.py:4459 _resolve_auto_route: the main runtime first
FOLLOWS_MAIN = frozenset({"approval", "mcp", "tts_audio_tags", "goal_judge"})
ALWAYS_OFF = frozenset({"title_generation"})  # an agent-level step the eval keeps off


def toolsets(mode: str) -> list[str]:
    """The arm's allowlist: the two arms differ only by the codemode toolset."""
    return list(EVAL_TOOLSETS) + ([] if mode == "direct" else [TOOLSET])


def _task_of(tool: str) -> str | None:
    for name, task in AUX_TASKS_BY_TOOL.items():
        if tool == name or (name.endswith("*") and tool.startswith(name[:-1])):
            return task
    return None


def _smart_approvals(config: Mapping[str, Any]) -> bool:
    """Fail closed: every mode but an explicit manual or off may call the approval classifier."""
    mode = (config.get("approvals") or {}).get("mode", "manual")
    return str(mode).strip().lower() not in ("manual", "off", "false")


def _main_fallback(config: Mapping[str, Any]) -> bool:
    from hermes_cli.fallback_config import get_fallback_chain

    return bool(get_fallback_chain(dict(config)))


def _route_problem(
    task: str, route: Mapping[str, Any], config: Mapping[str, Any]
) -> str | None:
    """Why ``task`` is not provably on ``route``, or None."""
    block = (config.get("auxiliary") or {}).get(task) or {}
    provider = str(block.get("provider") or "auto").strip()
    model = str(block.get("model") or "").strip()
    model = "" if model.lower() == "auto" else model  # stock auxiliary_client.py:6003: auto is no override
    base_url = str(block.get("base_url") or "").strip()
    if block.get("fallback_chain"):
        return "auxiliary.%s.fallback_chain can take it off the route" % task
    if task in ALWAYS_OFF:
        return "the eval keeps %s off" % task
    if provider.lower() == "auto" and not base_url:
        if task not in FOLLOWS_MAIN:
            return "auxiliary.%s is auto, which picks its own %s model" % (task, task)
        if model and model != route.get("model"):
            return "auxiliary.%s is auto with model %s" % (task, model)
        if _main_fallback(config):
            return (
                "auxiliary.%s is auto, which falls back to fallback_providers when the route fails"
                % task
            )
        return None
    if (
        provider == route.get("provider")
        and model == route.get("model")
        and not base_url
    ):
        return None
    if base_url:  # never its value: userinfo or a query can carry a credential
        return "auxiliary.%s has its own base_url" % task
    return "auxiliary.%s goes to %s/%s" % (task, provider, model or "its default model")


def aux_problem(
    route: Mapping[str, Any], names: Iterable[str], config: Mapping[str, Any]
) -> str | None:
    """Why an enabled tool of the toolsets ``names`` could call an auxiliary model off ``route``; None
    when every auxiliary task such a tool calls stays on it."""
    import toolsets as stock_toolsets

    where = "%s/%s" % (route.get("provider"), route.get("model"))
    for toolset in names:
        for tool in sorted(stock_toolsets.resolve_toolset(toolset)):
            task = _task_of(tool)
            if task is None or (task in SMART_ONLY and not _smart_approvals(config)):
                continue
            problem = _route_problem(task, route, config)
            if problem:
                return (
                    "auxiliary task %s (tool %s, toolset %s) leaves the eval route %s: %s"
                    % (task, tool, toolset, where, problem)
                )
    return None
