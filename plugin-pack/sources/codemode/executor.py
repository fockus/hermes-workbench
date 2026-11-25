"""Monty executor (spec Task 6; REQ-003, REQ-004, REQ-017, REQ-018, REQ-019, REQ-033).

One pool of Monty workers per process. A Hermes session keeps one REPL (repl.py) for its
sequential scripts; a second concurrent script of the session runs in a fresh REPL without
session state, a third waits. Monty type-checks every snippet against the SDK stubs and the
names the REPL already holds (it keeps that state across dumps too, codemode/SPIKE.md), so an
unknown tool, a wrong argument or an unknown name is refused with its line before any nested
call; the host adds only what ty cannot see: stop-listed tools ("call it directly").

Scripts get no mounts and no ``os`` handler: no filesystem, network or environment, and only
the modules Monty bundles. Sandbox time is enforced by a watchdog (watchdog.py), because Monty
fixes its feed time limit per checkout and a dump carries it, so a pragma that lowered it could
never be raised again in that REPL; Monty's own limit stays at the ceiling as a backstop. A REPL
that hit the time limit, crashed or used its suspension budget is replaced with a warning, and one
whose remaining budget might not finish the next script (fewer suspensions left than twice its call
limit) is renewed before it starts, never in the middle of its side effects. Ending a session or
closing the executor kills the workers of its running scripts instead of waiting for them, by their
pid while the REPL still holds the checkout; Monty starts each worker under a wrapper
(stopwrap.py) that also kills it should the host die. When
the session's tools changed since its REPL was created (REQ-026): tools only added, the REPL is
reloaded with the new SDK and keeps its variables (every saved name still reaches its own tool with
its own argument mapping, so nothing is tracked); a name removed, retargeted or with changed
parameters, a fresh REPL replaces it with a warning, and ty
refuses an old name before any nested call. A failed reload starts a fresh REPL with a warning.
"""

from __future__ import annotations

import ast
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import imports, stopwrap
from .limits import DEFAULT_LIMITS, Limits, parse_pragma
from .repl import Call, Dispatch, Repl, RunOutcome, fresh_repl, run_script
from .sdk import STOP_LIST, Sdk
from .watchdog import INTERRUPTED

__all__ = ["Call", "Dispatch", "Executor", "RunOutcome", "SUSPENSION_BUDGET"]

SUSPENSION_BUDGET = (
    1000  # Monty suspensions one session REPL takes before it is renewed
)
MAX_CONCURRENT = 2  # scripts per session at once; the second runs without session state
MAX_WORKERS = 16
CHECKOUT_TIMEOUT = 60.0
SESSION_ENDED = "the session ended while the script ran"
CLOSED = "code-mode closed while the script ran"


def hook_refusal(session_id: str, dispatch: Dispatch, tool: str) -> str | None:
    """Why the agent's own pre_tool_call rules refuse ``tool`` as a direct call, if they do. Only the stock twin
    ``execute_code`` is asked: "call it directly" misleads an agent whose rules refuse that call too (AGR-094)."""
    if tool != "execute_code":
        return None
    try:
        from hermes_cli.plugins import get_pre_tool_call_block_message

        turn_id = (getattr(dispatch, "ids", None) or {}).get("turn_id") or ""
        return get_pre_tool_call_block_message(tool, {}, session_id=session_id, turn_id=turn_id, tool_call_id="")
    except Exception:  # fail open to the plain refusal; the script never runs either way
        return None


def direct_calls(code: str, refusal: Callable[[str], str | None] = lambda tool: None) -> str | None:
    """Refusal for stop-listed tools called from code, which the SDK stubs cannot name; ``refusal(tool)`` may
    give the agent's own reason for a tool it cannot call directly either."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None  # Monty reports it with its line
    found = sorted(
        (node.lineno, node.attr)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "tools"
        and node.attr in STOP_LIST
    )
    if not found:
        return None
    line, name = found[0]
    reason = refusal(name)
    if reason:
        return "line %d: %s" % (line, reason)
    return (
        "line %d: tools.%s stays a direct tool: call it directly, outside codemode"
        % (
            line,
            name,
        )
    )


def _moved(old: Sdk, new: Sdk) -> str | None:
    """The first name of ``old`` that ``new`` removed, points at another tool or maps its arguments
    differently (a saved callable must never send them under other names), if any."""
    for name in old.all_tools:
        was, now = old.resolve(name), new.resolve(name)
        if was is None or now is None or (now.kind, now.target, now.params) != (was.kind, was.target, was.params):
            return name
    return None


@dataclass
class _Session:
    slots: threading.BoundedSemaphore = field(
        default_factory=lambda: threading.BoundedSemaphore(MAX_CONCURRENT)
    )
    lock: threading.Lock = field(
        default_factory=threading.Lock
    )  # held while the REPL runs
    repl: Repl | None = None
    note: str | None = None  # why the next session REPL starts fresh
    last: float = 0.0
    running: set = field(default_factory=set)  # REPLs running a script of this session now
    ended: str | None = None  # why the session is gone; its REPLs close when their script stops


class Executor:
    """Runs scripts for Hermes sessions; ``dispatch`` answers their nested calls (the bridge)."""

    def __init__(
        self,
        binary: Path,
        sdk_factory: Callable[[], Sdk],
        max_workers: int = MAX_WORKERS,
    ):
        from .deps import client

        pm = client()  # the validated client, never whatever `import pydantic_monty` finds later
        self._pm, self._sdk_factory = pm, sdk_factory
        self._launcher = stopwrap.launcher(str(binary))
        try:
            self._pool = pm.Monty(
                binary_path=self._launcher,
                min_processes=1,
                max_processes=max_workers,
                checkout_timeout=CHECKOUT_TIMEOUT,
            )
            self._pool.__enter__()
        except BaseException:
            shutil.rmtree(os.path.dirname(self._launcher), ignore_errors=True)
            raise
        self._lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}
        self._max_repls = max(1, max_workers - 2 * MAX_CONCURRENT)

    def run(
        self,
        session_id: str,
        code: str,
        dispatch: Dispatch,
        base: Limits = DEFAULT_LIMITS,
        sdk: Sdk | None = None,
        interrupted: Callable[[], bool] | None = None,
    ) -> RunOutcome:
        """``sdk``: the calling session's SDK (default: the factory's); ``interrupted() -> bool``
        stops the script when Hermes interrupts the call."""
        start = time.monotonic()
        code = code.replace("\r\n", "\n").replace("\r", "\n")  # Python's own reading of source lines
        limits, notes = parse_pragma(code, base)
        out = RunOutcome(limits=limits, warnings=notes, error=direct_calls(code, lambda tool: hook_refusal(session_id, dispatch, tool)) or imports.nested_import(code))
        try:
            if out.error is None:
                self._admit(session_id, code, dispatch, out, self._sdk_factory() if sdk is None else sdk,
                            interrupted)
        except ImportError as exc:  # the pinned client was shadowed after activation (deps.client)
            out.error = "codemode refused to run: %s" % exc
        except self._pm.MontyCrashedError as exc:  # a REPL's worker that never started
            out.error = "codemode refused to run: the sandbox worker did not start (%s)" % exc
        out.seconds = time.monotonic() - start
        return out

    def _admit(self, session_id: str, code: str, dispatch: Dispatch, out: RunOutcome, sdk: Sdk,
               interrupted: Callable[[], bool] | None) -> None:
        with self._lock:
            state = self._sessions.setdefault(session_id, _Session())
        with state.slots:
            if state.lock.acquire(blocking=False):
                try:
                    self._run_in_session(state, code, dispatch, out, sdk, interrupted)
                finally:
                    state.lock.release()
            else:
                repl = self._new_repl(sdk)
                try:
                    self._run(state, repl, code, dispatch, out, interrupted)
                finally:
                    repl.close()

    def end_session(self, session_id: str, reason: str = SESSION_ENDED) -> None:
        """Close the session's REPL; a script it runs now is killed, never waited for."""
        with self._lock:
            state = self._sessions.pop(session_id, None)
            if state is None:
                return
            state.ended, running = reason, list(state.running)
        for repl in running:
            repl.kill(reason)
        if state.lock.acquire(blocking=False):  # else the running script's thread closes it
            try:
                if state.repl is not None:
                    state.repl.close()
                    state.repl = None
            finally:
                state.lock.release()

    def close(self) -> None:
        for session_id in list(self._sessions):
            self.end_session(session_id, CLOSED)
        self._pool.__exit__(None, None, None)
        shutil.rmtree(os.path.dirname(self._launcher), ignore_errors=True)

    def _new_repl(self, sdk: Sdk | None = None) -> Repl:
        if sdk is None:
            sdk = self._sdk_factory()
        return Repl(self._pm, self._pool, sdk, SUSPENSION_BUDGET)

    def _run(self, state: _Session, repl: Repl, code: str, dispatch: Dispatch, out: RunOutcome,
             interrupted: Callable[[], bool] | None) -> bool:
        """run_script, with the REPL listed as running so that end_session can stop it."""
        with self._lock:  # an end_session after this sees the REPL running and kills it
            if state.ended:
                out.error = state.ended
                return True
            state.running.add(repl)
        stop_when = getattr(dispatch, "stop_when", None)  # the bridge: start no call once stopped
        if callable(stop_when):
            stop_when(lambda: repl.killed or (INTERRUPTED if interrupted is not None and interrupted() else None))
        try:
            return run_script(repl, code, dispatch, out, interrupted)
        finally:
            with self._lock:
                state.running.discard(repl)

    def _renewal(self, repl: Repl, code: str, calls: int) -> str | None:
        """Why the session REPL must be renewed before this script, if it must. A script may take
        two suspensions per call (the call and its future) plus one per SDK import; with fewer
        left it might run out mid-way. A fresh REPL only helps when its whole budget holds that much."""
        left, need = repl.budget - repl.used, 2 * calls + imports.sdk_imports(code)
        if left <= 0:
            return "the session REPL used its suspension budget of %d" % repl.budget
        if left < need <= repl.budget:
            return ("the session REPL had %d of its %d suspensions left, fewer than this script may take"
                    % (left, repl.budget))
        return None

    def _run_in_session(self, state: _Session, code: str, dispatch: Dispatch, out: RunOutcome, sdk: Sdk,
                        interrupted: Callable[[], bool] | None) -> None:
        repl = state.repl
        renew = None if repl is None else self._renewal(repl, code, out.limits.calls)
        if repl is not None and renew is not None:
            state.note = renew
            repl.close()
            repl = state.repl = None
        if repl is not None and repl.sdk != sdk:
            state.repl, moved = None, _moved(repl.sdk, sdk)
            if moved:
                state.note = "the session's tools changed (%s no longer reaches the same tool the same way)" % moved
                repl.close()
            else:
                try:
                    state.repl = repl.reload(sdk)
                except Exception as exc:  # dump or load refused, worker gone: a fresh REPL serves
                    state.note = "the session's tools changed and its REPL could not be reloaded (%s)" % exc
        if state.repl is None:
            self._evict_idle()
            if state.note:
                out.warnings.append(fresh_repl(state.note))
                state.note = None
            state.repl = self._new_repl(sdk)
        state.last = time.monotonic()
        if self._run(state, state.repl, code, dispatch, out, interrupted) or state.ended:
            state.repl.close()
            state.repl = None

    def _evict_idle(self) -> None:
        """Close the least recently used idle session REPL when the pool has no room for more."""
        with self._lock:
            live = [s for s in self._sessions.values() if s.repl is not None]
        if len(live) < self._max_repls:
            return
        for victim in sorted(live, key=lambda s: s.last):
            if victim.lock.acquire(blocking=False):
                try:
                    if victim.repl is not None:
                        victim.repl.close()
                        victim.repl = None
                        victim.note = (
                            "the idle session REPL was closed to free a sandbox worker"
                        )
                finally:
                    victim.lock.release()
                return
