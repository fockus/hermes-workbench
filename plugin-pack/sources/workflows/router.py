"""The pattern router (workflow-engine Task 14, REQ-012, AGR-059, AGR-100): a request with only a ``task`` runs the
committed ``route`` workflow, whose classifier step (the agent labelled ``route``) runs on a small model.

``ROUTER_MODEL`` is the default classifier model, the logical alias ``router``; the ``router_model`` plugin setting
overrides it. A ``delegation.backends.subagents.aliases`` entry maps one alias to one provider and model, so the
alias ``router`` has one variant per provider (``VARIANTS``, the user's config defines them, see plugin.yaml) and
``RouterHost`` tries them in order: a variant whose alias is configured and whose provider is installed, the first
that answers wins, a failed one falls to the next. Any other ``model`` value is a pinned name, passed as given to
``SubagentsAPI.spawn(model=...)``. ``RouterHost(inner, model, available=None)`` wraps an ``AgentHost`` and only
touches the classifier spawn; every other call, and a classifier request that names its own model, goes through
unchanged. Harness models (``claude-code:``, ``codex:``, ``pi:``) need nothing here.
"""

from __future__ import annotations

from typing import Any, Callable

ROUTER_MODEL = "router"
VARIANTS = (("router-codex", "openai-codex"), ("router-claude", "claude-subscription-directsdk"),
            ("router-opencode", "opencode-go"))
CLASSIFIER_LABEL = "route"
OPTIONAL_ARGS = ("max_iterations", "threshold", "criteria", "gate")


def variant_available(alias: str, provider: str) -> bool:
    """The alias is in ``delegation.backends.subagents.aliases`` and the provider is installed; a config that cannot
    be read counts every variant as available (the spawn then reports what is missing)."""
    try:
        from hermes_cli.config import get_config_path, read_user_config_raw

        delegation = read_user_config_raw(get_config_path()).get("delegation")
    except Exception:
        return True
    aliases = (((delegation if isinstance(delegation, dict) else {}).get("backends") or {}).get("subagents") or {})
    if alias not in (aliases.get("aliases") or {}):
        return False
    import providers

    return providers.get_provider_profile(provider) is not None


class RouterHost:
    def __init__(self, inner: Any, model: str = ROUTER_MODEL, available: Callable[[str, str], bool] | None = None):
        self._inner, self._model, self._available = inner, model, available

    def spawn(self, req: dict) -> dict:
        if req.get("label") != CLASSIFIER_LABEL or req.get("model"):
            return self._inner.spawn(req)
        if self._model != ROUTER_MODEL:
            return self._inner.spawn({**req, "model": self._model})
        return self._chain(req)

    def _chain(self, req: dict) -> dict:
        available = self._available or variant_available
        tried = [alias for alias, provider in VARIANTS if available(alias, provider)]
        if not tried:
            return {"ok": False, "error": "no router variant is configured: add %s to delegation.backends.subagents."
                                          "aliases (plugin.yaml)" % ", ".join(alias for alias, _ in VARIANTS)}
        for alias in tried:
            failure = self._inner.spawn({**req, "model": alias})
            if failure.get("ok") or failure.get("skipped"):
                return failure
        return {**failure, "error": "%s (router variants tried: %s)" % (failure.get("error", "failed"), ", ".join(tried))}

    def resume(self, agent_id: str, prompt: str) -> dict:
        return self._inner.resume(agent_id, prompt)

    def abort(self, agent_id: str) -> None:
        return self._inner.abort(agent_id)

    def gate(self, command: str, cwd: str | None = None) -> dict:
        return self._inner.gate(command, cwd)

    def load(self, ref: Any) -> dict:
        return self._inner.load(ref)


def route_request(task: str, args: Any = None) -> dict:
    """The ``route`` workflow call for a request without a named pattern: the task plus the optional keys given."""
    given = args if isinstance(args, dict) else {}
    return {"name": "route", "args": {"task": task, **{k: given[k] for k in OPTIONAL_ARGS if given.get(k) is not None}}}
