"""What the plugin's entry points share (workflow-engine Task 16): the opt-in state, the agent host, the engine
factory and the run controls.

``Wiring(ctx, make_host=None)``: ``host(cwd=None)`` builds a ``HermesAgentHost`` for the calling session's
workspace on every call (``ctx.session_cwd()``, I-057: one process serves many sessions, so a host kept from an
earlier call would run gates and ``load`` in the wrong folder). ``make_host(cwd)`` replaces it; the tests use that
seam. ``router_model()`` is the ``router_model`` plugin setting, else ``ROUTER_MODEL``.
``pre_llm_call`` is the plugin's hook: it feeds ``OptIn`` and keeps the ``+N`` budget directive of the session's latest
turn (REQ-021, ``limits.budget_total``); ``budget(session)`` returns it, None when that turn had none.
``engine(host=None)`` is a ``WorkflowEngine`` whose progress feeds the one ``Controls`` of the plugin
(``controls()``, built on first use), so ``/workflow skip`` sees the agents of a run a tool call started. That
``Controls`` runs and resumes through an engine built per call (host of the asking session), never one kept from
its first use.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from . import limits
from .optin import OptIn, _text
from .router import ROUTER_MODEL


class Wiring:
    def __init__(self, ctx: Any, make_host: Callable[[str], Any] | None = None):
        self.ctx, self.optin, self._make_host = ctx, OptIn(), make_host
        self._controls: Any = None
        self._budgets: dict[str, int | None] = {}
        self._lock = threading.Lock()

    def host(self, cwd: str | None = None) -> Any:
        cwd = cwd or self.ctx.session_cwd()
        if self._make_host is not None:
            return self._make_host(cwd)
        from .agents import HermesAgentHost

        return HermesAgentHost(api=self._api, cwd=cwd)

    def _api(self, cwd: str | None) -> Any:
        from .subagents_link import api_class

        return api_class(self.ctx)(cwd=(lambda: cwd) if cwd else None)

    def pre_llm_call(self, session_id: str = "", user_message: object = "", **_: Any) -> None:
        self.optin.pre_llm_call(session_id=session_id, user_message=user_message)
        with self._lock:
            self._budgets[session_id] = limits.budget_total(_text(user_message))

    def budget(self, session: str) -> int | None:
        with self._lock:
            return self._budgets.get(session)

    def router_model(self) -> str:
        value = self.ctx.get_config("router_model")
        return value.strip() if isinstance(value, str) and value.strip() else ROUTER_MODEL

    def engine(self, host: Any = None) -> Any:
        self.controls()  # before the run starts: its progress is fed from a bridge thread, which has no session
        return self._engine(host or self.host())

    def controls(self, cwd: str | None = None) -> Any:
        """The plugin's ``Controls``, built once; its engine takes the host of each call (``/workflow resume`` from
        another session gates and loads in that session's workspace; runs store no workspace). With ``cwd`` (the
        CLI process, which has no session) a ``Controls`` for that folder, not kept."""
        if cwd is not None:
            return self._build_controls(cwd)
        with self._lock:
            if self._controls is None:
                self._controls = self._build_controls(None)
            return self._controls

    def _build_controls(self, cwd: str | None) -> Any:
        from .controls import Controls

        return Controls(_PerCall(lambda: self._engine(self.host(cwd))))

    def _engine(self, host: Any) -> Any:
        from .engine import WorkflowEngine

        return WorkflowEngine(host=host, on_progress=self._feed)

    def _feed(self, run_id: str, entries: list) -> None:
        if self._controls is not None:
            self._controls.feed(run_id, entries)


class _PerCall:
    """An engine facade for ``Controls``: each ``run`` / ``resume`` builds its engine (and host) when it is called."""

    def __init__(self, make: Callable[[], Any]):
        self._make = make

    def run(self, *args: Any, **kwargs: Any) -> Any:
        return self._make().run(*args, **kwargs)

    def resume(self, *args: Any, **kwargs: Any) -> Any:
        return self._make().resume(*args, **kwargs)
