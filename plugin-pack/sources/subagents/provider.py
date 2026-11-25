"""The `subagents` DelegationProvider (REQ-010, REQ-012): delegate_task gains `agent_type` and `name`
(plus the tier/call overrides `effort` and `model_alias`), and each task's model, provider and
reasoning effort are resolved from the call, else the agent type, else the `delegation` section.

Hermes uses it only while `delegation.backend` is `subagents` (`agent/delegation_registry.py:35`).
`agent_type` is a free string checked here, so new definitions never change the cached schema; the
definition body and the role are not route keys (enforce.py injects and downgrades them).
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from agent.delegation_provider import ROUTE_KEYS, DelegationProvider, DelegationRouteError
from hermes_constants import VALID_REASONING_EFFORTS

from .registry import Registry, UnknownAgentType
from .workspaces import Registries

# Short model names → (provider, model); `delegation.backends.subagents.aliases` may remap any of them.
ALIASES: dict[str, tuple[str, str]] = {
    "opus": ("claude-subscription-directsdk", "opus"),
    "sonnet": ("claude-subscription-directsdk", "sonnet"),
    "haiku": ("claude-subscription-directsdk", "haiku"),
    "gpt": ("openai-codex", "gpt-6-sol"),
    "kimi": ("opencode-go", "kimi-k3"),
    "glm": ("opencode-go", "glm-5.3"),
}
# Agent models on these prefixes run through harness-runner (Task 12), never as delegate_task children.
HARNESS_PREFIXES = ("claude-code:", "codex:", "pi:")
# Endpoint keys of the `delegation` section that belong to its own provider, not to an aliased one.
ENDPOINT_KEYS = ("base_url", "api_key", "api_mode")
# what belongs to one provider's endpoint: never carried to the provider an alias switches to (stock merges
# request_overrides into the outbound API call, and validates service_tier per provider)
PROVIDER_BOUND = (*ENDPOINT_KEYS, "request_overrides", "service_tier")

PARAMETERS: dict[str, dict[str, Any]] = {
    "agent_type": {"type": "string", "description": (
        "Agent type this task runs as: a definition from .hermes/agents, .claude/agents, ~/.hermes/agents, "
        "~/.claude/agents or the Memory Bank, or a built-in (general-purpose, Explore, Plan, verifier). "
        "Its instructions, model and tool limits apply. Omit for a plain child.")},
    "name": {"type": "string", "description": (
        "Optional short unique name for this agent, used to message or continue it later.")},
    "effort": {"type": "string", "enum": list(VALID_REASONING_EFFORTS), "description": (
        "Reasoning effort for this task (a task tier preset); overrides the agent type's effort.")},
    "model_alias": {"type": "string", "description": (
        "Model for this task, overriding the agent type's: opus, sonnet, haiku, gpt, kimi, glm, or a model id "
        "on the current provider.")},
}


class SubagentsProvider(DelegationProvider):
    """Routes delegate_task tasks by agent type; definitions come from ``registry``."""

    name = "subagents"

    def __init__(self, registry: Registry | Registries,
                 admit: Callable[[Mapping[str, Any], dict[str, Any]], None] | None = None,
                 staged: Callable[[Mapping[str, Any]], Any] = lambda task: None):
        self.registry = registry
        # the staged slot (calls.Slot) of the task stock is routing now, None when it matches none: its definition
        # snapshot is the only one routed and enforced for it (a resume has no slot and reads the registry)
        self.staged = staged
        self.admit = admit  # the limiter's admission of the task stock is routing now (launch.Launcher.admit)

    @property
    def summary(self) -> str:
        return "Typed agents (Claude Code format) with per-type model, effort and tools"

    def task_parameters(self, config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        return {name: dict(schema) for name, schema in PARAMETERS.items()}

    def route_task(self, task: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
        if config.get("profile"):
            # stock runs a profile route under that profile's plugins (tools/delegate_tool_routes.py), where this
            # plugin's identity binding and enforcement may be absent: phase 1 refuses every profile route
            raise DelegationRouteError(
                "This task cannot run on the delegation profile '%s': phase 1 of the subagents backend cannot enforce "
                "agent policy inside another profile. Remove delegation.profile to delegate." % config["profile"])
        agent_type, agent = task.get("agent_type"), None
        if agent_type is not None:
            if not isinstance(agent_type, str) or not agent_type.strip():
                raise DelegationRouteError("agent_type must be the name of an agent type.")
            slot = self.staged(task)
            agent = slot.spawn.agent if slot is not None else None
            if agent is None:
                try:
                    found = self.registry.find(agent_type.strip())
                except UnknownAgentType as exc:
                    raise DelegationRouteError(str(exc)) from exc
                if slot is not None:  # unresolved when the call was staged: never routed as an untyped child
                    raise DelegationRouteError("Agent type '%s' was not known when the call was made; spawn it "
                                               "again." % agent_type.strip())
                agent = found
        route = {k: v for k, v in dict(config).items() if k in ROUTE_KEYS and v not in (None, "")}
        model = task.get("model_alias") or (agent.model if agent else None)
        if model:
            route = self._with_model(route, str(model).strip(), config, agent_type)
        effort = task.get("effort") or (agent.effort if agent else None)
        if effort:
            route["reasoning_effort"] = effort
        if self.admit is not None:
            self.admit(task, route)  # may raise DelegationRouteError: queued or over the root task's cap
        return route

    def _with_model(self, route: dict[str, Any], model: str, config: Mapping[str, Any],
                    agent_type: Any) -> dict[str, Any]:
        if model.lower().startswith(HARNESS_PREFIXES):
            raise DelegationRouteError(
                "Agent type '%s' runs on the %s harness (model %s), which delegate_task cannot start; spawn it "
                "through the subagents API instead." % (agent_type, model.split(":", 1)[0], model))
        aliased = self._aliases(config).get(model.lower())
        if aliased is None:
            return {**route, "model": model}
        provider, aliased_model = aliased
        if route.get("provider") == provider:  # the configured provider itself: its endpoint settings still apply
            return {**route, "model": aliased_model}
        kept = {k: v for k, v in route.items() if k not in PROVIDER_BOUND}
        return {**kept, "provider": provider, "model": aliased_model}

    def _aliases(self, config: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
        return alias_table(self.settings(config).get("aliases"))


def alias_table(own: Any) -> dict[str, tuple[str, str]]:
    """The built-in aliases with the remaps of ``delegation.backends.subagents.aliases`` applied."""
    aliases = dict(ALIASES)
    for alias, target in (own.items() if isinstance(own, Mapping) else ()):
        if isinstance(target, Mapping) and target.get("provider") and target.get("model"):
            aliases[str(alias).lower()] = (str(target["provider"]), str(target["model"]))
    return aliases
