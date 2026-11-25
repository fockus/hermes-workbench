"""Nudge and continue of harness and subagent runs from the cron tick (design.md § Nudge and continue; Task 4).

The tick is its own short no-agent process, and both delivery paths are process-local: harness-runner's
``runner_for`` sees only the steps of this process, and stock ``delegate_task(action="steer")`` reaches only a
child registered in this process (``tools/delegate_tool_registry.py`` ``_active_subagents``). A stock ``continue``
of an idle agent is a model run, which the tick must never start (NFR-001), and so is ``Runner.resume``. So this
messenger delivers only what it reaches without starting anything: a harness step of this process (steer, or a
queued continue note). A teammate runs in its lead's process and is never sent to. Everything else raises
``Undelivered`` with the reason, and act turns it into the human question.

``Messengers(subagents=None, harness=None)``: the factories return a ``SubagentsAPI`` (read for the teammate's name
and state, so the question names why) and a ``runner_for`` lookup; the defaults take them at call time from the copies Hermes' loader made
(``hermes_plugins.<slug>``), else the source checkout, and none at all means that plugin is absent. Texts arrive masked by act.
"""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace
from typing import Any, Callable

CONTINUE = "[heartbeat] continue"


class Undelivered(Exception):
    """The run was not reached; the message is the reason the question names."""


def _loaded(slug: str, dev: str, sub: str) -> list[Any]:
    """``<plugin>.<sub>`` of every copy Hermes' loader made (``hermes_plugins.<slug>``, ``__home_<digest>`` for
    another home), else of the source checkout's ``dev`` package (tests, development); [] when none imports."""
    root = "hermes_plugins." + slug
    copies = [name for name in list(sys.modules) if sys.modules.get(name) is not None and (
        name == root or (name.startswith(root + "__home_") and "." not in name[len(root):]))]
    found = []
    for package in copies or [dev]:
        try:
            found.append(importlib.import_module("%s.%s" % (package, sub)))
        except ImportError:
            continue
    return found


def _subagents_api() -> Any:
    for api in _loaded("subagents", "subagents", "api"):
        return api.SubagentsAPI()
    raise Undelivered("hermes-subagents is not installed")


def _harness_runners() -> Any:
    copies = _loaded("harness_runner", "harnessrunner", "runner")
    if not copies:
        raise Undelivered("harness-runner is not installed")
    # each copy keeps its own registry of Runners: the step runs in whichever made it
    return SimpleNamespace(runner_for=lambda task_id: next(
        (runner for runner in (copy.runner_for(task_id) for copy in copies) if runner is not None), None))


class Messengers:
    """act's ``Messenger`` port for ``ultracode-*`` (harness) and ``team-*`` (subagent) runs."""

    def __init__(self, subagents: Callable[[], Any] | None = None, harness: Callable[[], Any] | None = None):
        self._subagents = subagents or _subagents_api
        self._harness = harness or _harness_runners

    def nudge(self, task, text: str) -> bool:
        return self._send(task, text, "steer")

    def continue_(self, task) -> bool:
        return self._send(task, CONTINUE, "queue")

    def _send(self, task, text: str, mode: str) -> bool:
        from .act import run_kind

        kind = run_kind(task)
        if kind == "harness":
            return self._to_step(task, text, mode)
        if kind == "subagent":
            return self._to_teammate(task)
        raise Undelivered("no messenger for a %s run" % kind)

    def _to_step(self, task, text: str, mode: str) -> bool:
        runner = self._harness().runner_for(task.id)
        if runner is None:  # never resume here: that runs the whole step inside the cron tick
            raise Undelivered("the harness step of %s does not run in this process; the cron tick cannot steer it"
                              % task.id)
        try:
            runner.send(task.id, text, mode)
        except ValueError as exc:  # the step ended between runner_for and send
            raise Undelivered(str(exc)) from exc
        return True

    def _to_teammate(self, task) -> bool:
        lane, _, teammate = str(task.claim_lock or "").partition(":")  # team claims are "<team id>:<teammate>"
        if lane != task.assignee or not teammate:
            raise Undelivered("no teammate holds %s" % task.id)
        lead = str(task.created_by or "")  # the lead's root conversation
        api = self._subagents()
        names = api.messenger.names
        agent = names.resolve(teammate, names.root_of(lead)) if lead else None
        if agent is None:
            raise Undelivered("no named agent %s in the conversation of team %s" % (teammate, lane))
        live = importlib.import_module(type(api).__module__.rpartition(".")[0] + ".names").LIVE  # the same copy
        if agent.state not in live:  # send would continue it: a model run inside the cron tick
            raise Undelivered("teammate %s is not running (%s); waking it would start a model in the cron tick"
                              % (teammate, agent.state))
        # never send: stock steers only children of this process, and send re-reads the state, so a teammate that
        # stops in between would get a stock continue, a model run in the tick (NFR-001)
        raise Undelivered("teammate %s runs in another process; the cron tick cannot steer it" % teammate)
