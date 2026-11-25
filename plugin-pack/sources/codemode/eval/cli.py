"""``hermes codemode eval`` (spec Task 11, REQ-029, REQ-030): run the task set live in direct and
on modes, print the per-class report and the rollout gate. Every run calls the model ``--model``
picks (luna, haiku, sonnet, terra or deepseek-flash, cheap routes only: AGR-059, AGR-104), so the
command spends subscription quota or, on deepseek-flash, a few cents of opencode-go tokens; Task 15 mounts it under ``hermes codemode``."""

from __future__ import annotations

import argparse
import functools
import json
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Mapping

from . import aux_routes, live, preflight as preflight_check, route_choice, send_bound
from .big_gate import big_gate
from .big_tasks import BIG_TASKS
from .gate import GateResult, rollout_gate
from .metrics import MODES, run_eval
from .tasks import TASK_SET, by_id

PROTOCOL_REPEATS = 3  # spec Task 11: every task three times per mode

_COLUMNS = (
    "runs",
    "accepted_rate",
    "fresh_input_tokens",
    "cached_input_tokens",
    "gross_input_tokens",
    "cost",
    "cost_per_accepted",
    "requests_per_attempt",
    "tool_errors",
    "p50_seconds",
    "p95_seconds",
    "calls",
)
TOKEN_RULE = ("gate token rule: fresh input = uncached input + cache-write tokens; cache reads are reported "
              "(cached_input_tokens) but not counted; gross_input_tokens = fresh + cached, kept as an extra column")


def setup(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model", default=None, help="required: luna, terra (Codex), haiku, sonnet (Claude subscription) or deepseek-flash (opencode-go)"
    )
    parser.add_argument(
        "--repeats", type=int, default=3, help="runs per task and mode (default 3)"
    )
    parser.add_argument(
        "--tasks",
        default="",
        help="comma-separated task ids (default: all %d)" % len(TASK_SET),
    )
    parser.add_argument(
        "--class", dest="task_class", choices=("big",), default=None,
        help="big: run the %d big tasks, judged by big_gate (default: the %d-task protocol, rollout_gate)"
        % (len(BIG_TASKS), len(TASK_SET)),
    )
    parser.add_argument(
        "--json", dest="json_path", default=None, help="also write the full report here"
    )


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    return "%.4g" % value if isinstance(value, float) else str(value)


def format_report(report: Mapping[str, Any], verdict: GateResult, gate_name: str | None = None) -> str:
    lines = ["gate applied: %s" % gate_name] if gate_name else []
    lines += [TOKEN_RULE, "class mode " + " ".join(_COLUMNS)]
    for cls, modes in sorted(report["classes"].items()):
        for mode, summary in modes.items():
            lines.append(
                "%s %s %s" % (cls, mode, " ".join(_cell(summary[c]) for c in _COLUMNS))
            )
    for cls, modes in sorted(report["classes"].items()):
        for mode, summary in modes.items():
            if summary.get("tool_errors"):
                lines.append("tool errors %s %s: %s" % (cls, mode, ", ".join(
                    "%s %d" % kv for kv in sorted(summary["tool_errors_by_tool"].items()))))
    lines.append("gate: %s" % ("passed" if verdict.passed else "not passed"))
    lines.extend("  - " + reason for reason in verdict.reasons)
    return "\n".join(lines)


def _partial(selected: list, repeats: int, report: Mapping[str, Any], full: tuple = TASK_SET,
             gate_name: str = "rollout_gate") -> str | None:
    """Why this run cannot give a rollout verdict: a task subset of ``full``, fewer than the protocol's
    repeats, or runs that are not exactly every task ``repeats`` times in every mode."""
    missing = [t.id for t in full if t not in selected]
    expected = {(t.id, mode): repeats for t in full for mode in report["modes"]}
    ran = Counter((task_id, mode) for task_id, mode, _ in report["order"])
    if missing or repeats < PROTOCOL_REPEATS or ran != expected:
        why = ("tasks left out: %s" % ", ".join(missing) if missing else "%d repeats" % repeats
               if repeats < PROTOCOL_REPEATS else "the runs are not every task %d times per mode" % repeats)
        return "partial run (%s): diagnostics only, the %s needs every task and %d repeats" % (
            why, gate_name, PROTOCOL_REPEATS)
    return None


def _aux_problem(route: Mapping[str, Any]) -> str | None:
    from hermes_cli.config import load_config

    config = load_config() or {}
    return next(filter(None, (aux_routes.aux_problem(route, aux_routes.toolsets(m), config) for m in MODES)), None)


def run(
    args: argparse.Namespace,
    agent_factory: live.AgentFactory | None = None,
    root: Path | None = None,
    preflight: Callable[[live.AgentFactory, Path], str | None] | None = None,
) -> int:
    """0 when the gate passed, 1 when it did not, 2 on bad arguments or a failed preflight. The preflight
    (one codemode script, one write_file) runs before any measured attempt, for every agent factory; a test
    that must skip it passes a stub as ``preflight``."""
    ids = [t.strip() for t in (args.tasks or "").split(",") if t.strip()]
    big = getattr(args, "task_class", None) == "big"
    pool = BIG_TASKS if big else TASK_SET  # a big task never runs under the rollout gate, nor a protocol task under big_gate
    try:
        selected = [by_id(t) for t in ids] if ids else list(pool)
        unknown = next((t for t in selected if t not in pool), None)
        if unknown is not None:
            raise KeyError(unknown.id)
    except KeyError as exc:
        print("unknown eval task %s; known: %s" % (exc.args[0], ", ".join(t.id for t in pool)))
        return 2
    repeated = sorted({t for t in ids if ids.count(t) > 1})
    if repeated:  # a repeated task would weigh twice in every metric
        print("eval task listed more than once: %s" % ", ".join(repeated))
        return 2
    if not 1 <= args.repeats <= PROTOCOL_REPEATS:  # every repeat is one paid attempt per task and mode
        print("--repeats must be at least 1 and at most %d" % PROTOCOL_REPEATS)
        return 2
    route = None
    if agent_factory is None:
        problem = route_choice.choice_problem(getattr(args, "model", None))
        if problem:  # never a silent fallback to the configured main route
            print(problem)
            return 2
        try:  # one route for every attempt, its errors before any of them
            route = route_choice.resolve(args.model, live.configured_route)
        except Exception as exc:
            print("codemode eval cannot reach the configured model: %s" % exc)
            return 1
        moa = live.moa_problem(route)
        if moa:
            print("codemode eval: %s" % moa)
            return 2
        problem = live.route_problem(route)
        if problem:
            print("codemode eval: %s" % problem)
            return 1
        agent_factory = functools.partial(live.hermes_agent, route=route)
    aux = _aux_problem(route or {})
    if aux:  # before any agent: a tool's auxiliary model off the route would spend outside it (AGR-059)
        print("codemode eval: %s" % aux)
        return 2
    if route is not None:
        print("codemode eval route (--model %s): %s" % (args.model, live.describe(route)), flush=True)
    folder = (
        Path(root)
        if root is not None
        else Path(tempfile.mkdtemp(prefix="codemode-eval-"))
    )
    runner = live.make_runner(agent_factory, folder, selected)
    per_call = send_bound.sends_per_call(route)
    sends = live.MAX_SENDS * per_call
    print("codemode eval bounds: %d tasks x %d modes x %d repeats x %d sends per attempt (%d iterations + summary "
          "and its retry%s, a hard cap) = at most %d model requests, provider retries included" % (
              len(selected), len(MODES), args.repeats, sends, live.MAX_ITERATIONS,
              ", x2 for DirectSDK's re-send after a 401" if per_call > 1 else "",
              len(selected) * len(MODES) * args.repeats * sends), flush=True)
    print("codemode eval preflight: one codemode script and one write_file before any measured attempt", flush=True)
    problem = (preflight or preflight_check.check)(agent_factory, folder)
    if problem:  # nothing was measured
        print("codemode eval: %s; no attempt was measured" % problem)
        return 2
    report = run_eval(
        [{"id": t.id, "class": t.cls} for t in selected], runner, repeats=args.repeats
    )
    subscription = route_choice.is_subscription(route)
    gate_name, judge = ("big_gate", big_gate) if big else ("rollout_gate", rollout_gate)
    verdict = judge(report, subscription=subscription)
    partial = _partial(selected, args.repeats, report, pool, gate_name)
    if partial:  # diagnostics only: a rollout verdict needs the whole protocol
        verdict = GateResult(False, (partial,) + verdict.reasons)
    print(format_report(report, verdict, gate_name))
    if subscription:
        print("cost: not applicable (subscription): the route has no per-token price; judged on tokens and quality")
    print("work folders: %s" % folder)
    if args.json_path:
        full = {
            **report,
            "gate": {"name": gate_name, "passed": verdict.passed, "partial": bool(partial), "reasons": list(verdict.reasons)},
        }
        Path(args.json_path).write_text(json.dumps(full, indent=2))
    return 0 if verdict.passed else 1
