"""One Monty REPL and how one script runs in it (spec Task 6; used by executor.py).

A REPL is a checkout held open, with the SDK host modules and the prelude (sdk.py) fed once and
type checking on. ``run_script`` drives one script: every SDK call is answered with a future,
the calls a future resolution waits on are dispatched together (gather order), imports are
served by Monty itself. Only Monty turns count toward the script's time: a watchdog
(watchdog.py) kills the worker when they pass the limit (executor.py says why Monty's own feed
limit cannot be used) or Hermes interrupts the call; ``Repl.kill`` stops a script from outside
(session end). A REPL reloaded for an SDK that only added tools serves the SDK under
generation-prefixed module names, and a script's own SDK imports are renamed to match (imports.py).
"""

from __future__ import annotations

import os
import signal
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from . import imports
from .capture import Printed
from .nested import Answered, NotStarted, UserRefused
from .limits import (
    CEILING,
    DEFAULT_LIMITS,
    Limits,
    call_limit_message,
    call_limit_stop,
    time_limit_message,
)
from .sdk import Sdk, SdkTool
from .watchdog import Clock

PRELUDE_HEADROOM = (
    64  # suspensions the prelude imports take on top of the session budget
)
LOG_CHARS = 16_000_000  # print output kept whole per script; past it only the head (capture.py)


@dataclass(frozen=True)
class Call:
    name: str  # qualified SDK name, e.g. tools.read_file
    tool: SdkTool | None
    args: tuple
    kwargs: dict


Dispatch = Callable[
    [list[Call]], list[Any]
]  # one result per call: a value or an exception


@dataclass
class RunOutcome:
    value: Any = None
    error: str | None = None
    logs: str = ""
    calls: list[str] = field(default_factory=list)  # nested calls that reached their tool, in order
    attempted: int = 0  # nested calls the script made, refused ones too: the call limit counts these
    refused: list[str] = field(default_factory=list)  # why each refused or not-started call did not run
    refusal: str | None = None  # the stop message once a human refused a call (AGR-048)
    warnings: list[str] = field(default_factory=list)
    stopped: str | None = None  # "calls", "time" or "refused" (a human said no) when the script was stopped
    seconds: float = 0.0
    limits: Limits = DEFAULT_LIMITS


def fresh_repl(reason: str) -> str:
    return (
        "%s: a fresh REPL replaced the session REPL, earlier variables and definitions are gone"
        % reason
    )


class _Broken(Exception):
    """The REPL cannot serve the session any more; the message says why."""


class Repl:
    def __init__(self, pm: Any, pool: Any, sdk: Sdk, budget: int, generation: int = 0,
                 state: bytes | None = None):
        self.pm, self.pool, self.sdk, self.budget, self.used = pm, pool, sdk, budget, 0
        self.generation = generation
        self.prefix = imports.prefix(generation)
        self.pid: int | None = None
        self.killed: str | None = None  # why kill() stopped this REPL's worker
        self._held: int | None = None  # the worker's pid while this checkout holds it
        self._kill_lock = threading.Lock()
        self.session = pool.checkout(
            type_check=True,
            type_check_format="concise",
            external_modules=sdk.external_modules(self.prefix),
            limits={
                "max_feed_duration_secs": CEILING.seconds,
                "max_memory": CEILING.memory,
                "max_suspensions": budget + PRELUDE_HEADROOM,
            },
        )
        self.session.__enter__()
        try:
            if state is not None:
                self.session.load_session(state)
            snap = self.session.feed_start(sdk.prelude(self.prefix))
            while not isinstance(snap, pm.MontyComplete):
                snap = snap.resume_auto()
        except BaseException:
            self.close()
            raise
        self.pid = self.session.worker_pid
        self._held = self.pid

    def reload(self, sdk: Sdk) -> Repl:
        """This REPL's variables in a new checkout built with ``sdk``; this REPL is closed. Only for
        an SDK that keeps every name of this one on its target (executor.py), so no name a script
        saved can reach another tool. A restored dump keeps ty's view of the modules it imported
        (codemode/SPIKE.md), so the new checkout names the SDK modules with a generation prefix and
        its prelude binds them to the usual names. Raises when the dump or the load fails."""
        try:
            state = self.session.dump()
        finally:
            self.close()
        repl = Repl(self.pm, self.pool, sdk, self.budget, self.generation + 1, state)
        repl.used = self.used  # Monty's count restarts with the load; the session budget does not
        return repl

    def kill(self, reason: str) -> None:
        """Stop the script this REPL runs now by killing its worker; the REPL is lost."""
        with self._kill_lock:  # never after close() handed the worker back to the pool
            self.killed = self.killed or reason
            if self._held is not None:
                try:
                    os.kill(self._held, signal.SIGTERM)  # its wrapper (stopwrap.py) kills the worker
                except ProcessLookupError:  # it already died; the pool replaces it
                    pass

    def close(self) -> None:
        with self._kill_lock:  # the worker may serve another checkout next: no stop reaches it from here
            self._held = None
        try:
            self.session.__exit__(None, None, None)
        except Exception:  # a crashed worker is already gone; the pool replaces it
            pass


def run_script(repl: Repl, code: str, dispatch: Dispatch, out: RunOutcome,
               interrupted: Callable[[], bool] | None = None) -> bool:
    """Run ``code`` in ``repl``, filling ``out``; True when the REPL must be replaced.
    ``interrupted() -> bool`` stops the script when Hermes interrupts its call."""
    pm, clock, printed = repl.pm, Clock(out.limits.seconds, repl.kill, interrupted), Printed(LOG_CHARS)
    code, pending = imports.rewrite(code, repl.prefix), dict[int, Call]()
    try:
        snap = clock.turn(
            lambda: repl.session.feed_start(code, print_callback=printed)
        )
        while not isinstance(snap, pm.MontyComplete):
            repl.used += 1
            if repl.used > repl.budget:
                raise _Broken(
                    "the session REPL used its suspension budget of %d" % repl.budget
                )
            snap = clock.turn(_answer(repl, snap, pending, dispatch, out))
        out.value = snap.output
        if out.refusal:  # the script caught the stop and finished anyway
            out.value, out.error = None, out.refusal
        elif out.stopped == "calls":  # the script caught the stop and finished anyway
            out.error = call_limit_message(out.limits, len(out.calls))
        return False
    except _Broken as exc:
        out.error = fresh_repl(str(exc))
        return True
    except pm.MontyCrashedError as exc:
        if clock.fired or exc.timed_out:
            out.stopped, reason = "time", time_limit_message(out.limits)
        elif repl.killed:
            reason = repl.killed
        else:
            reason = "the sandbox worker stopped (%s)" % exc
        out.error = "%s\n%s" % (reason, fresh_repl("the REPL was lost with its worker"))
        return True
    except pm.MontyError as exc:
        return _failed(exc, out, repl, clock)
    finally:
        out.logs = printed.text()


def _failed(exc: Any, out: RunOutcome, repl: Repl, clock: Clock) -> bool:
    """A Monty error of the script. The REPL is replaced only on structured evidence that a
    resource ran out: the watchdog's sandbox time (Monty's own feed limit sits at the ceiling,
    beyond it) or the host's suspension count, never because the error text mentions a limit."""
    display = getattr(exc, "display", None)
    text = display() if callable(display) else str(exc)
    if repl.prefix:  # diagnostics name the SDK modules as the script does
        text = imports.plain(text)
    if clock.fired or clock.left <= 0:  # Monty's backstop at the ceiling
        out.stopped = "time"
        reason = fresh_repl("a timed-out heap is unsafe")
        out.error = "%s\n%s\n%s" % (time_limit_message(out.limits), text, reason)
        return True
    if repl.used >= repl.budget:
        reason = fresh_repl("the session REPL used its suspension budget")
        out.error = "%s\n%s" % (text, reason)
        return True
    if out.refusal:
        out.error = out.refusal + "\n" + text
        return False
    stopped = out.stopped == "calls"
    out.error = call_limit_message(out.limits, len(out.calls)) + "\n" + text if stopped else text
    return False


def _answer(
    repl: Repl, snap: Any, pending: dict[int, Call], dispatch: Dispatch, out: RunOutcome
) -> Callable[[], Any]:
    """How to resume ``snap``; nested calls are dispatched here, outside the script clock."""
    pm = repl.pm
    if isinstance(snap, pm.FunctionSnapshot) and snap.is_os_function:
        return snap.resume_auto  # imports of the bundled and SDK modules
    if isinstance(snap, pm.FunctionSnapshot):
        if out.refusal:  # every later call gets the stop again, as for the call limit
            refusal = KeyboardInterrupt(out.refusal)
            out.refused.append(_not_started(snap, out.refusal))
            return lambda: snap.resume({"exception": refusal})
        if out.attempted + len(pending) >= out.limits.calls:
            # KeyboardInterrupt: an `except Exception` cannot swallow the stop, and a script that
            # catches it anyway gets the same stop from every later call
            out.stopped = "calls"
            stop = KeyboardInterrupt(call_limit_stop(out.limits))
            out.refused.append(_not_started(snap, str(stop)))
            return lambda: snap.resume({"exception": stop})
        name = imports.plain(str(snap.function_name))
        tool = repl.sdk.resolve(name)
        pending[snap.call_id] = Call(name, tool, tuple(snap.args), dict(snap.kwargs))
        return lambda: snap.resume({"future": ...})
    if isinstance(snap, pm.FutureSnapshot):
        waiting = set(snap.pending_call_ids)
        ids = [i for i in pending if i in waiting]  # call order, which is gather order
        batch = [pending.pop(i) for i in ids]
        out.attempted += len(batch)
        raw = _settle(dispatch, batch)
        out.calls.extend(c.name for c, r in zip(batch, raw) if not isinstance(r, (NotStarted, Answered)))
        out.refused.extend(str(r) for r in raw if isinstance(r, NotStarted))
        for call, r in zip(batch, raw):
            if isinstance(r, UserRefused) and out.refusal is None:
                out.stopped, out.refusal = "refused", refusal_message(call.name, str(r))
        results = [_resumed(KeyboardInterrupt(out.refusal) if isinstance(r, UserRefused) else r) for r in raw]
        answers = dict(zip(ids, results))
        for lost in waiting - set(answers):
            answers[lost] = {
                "exception": RuntimeError("no nested call %d is pending" % lost)
            }
        return lambda: snap.resume(answers)
    return snap.resume_auto  # a name lookup: Monty raises NameError for an unknown name


def _not_started(snap: Any, reason: str) -> str:
    """Why a call the script made after the stop did not run (the 'not run' summary, result.py)."""
    return "%s was not started: %s" % (imports.plain(str(snap.function_name)), reason)


def refusal_message(name: str, reason: str) -> str:
    return ("the user refused %s, so the whole script was stopped: ask the user what to do next before "
            "going on (%s)" % (name, reason))


def _resumed(result: Any) -> dict:
    """How Monty resumes a call with one bridge result."""
    if isinstance(result, BaseException):
        return {"exception": result}
    return {"return_value": result.value if isinstance(result, Answered) else result}


def _settle(dispatch: Dispatch, batch: list[Call]) -> list[Any]:
    try:
        results = list(dispatch(batch))
    except Exception as exc:  # a failing bridge must not leave the script suspended
        results = [RuntimeError("nested call failed: %s" % exc)] * len(batch)
    if len(results) != len(batch):
        problem = "nested calls returned %d results for %d calls" % (
            len(results),
            len(batch),
        )
        results = [RuntimeError(problem)] * len(batch)
    return results
