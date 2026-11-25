"""The eval's model (AGR-059, AGR-104): every LLM eval runs on a cheap model, never on the configured
main route. ``--model luna`` is gpt-6-luna and ``--model terra`` gpt-5.6-terra on the Codex
subscription, ``--model haiku`` and ``--model sonnet`` the Claude subscription's models, their ids
resolved as stock's ``/model haiku`` resolves it under that provider (``resolve_alias``),
``--model deepseek-flash`` deepseek-v4.1-flash on opencode-go (priced per token, so its cost is
reported, not "subscription"). The pair then replaces the configured one in ``configured_route``, so
credentials and endpoint come from stock's own rules; a route that lands on another provider is
refused, never measured."""

from __future__ import annotations

from typing import Any, Callable, Mapping

CLAUDE_SUBSCRIPTION = "claude-subscription-directsdk-experimental"
CHOICES = {
    "luna": ("openai-codex", "gpt-6-luna"),
    "haiku": (CLAUDE_SUBSCRIPTION, "haiku"),
    "sonnet": (CLAUDE_SUBSCRIPTION, "sonnet"),
    "terra": ("openai-codex", "gpt-5.6-terra"),
    "deepseek-flash": ("opencode-go", "deepseek-v4.1-flash"),
}
# the stock endpoint of each choice; an override (HERMES_CODEX_BASE_URL) is refused, and the DirectSDK
# process endpoint ties route_problem's acp_command exemption to the stock transport
_CODEX = "https://chatgpt.com/backend-api/codex"
_PROCESS = "process://" + CLAUDE_SUBSCRIPTION
ENDPOINTS = {
    "luna": _CODEX,
    "haiku": _PROCESS,
    "sonnet": _PROCESS,
    "terra": _CODEX,
    "deepseek-flash": "https://opencode.ai/zen/go/v1",
}
_ALIASED = frozenset({"haiku", "sonnet"})  # a provider alias, not a model id
_FAMILY = {"haiku": "claude-haiku", "sonnet": "claude-sonnet"}  # what an alias must resolve to
SUBSCRIPTION_PROVIDERS = frozenset({"openai-codex", CLAUDE_SUBSCRIPTION})  # opencode-go is priced per token


def choice_problem(name: str | None) -> str | None:
    """Why ``name`` cannot pick the eval's model; None for every name in CHOICES."""
    if name in CHOICES:
        return None
    return (
        "codemode eval needs --model %s (AGR-059, AGR-104: evals run only on the cheap models listed), got %s"
        % (", ".join(CHOICES), repr(name) if name else "none")
    )


def _stock_model(provider: str, model: str) -> str:
    from hermes_cli.model_switch import resolve_alias

    found = resolve_alias(model, provider)
    if found is None:  # passed on, the CLI would pick the model itself (ANTHROPIC_DEFAULT_HAIKU_MODEL)
        raise RuntimeError("the alias %s does not resolve under %s" % (model, provider))
    if found[0] != provider:
        raise RuntimeError(
            "the alias %s resolves to %s/%s, not to %s"
            % (model, found[0], found[1], provider)
        )
    return str(found[1])


def resolve(
    name: str, configured_route: Callable[..., dict[str, Any]]
) -> dict[str, Any]:
    """The route for ``name`` through ``configured_route(provider=, model=)``; raises when it is not on
    the chosen provider, model or endpoint (a user alias or stock normalisation can change the model)."""
    provider, model = CHOICES[name]
    route = configured_route(
        provider=provider,
        model=_stock_model(provider, model) if name in _ALIASED else model,
    )
    if route.get("provider") != provider:
        raise RuntimeError(
            "--model %s resolved to provider %s, not %s"
            % (name, route.get("provider"), provider)
        )
    resolved = str(route.get("model") or "")
    family = _FAMILY.get(name)
    if not resolved.lower().startswith(family) if family else resolved != model:
        raise RuntimeError("--model %s resolved to model %s" % (name, resolved))
    if str(route.get("base_url") or "").rstrip("/") != ENDPOINTS[name]:
        raise RuntimeError("--model %s resolved to an endpoint other than stock's %s" % (name, ENDPOINTS[name]))
    return route


def is_subscription(route: Mapping[str, Any] | None) -> bool:
    """True on a route with no per-token price (cost is not applicable there)."""
    return route is not None and route.get("provider") in SUBSCRIPTION_PROVIDERS
