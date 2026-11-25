"""Live eval runs through a Hermes agent, the eval's model boundary (spec Task 11, REQ-029).

Each run gets a fresh work folder with the task's fixture files and a fresh agent. direct runs
disable the codemode toolset and carry a session id starting with DIRECT_PREFIX, so the plugin can
leave its prompt section out of the baseline (``direct_baseline``). Usage comes from the agent's
session counters: fresh input = uncached input + cache writes, cached input = cache reads; calls
are the tool calls the model issued; cost is None (unknown) when any request of the attempt had no
price (Hermes overwrites ``session_cost_status`` per request, so every write is watched); a model
call the counters do not cover (``receipts.unaccounted``) leaves the attempt's tokens and cost
incomplete (``usage_missing``, cost None). A crashed
run, a turn stock returns as failed or partial, or an agent that did not start counts as a rejected
attempt with the usage it had spent and its error; every agent is closed and its session finalized.
The route is resolved once, with the CLI's own model rules, before any attempt (``configured_route``).
A direct attempt stays the direct baseline when stock compaction gives it a new session id, and every
session id an attempt had is finalized once.
"""

from __future__ import annotations

import itertools
import logging
import os
import time
import uuid
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..modes import TOOLSET
from . import aux_routes, evidence, exempt, send_bound, tally
from . import receipts as rc
from .metrics import Runner
from .route_choice import CLAUDE_SUBSCRIPTION
from .tasks import TASK_SET, EvalTask

logger = logging.getLogger(__name__)

DIRECT_PREFIX = "codemode-eval-direct-"
MAX_ITERATIONS = 40
MAX_SENDS = MAX_ITERATIONS + 2  # stock handle_max_iterations: a summary request and one retry
RUN_BUDGET_SECONDS = 900.0
AgentFactory = Callable[
    [str, str, str], Any
]  # (mode, session_id, cwd) -> agent with run_conversation


_direct_attempts: list[Any] = []  # agents of the direct attempts now running


def direct_baseline(session_id: str | None) -> bool:
    """True for the session of a direct eval run, which must not see the code-mode prompt rule: its
    own id, or the id stock compaction rotated a running direct attempt to."""
    sid = str(session_id or "")
    return sid.startswith(DIRECT_PREFIX) or any(
        sid and getattr(agent, "session_id", None) == sid for agent in _direct_attempts)


def configured_route(provider: str | None = None, model: str | None = None) -> dict[str, Any]:
    """The model and transport the Hermes CLI would use: ``model.default`` / ``model.provider``
    through ``resolve_runtime_provider`` (as its background agents get them), the runtime's own
    model when the config names none, with stock startup normalisation (hardening G5b): aliases and
    provider-qualified names through ``resolve_startup_model_route`` first, the provider's native id
    and the api_mode it implies through the CLI's ``_normalize_model_for_provider`` last. Raises when
    no model resolves: an empty model would reach the provider on every attempt. ``provider`` and
    ``model`` replace the configured pair as an explicit ``--provider``/``--model`` would (AGR-059)."""
    from hermes_cli.config import load_config
    from hermes_cli.model_switch import resolve_startup_model_route
    from hermes_cli.runtime_provider import resolve_runtime_provider

    config = load_config() or {}
    model_cfg = config.get("model") or {}
    if isinstance(model_cfg, str):
        model_cfg = {"default": model_cfg}
    if provider:
        model_cfg = {"default": model or "", "provider": provider}
    configured = model = str(model_cfg.get("default") or model_cfg.get("model") or "").strip()
    # the CLI's provider order: model.provider, then HERMES_INFERENCE_PROVIDER (cli_init_mixin)
    requested, explicit = model_cfg.get("provider") or os.getenv("HERMES_INFERENCE_PROVIDER") or None, {}
    startup = resolve_startup_model_route(
        model, explicit_provider=provider or "", current_provider=requested or "", user_providers=config.get("providers"),
        custom_providers=config.get("custom_providers")) if model else None
    if startup is not None:  # the CLI's startup: the alias's or qualifier's provider wins over the config's
        model, requested = startup.model, startup.provider or requested
        explicit = {k: v for k, v in (("explicit_base_url", startup.base_url), ("explicit_api_key", startup.api_key))
                    if v}
    runtime = resolve_runtime_provider(requested=requested, target_model=model or None, **explicit)
    provider = runtime.get("provider")
    routed = str(runtime.get("model") or "").strip()
    # the CLI's rules (cli_agent_setup_mixin): a route's own model wins over none or a provider name,
    # then the provider's catalog default
    if routed and (not model or model in (provider, runtime.get("name"), model_cfg.get("provider"))):
        model = routed
    if not model and provider:
        from hermes_cli.models import get_default_model_for_provider

        model = str(get_default_model_for_provider(provider) or "").strip()
    if not model:
        raise RuntimeError("no model is configured for the eval: set one with `hermes model`")
    route = {"model": model, "acp_command": runtime.get("command"), "acp_args": runtime.get("args"),
             **{k: runtime.get(k) for k in ("api_key", "base_url", "provider", "api_mode")}}
    return _normalized(route, model_is_default=not configured)


def _normalized(route: dict[str, Any], model_is_default: bool) -> dict[str, Any]:
    """``route`` with the model and api_mode the CLI's own startup normalisation gives it (stock's
    mixin method run on the route; a missing method raises, so the eval refuses to start)."""
    from types import SimpleNamespace

    from hermes_cli.cli_model_switch_mixin import CLIModelSwitchMixin

    cli = SimpleNamespace(model=route["model"], api_mode=route.get("api_mode"), api_key=route.get("api_key"),
                          base_url=route.get("base_url"), _model_is_default=model_is_default,
                          _console_print=lambda *a, **k: None)
    CLIModelSwitchMixin._normalize_model_for_provider(cli, str(route.get("provider") or ""))
    return {**route, "model": cli.model, "api_mode": cli.api_mode}


def route_problem(route: Mapping[str, Any]) -> str | None:
    """Why the eval cannot measure on ``route``: a transport that runs the turn in its own tool
    runtime (a Codex app-server, an ACP command or an ``acp://`` / ``acp+tcp://`` agent) never shows
    the model Hermes's tools; an empty endpoint is refused as the CLI refuses it."""
    base_url = str(route.get("base_url") or "")
    scheme = urlsplit(base_url).scheme.lower()
    own_runtime = route.get("acp_command") and route.get("provider") != CLAUDE_SUBSCRIPTION  # DirectSDK: Hermes's tools
    if route.get("api_mode") == "codex_app_server" or own_runtime or scheme.split("+")[0] == "acp":
        return ("the route %s/%s runs its own tool runtime, so codemode is not visible to the model: both arms "
                "would measure the same thing" % (route.get("provider"), route.get("model")))
    if not base_url:
        return "the route %s/%s has no endpoint (base_url)" % (route.get("provider"), route.get("model"))
    return None


def moa_problem(route: Mapping[str, Any]) -> str | None:
    """MoA is refused: its advisors' usage is not verified per request, so a measurement could
    leave out an advisor that reported none."""
    if str(route.get("provider") or "").lower() == "moa" or str(route.get("model") or "").lower().startswith("moa:"):
        return "the route %s/%s is a MoA preset; the eval does not measure MoA" % (route.get("provider"),
                                                                                  route.get("model"))
    return None


def describe(route: Mapping[str, Any]) -> str:
    """The route every attempt will use: the endpoint as scheme, host and port only (no userinfo,
    path or query, which can carry a credential), and the eval's agent settings."""
    parts = urlsplit(str(route.get("base_url") or ""))
    host = parts.hostname or ""
    host = "[%s]" % host if ":" in host else host
    try:
        port = ":%d" % parts.port if parts.port else ""
    except ValueError:  # a malformed port is not shown
        port = ""
    return "provider %s, model %s, endpoint %s://%s%s, compaction off, toolsets %s (+%s in on), auxiliary: none " \
        "outside %s/%s" % (route.get("provider"), route.get("model"), parts.scheme, host, port,
                           ", ".join(aux_routes.EVAL_TOOLSETS), TOOLSET, route.get("provider"), route.get("model"))


def hermes_agent(mode: str, session_id: str, cwd: str, route: Mapping[str, Any] | None = None) -> Any:
    """A Hermes agent on ``route`` (default: the configured one); direct mode runs without the
    codemode toolset."""
    from run_agent import AIAgent

    route = configured_route() if route is None else route
    agent = AIAgent(
        **route,
        session_id=session_id,
        cwd=cwd,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        skip_background_review=True,
        # one allowlist without an auxiliary caller off the route, the arms apart only by codemode (AGR-059)
        enabled_toolsets=aux_routes.toolsets(mode),
        # delegation off in both arms: a child agent's calls would be counted only after they are spent
        disabled_toolsets=[TOOLSET, "delegation"] if mode == "direct" else ["delegation"],
        fallback_model=None,  # no fallback chain: every request stays on the eval's route
        max_iterations=MAX_ITERATIONS,
        run_budget_seconds=RUN_BUDGET_SECONDS,
    )
    # every automatic compaction off, overflow recovery included: its summary would go to
    # auxiliary.compression's model, which is not the eval's luna or haiku (AGR-059)
    agent.compression_enabled = False
    # compression.prepare_ahead off: a background summary worker would outlive its attempt and be
    # charged to none or the next one (stock: None leaves compaction inline)
    compressor = getattr(agent, "context_compressor", None)
    if compressor is not None and hasattr(compressor, "prepared_compaction"):
        compressor.prepared_compaction = None
    if compressor is not None:  # compression.micro_compact is not gated by compression_enabled (turn_finalizer)
        compressor._micro_compact_enabled = False
    # provider retries count: the printed bound is a hard cap
    return send_bound.cap(agent, MAX_SENDS, send_bound.sends_per_call(route))


def _usage(agent: Any, name: str) -> float:
    return getattr(agent, name, 0) or 0


def _cost(agent: Any, prices: list[Any] | None) -> float | None:
    """The run's cost, None when any of its requests had no price (Hermes counts those as 0.0)."""
    if agent is None or prices is None or "unknown" in prices or (
            not prices and getattr(agent, "session_cost_status", None) == "unknown"):
        return None
    return float(_usage(agent, "session_estimated_cost_usd"))


def _close(agent: Any, sessions: Sequence[str]) -> None:
    """Close the agent and finalize each session it had once (session-end hooks: codemode's REPL,
    for one): its first id and every id stock compaction rotated it to."""
    current = getattr(agent, "session_id", None)
    sessions = list(dict.fromkeys([*sessions, *([current] if isinstance(current, str) and current else [])]))
    try:
        if callable(getattr(agent, "close", None)):
            agent.close()
    except Exception as exc:  # a leak is logged, the report goes on
        logger.warning("codemode eval: closing an agent failed: %s", exc)
    for sid in sessions:
        try:
            from hermes_cli.lifecycle import finalize_session

            finalize_session(session_id=sid, platform="cli", reason="session_boundary")
        except Exception as exc:  # a leak is logged, the report goes on
            logger.warning("codemode eval: finalizing session %s failed: %s", sid, exc)


def make_runner(
    agent_factory: AgentFactory, root: Path, task_set: Sequence[EvalTask] = TASK_SET
) -> Runner:
    """``runner(task, mode)`` for run_eval: one fresh agent per run in its own work folder under ``root``."""
    known = {t.id: t for t in task_set}
    serial = itertools.count()

    def runner(task: Mapping[str, Any], mode: str) -> dict[str, Any]:
        spec = known[task["id"]]
        workdir = Path(root) / ("%s-%s-%d" % (spec.id, mode, next(serial)))
        workdir.mkdir(parents=True)
        spec.setup(workdir)
        prefix = DIRECT_PREFIX if mode == "direct" else "codemode-eval-%s-" % mode
        agent, error, accepted, seen, answer = None, None, False, None, ""
        messages: list[dict[str, Any]] = []
        session_id, start = prefix + uuid.uuid4().hex[:12], time.monotonic()
        events = tally.ToolEvents([session_id])
        try:
            agent = agent_factory(mode, session_id, str(workdir))
            exempt.mark(agent)  # AGR-095: this session only, never the user's orchestrator setting
            if mode == "direct":
                _direct_attempts.append(agent)
            seen = rc.watch(agent, session_id)
            events = tally.ToolEvents(seen.sessions)
            with rc.side_calls(seen), tally.watching(events):
                out = agent.run_conversation(spec.prompt(workdir)) or {}
            messages = out.get("messages") or []
            error = evidence.turn_failure(out)
            answer = str(out.get("final_response") or "")
            accepted = error is None and bool(spec.check(answer, workdir))
        except Exception as exc:  # the agent, model or provider failed: a rejected attempt, not an eval crash
            error = "%s: %s" % ("the agent did not start" if agent is None else "the run failed", exc)
        finally:
            if agent in _direct_attempts:
                _direct_attempts.remove(agent)
            _close(agent, seen.sessions if seen is not None else [session_id])
        if error:
            logger.warning("codemode eval: task %s in %s mode: %s", spec.id, mode, error)
        record: dict[str, Any] = {
            "accepted": accepted,
            "fresh_tokens": int(
                _usage(agent, "session_input_tokens")
                + _usage(agent, "session_cache_write_tokens")
            ),
            "cached_tokens": int(_usage(agent, "session_cache_read_tokens")),
            "cost": _cost(agent, seen.prices if seen is not None and seen.watched else None),
            "seconds": time.monotonic() - start,
            "calls": evidence.model_calls(messages),
        }
        # requests: every send at the transport boundary (a timeout and its retry are two), else the answered ones
        for key, value in (("requests", max(send_bound.sent(agent), int(_usage(agent, "session_api_calls")))),
                           ("tool_errors", tally.tool_errors(messages, events.failed))):
            if value:
                record[key] = value
        missing = rc.unaccounted(agent, seen) if seen is not None else 0
        if missing:  # tokens and cost undercount the attempt: never a measured value
            record.update(usage_missing=missing, cost=None)
        record["evidence"] = evidence.build(messages, events.failed, record, answer, accepted, error)
        return record if error is None else {**record, "error": error}

    return runner
