"""The codemode tool handler and the Monty pool behind it (spec Task 15; REQ-001, REQ-014, NFR-002).

One handler call: the calling agent's tool grant (caller.py) → its SDK → executor.run(script,
Bridge) → result.shape → JSON, and the nested calls that ran are counted for the catalog order
(usage.py). An agent whose grant cannot be read gets a refusal, never every tool. The pool opens on first use, or earlier in the
background when a session starts (``warm``), so the first script of a session does not pay the
cold start; register() never opens it, because Hermes stops a plugin load after 10 s. Session end
closes that session's REPL; unload and garbage collection close the pool.
"""

from __future__ import annotations

import json
import logging
import threading
import weakref
from pathlib import Path
from typing import Any

from . import usage

logger = logging.getLogger(__name__)


def _close(box: list) -> None:
    executor, box[0] = box[0], None
    if executor is not None:
        executor.close()


class Closed(RuntimeError):
    """The runtime was closed; it never opens a pool again."""


class Runtime:
    def __init__(self, binary: Path):
        self._binary = binary
        self._usage = usage.usage_path()  # fixed now: a script that outlives its home never writes elsewhere
        self._lock = threading.Lock()
        self._box: list = [
            None
        ]  # the executor, shared with the finalizer that closes it
        self._closed = False
        weakref.finalize(self, _close, self._box)

    @property
    def is_open(self) -> bool:
        return self._box[0] is not None

    def executor(self) -> Any:
        """The open executor; raises ``Closed`` once close() ran (unload is permanent)."""
        with self._lock:
            if self._closed:
                raise Closed("code-mode is closed (the plugin was unloaded)")
            if self._box[0] is None:
                from . import results
                from .executor import Executor
                from .sdk import session_sdk

                results.register_builtin()
                self._box[0] = Executor(self._binary, session_sdk)
            return self._box[0]

    def warm(self, **_host: Any) -> None:
        """on_session_start: open the pool off the request path (the host's keywords are not needed)."""
        if not self.is_open:
            threading.Thread(
                target=self._warm, name="codemode-warm", daemon=True
            ).start()

    def _warm(self) -> None:
        try:
            self.executor()
        except Closed:
            return
        except (
            Exception
        ) as exc:  # the first call reports it to the model; here only a log line
            logger.warning("codemode: the Monty pool did not open: %s", exc)

    def handle(
        self,
        args: dict,
        task_id: str | None = None,
        session_id: str | None = None,
        user_task: str | None = None,
        conversation_id: str | None = None,
    ) -> str:
        from . import caller, hints
        from .bridge import Bridge
        from .result import shape
        from .sdk import session_sdk
        from .served import definitions

        code = (args or {}).get("code")
        if not isinstance(code, str) or not code.strip():
            return json.dumps(
                {
                    "error": "codemode needs the Python script in the string argument 'code'"
                }
            )
        try:
            agent = caller.calling_agent()
            toolsets = (getattr(agent, "enabled_toolsets", None), getattr(agent, "disabled_toolsets", None))
            defs = None if agent is None else definitions(*toolsets)  # one selection, no side effects
            grant = caller.tool_grant(agent, defs)
        except caller.GrantUnknown as exc:
            return json.dumps({"error": str(exc)})
        sid = session_id or task_id or "codemode"
        names = None if grant is None else grant.names
        bridge = Bridge(
            sid, names, agent=agent, deferred=grant.deferred if grant else frozenset(),
            task_id=task_id, user_task=user_task, conversation_id=conversation_id,
        )
        try:
            executor = self.executor()
        except (Closed, ImportError) as exc:  # unloaded, or the client shadowed
            return json.dumps({"error": str(exc)})
        sdk = session_sdk(names, toolsets=toolsets, defs=defs)
        outcome = executor.run(sid, code, bridge, sdk=sdk, interrupted=caller.interrupt_check())
        outcome.error = hints.explain(outcome.error, sdk)
        if outcome.calls:
            try:
                usage.record(outcome.calls, self._usage)
            except OSError as exc:  # only the catalog order depends on it, never the result
                logger.warning("codemode: usage counts not saved: %s", exc)
        return shape(outcome, sid).to_json()

    def end_session(self, session_id: str | None = None, **_host: Any) -> None:
        """on_session_finalize / on_session_reset / on_session_delete; the host passes session_id
        by keyword together with platform, reason, profile and the like."""
        executor = self._box[0]
        if executor is not None and session_id:
            executor.end_session(session_id)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            _close(self._box)
