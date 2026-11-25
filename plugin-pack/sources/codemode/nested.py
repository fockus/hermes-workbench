"""One nested tool call as the calling agent's own loop makes it (used by bridge.py).

An agent-bound call runs stock ``agent.tool_executor._run_agent_tool_execution_middleware``, the
path the agent's concurrent executor takes for each model tool call: request middleware, the scope
block (here: the stock ``validate_deferred_call_args`` probe for a tool the agent reaches only
through ``tool_call``), plugin ``pre_tool_call`` hooks behind one authorization gate per codemode
call (gathered calls prompt one at a time), the pruned-argument check, the agent's guardrails, then
the checkpoint and progress preflight on the final arguments and one ``agent._invoke_tool``.
Blocked calls get stock's one terminal ``post_tool_call``; a dispatched call gets the one
``handle_function_call`` fires (the agent suppresses it around the outer codemode handler, so it is
re-enabled here, the stock connector-batch pattern). The result is classified with stock
``_detect_tool_failure`` and observed with the agent's ``_append_guardrail_observation``, so a
hard stop halts the outer turn as a direct call would.

The final dispatch boundary (``begin``) re-checks the bridge's stop: a call whose session ended
while its approval was pending is never started. A host call (no agent: the CLI, tests) goes to
``handle_function_call``, which runs its hooks and middleware itself.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Callable

from .sdk import parse_result


class NotStarted(RuntimeError):
    """A nested call that was refused or skipped: it never reached its tool."""


class UserRefused(NotStarted):
    """A nested call a human refused at its approval prompt: the whole script stops (AGR-048)."""


# Stock wording of a human "no" (tools/approval.py gate templates, acp_adapter/edit_approval.py). A
# timeout, a missing human, a deny rule or a hook block stays a plain refusal the script may handle.
_BY_USER = ("User denied", "denied by user", "denied by the user", "denied by ACP client")


def refused(name: str, reason: Any) -> NotStarted:
    """The script's exception for a call that was refused before it ran."""
    message = "%s refused: %s" % (name, reason)
    return UserRefused(message) if any(m in str(reason) for m in _BY_USER) else NotStarted(message)


class Answered:
    """The successful result an execution middleware gave without dispatching: the script gets
    ``value`` as a direct call's model gets it, and the call does not count as started (it never
    reached its tool). A failed answer is a ``NotStarted`` instead, so the script's await raises."""

    def __init__(self, value: Any):
        self.value = value


def outer_ids(agent: Any) -> dict[str, str]:
    """The hook identity stock dispatch gives the outer call (agent.tool_executor)."""
    return {
        key: value
        for key, value in (
            ("turn_id", getattr(agent, "_current_turn_id", "") or ""),
            ("api_request_id", getattr(agent, "_current_api_request_id", "") or ""),
        )
        if value
    }


def _failed(target: str, raw: Any) -> bool:
    from agent.display import _detect_tool_failure

    return _detect_tool_failure(target, raw)[0]


def _value(name: str, target: str, raw: Any, failed: bool) -> Any:
    """A nested result as the script sees it: parsed JSON, or an exception for a failed call. A
    terminal result with a non-empty top-level error fails the await too: stock's display rule
    calls one without an exit code (refused before it ran, e.g. a foreground pty) a success."""
    value = parse_result(raw)
    blocked = target == "terminal" and isinstance(value, dict) and bool(value.get("error"))
    if not (failed or blocked):
        return value
    message = (
        value.get("error") or value.get("message") if isinstance(value, dict) else None
    )
    if target == "terminal" and isinstance(value, dict) and value.get("status") == "blocked":
        return refused(name, message or value)  # stock sets "blocked" only on a command it never ran
    if message and any(m in str(message) for m in _BY_USER):
        return refused(name, message)
    if target == "terminal" and isinstance(value, dict) and not message and "status" not in value:
        # a command that ran and exited non-zero; sh(check=False) reads its output back from this form
        return RuntimeError("%s failed: exit %s: %s" % (name, value.get("exit_code"), value.get("output") or ""))
    return RuntimeError("%s failed: %s" % (name, message or value))


def _scope_block(target: str, args: dict) -> str | None:
    """Why stock ``tool_call`` would refuse these arguments for a deferred tool, if it would."""
    from tools.tool_search_validation import validate_deferred_call_args

    error = validate_deferred_call_args(target, args)
    if error is None:
        return None
    body = parse_result(error)
    return str(body.get("error") or body) if isinstance(body, dict) else str(body)


class Policy:
    """How the nested calls of one codemode call are dispatched (None agent: a host call)."""

    def __init__(self, agent: Any, deferred: frozenset[str]):
        self.agent, self.deferred = agent, deferred
        self.gate = None
        if agent is not None:
            from agent.tool_executor import _ConcurrentToolAuthorizationGate

            self.gate = _ConcurrentToolAuthorizationGate()
        self._lock = (
            threading.Lock()
        )  # one guardrail observer, shared by the batch's threads

    def halted(self) -> str | None:
        """The guardrail hard stop of the calling agent's turn, once one was decided."""
        decision = getattr(self.agent, "_tool_guardrail_halt_decision", None)
        return (
            None
            if decision is None
            else "a guardrail stopped this turn (%s)"
            % getattr(decision, "code", "halt")
        )

    def call(
        self,
        name: str,
        target: str,
        args: dict,
        ids: dict,
        stopped: Callable[[], str | None],
        began: Callable[[], None],
    ) -> Any:
        """The script's value or exception; ``stopped() -> reason`` is checked at the dispatch
        boundary, ``began()`` is told once the call is dispatched."""
        import model_tools

        if self.agent is None:
            reason = stopped()
            if reason:
                return NotStarted("%s was not started: %s" % (name, reason))
            began()
            with model_tools.suppress_post_tool_call_hook(False):
                raw = model_tools.handle_function_call(target, args, **ids)
            return _value(name, target, raw, _failed(target, raw))
        with model_tools.suppress_post_tool_call_hook(False):
            return self._agent_call(name, target, args, ids, stopped, began)

    def _agent_call(
        self,
        name: str,
        target: str,
        args: dict,
        ids: dict,
        stopped: Callable[[], str | None],
        began: Callable[[], None],
    ) -> Any:
        from agent.tool_executor import (
            _BatchAbandoned,
            _run_agent_tool_execution_middleware,
            _ToolCallRef,
        )

        agent, task, call_id = self.agent, ids.get("task_id") or "", ids["tool_call_id"]

        def begin(callback: Callable[[], None] | None = None) -> None:
            if callback is None:  # a blocked call: stock emits its terminal event next
                return
            if stopped():
                raise _BatchAbandoned(
                    target
                )  # a BaseException: no middleware swallows it
            began()
            callback()

        def execute(final_args: dict) -> Any:
            return agent._invoke_tool(
                target,
                final_args,
                task,
                call_id,
                pre_tool_block_checked=True,
                skip_tool_request_middleware=True,
                skip_tool_execution_middleware=True,
            )

        scope = _scope_block(target, args) if target in self.deferred else None
        try:
            managed = _run_agent_tool_execution_middleware(
                agent,
                function_name=target,
                function_args=args,
                effective_task_id=task,
                tool_call_id=call_id,
                execute=execute,
                scope_block=scope,
                begin_execution=begin,
                authorization_gate=self.gate,
            )
        except _BatchAbandoned:
            message = "%s was not started: %s" % (name, stopped())
            _ToolCallRef(target, args, task, call_id, []).emit_post(
                agent,
                json.dumps({"error": message}),
                status="cancelled",
                error_type="codemode_stopped",
                error_message=message,
            )
            return NotStarted(message)
        if managed.blocked:
            body = parse_result(managed.result)
            reason = (
                (body.get("message") or body.get("error"))
                if isinstance(body, dict)
                else body
            )
            return refused(name, reason)
        failed = _failed(target, managed.result)
        if not managed.dispatched:  # stock emits the one terminal event of a middleware answer
            _ToolCallRef(target, managed.args, task, call_id, managed.middleware_trace).emit_post(agent, managed.result)
            value = _value(name, target, managed.result, failed)
            # a failed answer raises at the script's await; it still never reached its tool
            if isinstance(value, NotStarted):  # keeps a UserRefused one, so the human's no stops the script
                return value
            return NotStarted(str(value)) if isinstance(value, BaseException) else Answered(value)
        from tools.connectors.data_use import record_data_use

        record_data_use(managed.data_use)  # into the outer codemode call's sink, stamped on its message
        with self._lock:  # records the outcome and any hard stop; its model-facing notes are not data
            agent._append_guardrail_observation(target, managed.args, managed.result, failed=failed,
                                                tool_call_id=call_id)
        try:
            agent._record_file_mutation_result(target, managed.args, managed.result, failed, task_id=task)
        except Exception:  # stock: the turn-end verifier's bookkeeping never fails the call
            pass
        return _value(name, target, managed.result, failed)
