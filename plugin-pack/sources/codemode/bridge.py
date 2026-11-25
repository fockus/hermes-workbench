"""Nested call bridge: answers a script's SDK calls (spec Task 7; REQ-007, REQ-011, REQ-012, REQ-013).

A batch (the calls one future resolution waits on, in gather order) is split by Hermes's own
planner ``_plan_tool_batch_segments``: parallel segments run on at most the per-batch worker cap
(``_max_workers_for_tool_batch``: the image request limit for image_generate), sequential ones in
turn; results come back in gather order. Each worker runs in ``propagate_context_to_thread``
(ACP approval is a ContextVar, terminal approval callbacks are thread-local) and the batch inside
the compaction spill suppression, so nested results reach code-mode unshaped. Each call is
dispatched as the calling agent's loop dispatches it (nested.py: hooks, approvals, guardrails,
checkpoint, one terminal event, the outer turn and request ids).

Waits are bounded: a segment is waited on until it finishes, the script is stopped
(``stop_when``: session end, close, time limit, a Hermes interrupt), the agent's guardrails halt
the turn, or stock's per-call tool timeout passes (human approval time excluded). Then the bridge
stops waiting: calls that had not started never will, one still running is abandoned on its
daemon thread, and the script's checkout and session slot are released. A refused or skipped
call comes back as ``NotStarted``, so the script's started-call summary lists only calls that
reached their tool. The executor calls the bridge between Monty turns, so hook and approval waits
never count toward the script's time limit.
"""

from __future__ import annotations

import itertools
import json
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from .nested import NotStarted, Policy, UserRefused, outer_ids
from .repl import Call
from .sdk import STOP_LIST, parse_result
from .spill_link import suppressed

IDS = frozenset(
    {"task_id", "turn_id", "api_request_id", "user_task", "conversation_id"}
)


def refusal(name: str) -> Exception:
    """The error for an SDK name the session's SDK does not resolve."""
    if name.startswith("tools.") and name[len("tools.") :] in STOP_LIST:
        return NotStarted("%s stays a direct tool: call it directly, outside codemode" % name)
    return NotStarted("unknown SDK tool %s: see search_tools() or ALL_TOOLS" % name)


class Bridge:
    """The ``dispatch`` of one codemode call: ``bridge(calls) -> [value | exception, ...]``."""

    def __init__(self, session_id: str, grant: frozenset[str] | None = None, *, agent: Any = None,
                 deferred: frozenset[str] = frozenset(), **ids: Any):
        """``grant``: the registry tools the calling session may use (caller.py), None = all;
        ``deferred``: those it reaches only through its tool_call bridge; ``agent``: the calling
        agent (None for a host call), whose policy and outer ids nested calls keep."""
        unknown = sorted(set(ids) - IDS)
        if unknown:
            raise TypeError("Bridge got unknown call ids %s" % unknown)
        self.session_id, self.grant = session_id, grant
        self.ids = {**outer_ids(agent), **{k: v for k, v in ids.items() if v is not None}}
        self.policy = Policy(agent, deferred)
        self._prefix = "codemode-%s-" % uuid.uuid4().hex[:12]
        self._seq = itertools.count(1)
        self._stop: Callable[[], str | None] = lambda: None
        self._uncertain: str | None = None  # a call abandoned while it ran: it may still act
        self._refused: str | None = None  # a human refused a call: no later call starts (AGR-048)

    def stop_when(self, check: Callable[[], str | None]) -> None:
        """``check() -> reason | None``: once it names a reason, no further call starts."""
        self._stop = check

    def stopped(self) -> str | None:
        return self._stop() or self.policy.halted() or self._uncertain or self._refused

    def __call__(self, calls: list[Call]) -> list[Any]:
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        from agent.tool_executor import _max_workers_for_tool_batch

        results: list[Any] = [None] * len(calls)
        jobs = []
        for index, call in enumerate(calls):
            try:
                jobs.append(self._job(index, call))
            except NotStarted as exc:  # unknown, stop-listed or not granted: never dispatched
                results[index] = exc
            except Exception as exc:  # badly called (stub arguments): never dispatched
                results[index] = NotStarted(str(exc))
        with suppressed():
            for kind, segment in _plan_tool_batch_segments(jobs, execution_cwd=self._cwd()):
                if kind == "parallel":
                    workers = _max_workers_for_tool_batch([(job.index, job, job.function.name) for job in segment])
                    self._settle(segment, workers, results)
                else:
                    for job in segment:
                        self._settle([job], 1, results)
        return results

    def _settle(self, jobs: list[SimpleNamespace], workers: int, results: list[Any]) -> None:
        """Run ``jobs`` on daemon threads (at most ``workers`` at once) and wait, bounded."""
        from tools.thread_context import propagate_context_to_thread

        slots, done, cond = threading.BoundedSemaphore(max(1, workers)), {}, threading.Condition()

        def work(job: SimpleNamespace) -> None:
            job.tid = threading.get_ident()
            value: Any = None
            try:
                with slots:
                    value = job.run()
            except BaseException as exc:  # KeyboardInterrupt from an interrupt: still an outcome
                value = RuntimeError("%s was interrupted: %s" % (job.qualified, exc or type(exc).__name__))
            finally:
                _clear_interrupt(job.tid)  # a reused thread id starts clean (stock _registered_tool_worker)
                with cond:
                    done[job.index] = value
                    cond.notify_all()

        timeout = _deadline(len(jobs) == 1 and workers == 1, jobs)  # before any call starts
        for job in jobs:
            threading.Thread(target=propagate_context_to_thread(lambda job=job: work(job)),
                             name="codemode-call-%d" % job.index, daemon=True).start()
        start = time.monotonic()
        excluded = self.policy.gate.excluded_seconds if self.policy.gate else (lambda: 0.0)
        base = excluded()  # approval time of earlier segments was theirs
        with cond:
            while len(done) < len(jobs):
                waited = time.monotonic() - start - (excluded() - base)
                reason = self._stop() or self.policy.halted()
                if reason is None and timeout is not None and waited >= timeout:
                    reason = "it did not finish within Hermes's tool timeout of %gs" % timeout
                if reason:
                    for job in jobs:
                        job.cut[0] = reason  # a call still queued or awaiting approval never starts
                    _interrupt([j.tid for j in jobs if j.index not in done])  # held before dispatch too
                    break
                cond.wait(0.05)
            finished = dict(done)
        for job in jobs:
            if job.index in finished:
                results[job.index] = finished[job.index]
            elif job.began.is_set():
                results[job.index] = RuntimeError("%s was abandoned while it ran: %s" % (job.qualified, job.cut[0]))
                self._uncertain = self._uncertain or "%s may still be running" % job.qualified
            else:
                results[job.index] = NotStarted("%s was not started: %s" % (job.qualified, job.cut[0]))

    def _job(self, index: int, call: Call) -> SimpleNamespace:
        """The call as the object Hermes's planner reads (``function.name`` / ``.arguments``)."""
        tool = call.tool
        if tool is None or (tool.kind == "tool" and tool.target in STOP_LIST):
            raise refusal(call.name)
        if self.grant is not None and (tool.kind != "tool" or tool.target not in self.grant):
            raise NotStarted("%s is not available in this session" % call.name)  # commands: host calls only
        args = tool.arguments(call.args, call.kwargs)
        target = tool.target if tool.kind == "tool" else call.name  # a command is a barrier
        job = SimpleNamespace(index=index, qualified=call.name, began=threading.Event(), cut=[None], tid=None,
                              args=args, function=SimpleNamespace(name=target, arguments=json.dumps(args, default=str)))
        stopped = lambda: self.stopped() or job.cut[0]  # noqa: E731
        if tool.kind == "tool":
            def run() -> Any:
                return self._tool(call.name, tool.target, args, stopped, job.began)
        else:
            def run() -> Any:
                return self._command(call.name, tool.target, args, stopped, job.began)
        job.run = _guarded(call.name, run)
        return job

    def _tool(self, name: str, target: str, args: dict, stopped: Callable[[], str | None],
              began: threading.Event) -> Any:
        ids = {**self.ids, "session_id": self.session_id, "tool_call_id": self._prefix + str(next(self._seq))}
        value = self.policy.call(name, target, args, ids, stopped, began.set)
        if isinstance(value, UserRefused):
            self._refused = self._refused or "the user refused %s" % name
        return value

    def _command(self, name: str, target: str, args: dict, stopped: Callable[[], str | None],
                 began: threading.Event) -> Any:
        from hermes_cli import plugins
        from hermes_cli.plugin_command_dispatch import invoke_plugin_command

        handler = plugins.get_plugin_command_handler(target)
        if handler is None:
            return NotStarted("%s: the plugin command /%s is not available" % (name, target))
        reason = stopped()
        if reason:
            return NotStarted("%s was not started: %s" % (name, reason))
        began.set()
        raw = invoke_plugin_command(
            handler,
            args.get("raw_args") or "",
            source=None,
            session_id=self.session_id,
            session_key=self.session_id,
        )
        return parse_result(plugins.resolve_plugin_command_result(raw))

    def _cwd(self) -> Path | None:
        """The cwd the session's tools resolve relative paths against (as Hermes plans its batches)."""
        try:
            from tools.terminal_tool_lifecycle import get_active_env

            env = get_active_env(self.ids.get("task_id") or "default")
        except Exception:
            return None
        cwd = getattr(env, "cwd", None)
        return Path(cwd) if cwd else None


def _guarded(name: str, fn: Callable[[], Any]) -> Callable[[], Any]:
    def run() -> Any:
        try:
            return fn()
        except (
            Exception
        ) as exc:  # the script gets the failure at its await, the batch goes on
            return RuntimeError("%s failed: %s" % (name, exc))

    return run


def _interrupt(tids: list) -> None:
    """Raise Hermes's interrupt bit on abandoned call threads (stock ``_interrupt_worker_tids``), so a
    tool that polls it stops; one that never does keeps only its own daemon thread."""
    if tids:
        from agent.tool_executor import _interrupt_worker_tids

        _interrupt_worker_tids(None, [t for t in tids if t is not None])


def _clear_interrupt(tid: int | None) -> None:
    try:
        from tools.interrupt import set_interrupt

        set_interrupt(False, tid)
    except Exception:  # no interrupt module: no bit was set either
        pass


DELEGATION = "delegate_task"  # stock's one deadline-exempt tool a script can reach (manage_connections is stop-listed)
_DELEGATING: set[str] = set()  # sessions whose REPL may hold a delegate_task alias from an earlier snippet


def outer_floor(args: dict) -> float:
    """codemode's ``ToolEntry.deadline_floor``: its sandbox and nested-wait floor, or stock's longest
    safe wait once a snippet of the calling session names delegate_task (a later snippet may call a
    saved alias), so the agent never abandons its delegation batch (a retry would launch a second).
    The nested-wait allowance covers the longest registered transport timeout (``execution_timeout``)."""
    from .caller import GrantUnknown, calling_agent
    from .limits import NESTED_WAIT_SECONDS, deadline_floor

    try:
        agent = calling_agent()
    except GrantUnknown:
        agent = None
    session = getattr(agent, "session_id", None) or None
    code = args.get("code") if isinstance(args, dict) else None
    named = isinstance(code, str) and DELEGATION in code
    if named and session:
        _DELEGATING.add(session)
    if named or session in _DELEGATING:
        from agent.deadline import MAX_SAFE_TIMEOUT_S

        return MAX_SAFE_TIMEOUT_S
    from tools.registry import registry

    # every entry, not the agent's valid_tool_names: tool search defers MCP tools (the ones with a
    # transport timeout) behind tool_call; over-estimating stays bounded by sandbox time and the call cap
    wait = max((e.execution_timeout or 0.0 for e in registry.get_all_entries()), default=0.0)
    return deadline_floor(args) + max(0.0, wait - NESTED_WAIT_SECONDS)


def _deadline(sequential: bool, jobs: list[SimpleNamespace]) -> float | None:
    """Stock's bound for these calls: the generic tool timeout raised to every declared floor and, for
    a sequential call, to its registered transport timeout; none for delegate_task (stock exempts it)."""
    if sequential and jobs[0].function.name == DELEGATION:
        return None
    timeout = _call_timeout(sequential)
    from agent.tool_dispatch_helpers import _extend_tool_timeout
    from agent.tool_executor_deadline import declared_deadline_floor, deadline_with_floor

    for job in jobs:
        timeout = deadline_with_floor(timeout, declared_deadline_floor(job.function.name, job.args))
    return _extend_tool_timeout(jobs[0].function.name, timeout) if sequential else timeout


def _call_timeout(sequential: bool) -> float | None:
    """Stock's bound on one tool call (``timeouts.tools``), None when it is off or unreadable."""
    try:
        from agent import tool_executor

        resolve = (tool_executor._resolve_sequential_tool_timeout if sequential
                   else tool_executor._resolve_concurrent_tool_timeout)
        return resolve()
    except Exception:
        return None
