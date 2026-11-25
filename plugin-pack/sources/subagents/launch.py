"""The global limiter on delegate_task spawns (REQ-016; wave-1 decision: a queued spawn never waits).

Nothing is admitted or queued at a spawn's ``pre_tool_call``: another hook may still veto the call or its
approval may be declined. Stock calls ``route_task`` only for a call it executes, once per task
(calls.py), and that is where each task takes its slot atomically (``admit``): it runs, or it is queued in
the limiter and refused by its route with its queue position (stock then builds no child for it), or it is
refused at the root task's cap. The call's result carries the queued note (``tool_result``); a call whose
every task queued answers ``status: queued``. A child keeps its task's slot from ``subagent_start`` until
``subagent_stop``; a slot whose child never starts is freed when its call ends.

A queued spawn is never relaunched by the plugin (Astra review round 2, launch.py:264): stock runs delegate_task
only inside the agent loop (model_tools.py ``_AGENT_LOOP_TOOLS``), so no stock path would re-run it through the
host's tool scope, guardrails and middleware under the requester's current permissions. When the limiter
promotes it, its slot goes straight on to the next launch and the requester is told, in its own chat (the
session key bound when it queued), to spawn it again; that spawn is authorized like any other.

A continue (a model's call or a message wake) takes its slot before stock rebuilds the child, and is refused
when there is no room; stock's automatic resume, which nothing can refuse, is counted when it starts.

The ledger is process-wide like the limiter it accounts for (one for every Hermes home of the process, so a
plugin reload never strands a slot). A queued launch carries the launcher that queued it: whichever load frees
the slot, the notice goes out through its own profile's plugin load.
"""

from __future__ import annotations

import contextvars
import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .callers import CallerMap, active_parent
from .calls import QUEUED as SLOT_QUEUED
from .calls import REFUSED as SLOT_REFUSED
from .calls import ROUTED, STARTED, Call, Calls
from .calls import action_of
from .limiter import QUEUED, REFUSED, RUNNING, Limiter, process_shared
from .stockcalls import SPAWN, bound_key, nothing_ran, route_error

logger = logging.getLogger("subagents.launch")

FULL_NOTE = ("Not continued now: the global agent limiter is full (8 running, 4 per provider). Continue %s again "
             "after one of the running agents finishes.")
PROVIDERS_KEPT = 1024  # subagent id -> the provider its slot counted on, for its continues
QUEUED_NOTE = ("Queued by the global agent limiter (8 running, 4 per provider): it does not start by itself. When a "
               "slot frees you are told; then spawn it again with delegate_task.")
FREED_NOTE = ("[subagents] A slot of the global agent limiter is free for the queued agent %s (%s); it was not "
              "started for you: spawn it again with delegate_task if it is still needed. Goal: %s")


@dataclass(frozen=True)
class QueuedLaunch:
    launch_id: str
    owner_session: str
    goal: str
    label: str
    promoted: Callable[["QueuedLaunch"], None]  # the queuing launcher's ``_promoted``: tells the requester
    scope: contextvars.Context  # the requester's context when it queued (its profile and delivery origin)
    session_key: str = ""  # the requester's chat (gateway route key) bound when it queued; "" in a CLI


@dataclass
class Ledger:
    """Slots held by running children and the queued launches. The fields are guarded by ``lock``."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    held: dict[str, str] = field(default_factory=dict)              # child session -> limiter id
    queued: dict[str, QueuedLaunch] = field(default_factory=dict)
    reserved: dict[str, tuple[str, str, str]] = field(default_factory=dict)  # lease -> (subagent id, session, call)
    continued: dict[tuple[str, str], str] = field(default_factory=dict)  # (session, continue call) -> its child


LEDGER: Ledger = process_shared("ledger", Ledger)


class Launcher:
    """``admit`` (the provider's route_task), subagent_start, subagent_stop and ``tool_result``."""

    def __init__(self, limiter: Limiter, callers: CallerMap, calls: Calls,
                 inject: Callable[[str], Any] | None = None, settle: Callable[[str], Any] | None = None):
        self.limiter = limiter
        self.callers = callers
        self.calls = calls
        self.inject = inject
        self._providers: dict[str, str] = {}
        # a child stock built but never ran (no subagent_stop will come): the plugin's own stop path, by session
        self.settle = settle or (lambda child_session: self.subagent_stop(child_session_id=child_session))

    def admit(self, task: dict[str, Any], route: dict[str, Any]) -> None:
        """Takes the slot of the task stock is routing now; raises the route error that queues or refuses it."""
        parent = active_parent()
        session = str(getattr(parent, "session_id", "") or "")
        hit = self.calls.route(session, str(getattr(parent, "_current_turn_id", "") or ""), task.get("index"),
                               task) if parent is not None else None
        if hit is None:  # not a staged spawn: a durable resume stock re-resolves (delegate_tool_continue.py:106)
            with LEDGER.lock:
                reserved = any(s == session for _, s, _ in LEDGER.reserved.values())  # a continue holding its slot
            provider = _destination(route, parent)
            if parent is not None and not reserved and self.limiter.preview(provider) != RUNNING:
                raise route_error()("Not resumed now: the global agent limiter is full (8 running, 4 per "
                                     "provider). It can be continued after one of the running agents finishes.")
            return  # its child takes its slot at subagent_start
        call, slot = hit
        provider = _destination(route, parent)
        # an API spawn's explicit root_task, else the caller's root (a model's slot stages root "")
        root, lease = slot.spawn.root or self.callers.root_of(session), "launch-%s" % uuid.uuid4().hex[:12]
        label = str(slot.task.get("name") or slot.task.get("agent_type") or slot.goal or lease)[:80]
        item = QueuedLaunch(lease, session, slot.goal, label, self._promoted, contextvars.copy_context(),
                            bound_key())
        with LEDGER.lock:  # published before the limiter can promote it, withdrawn unless it queued
            LEDGER.queued[lease] = item
            verdict = self.limiter.acquire(lease, provider=provider, root_task=root)
            if verdict != QUEUED:
                del LEDGER.queued[lease]
        if verdict == RUNNING:
            slot.lease = lease
            return
        if verdict == REFUSED:
            slot.state = SLOT_REFUSED
            raise route_error()("Not started: root task %s has reached the limit of %d agents."
                                 % (root, self.limiter.per_root))
        slot.state = SLOT_QUEUED
        call.meta.setdefault("queued", []).append(item)
        raise route_error()("Queued as %s (position %s); not started now. %s"
                             % (lease, self.limiter.position(lease), QUEUED_NOTE))

    def pre_tool_call(self, tool_name: str = "", args: Any = None, session_id: str = "", tool_call_id: str = "",
                      **_: Any) -> dict | None:
        """A model's own continue takes its slot at its call, before stock rebuilds the child; no room refuses it."""
        target = args.get("subagent_id") if tool_name == SPAWN and action_of(args) == "continue" else None
        refusal = self.reserve(target.strip(), session_id, tool_call_id or "") \
            if isinstance(target, str) and target.strip() else None
        return {"action": "block", "message": refusal} if refusal else None

    def reserve(self, subagent_id: str, session_id: str, call_id: str = "") -> str | None:
        """The slot of one continue call of ``subagent_id`` (a model's call or a message wake), owned by that call
        until its child starts; None when taken, else why it may not run now (nothing is queued for it)."""
        lease = "continue-%s" % uuid.uuid4().hex[:12]
        if self.limiter.acquire(lease, provider=self._providers.get(subagent_id), wait=False) != RUNNING:
            return FULL_NOTE % subagent_id
        with LEDGER.lock:
            LEDGER.reserved[lease] = (subagent_id, session_id, call_id or lease)
        return None

    def unreserve(self, session_id: str = "", call_id: str = "") -> None:
        """Frees the slots of continues no child took: of call ``call_id`` (of ``session_id`` when given: a provider's
        call ids are unique only per session), else every one ``session_id`` holds."""
        with LEDGER.lock:
            gone = [k for k, (_, s, c) in LEDGER.reserved.items() if ((c == call_id and session_id in ("", s))
                    if call_id else (session_id and s == session_id))]
            for lease in gone:
                del LEDGER.reserved[lease]
        for lease in gone:
            self._start(self.limiter.release(lease))

    def post_tool_call(self, tool_name: str = "", session_id: str = "", tool_call_id: str = "", status: Any = None,
                       **_: Any) -> None:
        """A delegate_task call stock blocked or cancelled before it ran (a later hook's veto, an interrupt): its
        staged tasks are dropped and a continue's reserved slot is freed at once (judge-A G4, G12)."""
        if tool_name == SPAWN and status in ("blocked", "cancelled") and tool_call_id:
            self.calls.drop(session_id, tool_call_id)
            self.unreserve(session_id, tool_call_id)

    def pre_llm_call(self, session_id: str = "", **_: Any) -> None:
        """The session's next model call: a continue it asked for that never started (vetoed) gives its slot back."""
        self.unreserve(session_id=session_id)

    def subagent_start(self, child_session_id: str = "", child_subagent_id: str = "", **_: Any) -> None:
        slot = self.calls.bound(child_session_id)
        held = slot.lease if slot is not None else None
        if held is None:  # a continued, resumed or unstaged child: the slot its continue call took, else one now
            with LEDGER.lock:
                lease = next((k for k, (sid, _, _) in LEDGER.reserved.items() if sid == child_subagent_id), None) \
                    if child_subagent_id else None
                reserved = LEDGER.reserved.pop(lease) if lease is not None else None
                if reserved is not None:
                    LEDGER.continued[reserved[1:]] = child_session_id
            held = lease or child_subagent_id or child_session_id
            if reserved is None:  # a start no route admitted (stock's automatic resume): counted, never refused
                if self.limiter.acquire(held, provider=self._providers.get(child_subagent_id), wait=False) != RUNNING:
                    self.limiter.acquire(held, provider=self._providers.get(child_subagent_id), force=True)
                    self._over_cap(child_session_id, child_subagent_id)
        if child_subagent_id:
            self._providers.pop(child_subagent_id, None)
            self._providers[child_subagent_id] = self.limiter.provider_of(held)
            while len(self._providers) > PROVIDERS_KEPT:
                self._providers.pop(next(iter(self._providers)))
        with LEDGER.lock:  # a start of a session still holding a slot (its run's stop not fired yet) frees it
            stale, LEDGER.held[child_session_id] = LEDGER.held.get(child_session_id), held
        if stale is not None and stale != held:
            self._start(self.limiter.release(stale))

    def _over_cap(self, child_session: str, subagent_id: str) -> None:
        """Stock's automatic resume has no route to refuse it: the overshoot is bounded (R-A2) and only logged; the
        child keeps its own policy."""
        logger.warning("subagents: %s started past the agent limiter's caps with no route to refuse it; it runs and "
                       "is counted", subagent_id or child_session)

    def rebind(self, old_session_id: str, new_session_id: str) -> None:
        """A child compacted into a new session keeps the slot it holds."""
        with LEDGER.lock:
            if old_session_id in LEDGER.held:
                LEDGER.held[new_session_id] = LEDGER.held.pop(old_session_id)

    def subagent_stop(self, child_session_id: str = "", **_: Any) -> None:
        with LEDGER.lock:
            launch_id = LEDGER.held.pop(child_session_id, None)
        if launch_id is not None:
            self._start(self.limiter.release(launch_id))

    def end_continue(self, call_id: str, result: str, session_id: str = "") -> None:
        """The end of one continue (a model's call or a message wake, whose id is unique): its unused slot back; a
        child stock built but cancelled before dispatch is settled."""
        self.unreserve(session_id, call_id)
        with LEDGER.lock:
            key = next((k for k in LEDGER.continued if k[1] == call_id and session_id in ("", k[0])), None)
            continued = LEDGER.continued.pop(key) if key is not None else None
        if continued is not None and nothing_ran(result):
            self.settle(continued)

    def tool_result(self, result: str, session_id: str = "", call_id: str = "") -> str | None:
        """The end of a delegate_task call: slots whose child never started freed, the queued note added."""
        if call_id:
            self.end_continue(call_id, result, session_id)
        call = self.calls.close(session_id, call_id)
        if call is None:
            return None
        if nothing_ran(result):  # stock closed every child it built (a later build failed): none will stop
            for slot in call.slots:
                if slot.state == STARTED and slot.child:
                    self.settle(slot.child)
        queued = call.meta.get("queued") or []
        note = {"status": "queued", "note": QUEUED_NOTE,
                "queued": [{"agent": item.label, "launch_id": item.launch_id,
                            "position": self.limiter.position(item.launch_id)} for item in queued]}
        self._free_unstarted(call)  # after the note: the room it frees may start a queued launch at once
        if not queued:
            return None
        try:
            data = json.loads(result)
        except ValueError:
            data = None
        if all(slot.state == SLOT_QUEUED for slot in call.slots) and len(call.slots) == len(queued):
            return json.dumps(note, ensure_ascii=False)  # every task queued: the call is answered as queued
        if not isinstance(data, dict):
            return "%s\n%s" % (result, json.dumps(note, ensure_ascii=False))
        return json.dumps({**data, "queued": note["queued"], "queued_note": note["note"]}, ensure_ascii=False)

    def _free_unstarted(self, call: Call) -> None:
        stale = [slot.lease for slot in call.slots if slot.state == ROUTED and slot.lease is not None]
        for lease in stale:
            self._start(self.limiter.release(lease))

    def promote(self, launch_ids: list[str]) -> None:
        """Launches the limiter promoted (``release``'s answer): a queued spawn's slot goes on at once and its requester
        is told; a launch this ledger does not hold (the API's, a team's) is left to its owner."""
        pending = list(launch_ids)
        while pending:
            launch_id = pending.pop(0)
            with LEDGER.lock:
                item = LEDGER.queued.pop(launch_id, None)
            if item is None:  # not a delegate_task launch (the API's or a team's): its owner starts it
                continue
            pending += self.limiter.release(launch_id)
            item.promoted(item)

    _start = promote  # the private name the launcher's own release paths call

    def _promoted(self, item: QueuedLaunch) -> None:
        self._tell(item, FREED_NOTE % (item.label, item.launch_id, item.goal))

    def _tell(self, item: QueuedLaunch, text: str) -> None:
        """A notice to the requester, sent from its own context to its own chat key. A notice no host accepted is
        logged whole, never dropped silently."""
        try:
            accepted = self.inject is not None and bool(item.scope.copy().run(self.inject, text, item.session_key))
            reason = "no Hermes session accepted it" if self.inject is not None else "no host can inject here"
        except Exception as exc:
            accepted, reason = False, "%s: %s" % (type(exc).__name__, exc)
        if not accepted:
            logger.warning("subagents: notice for %s not delivered to session %s (%s): %s", item.label,
                           item.owner_session, reason, text)


def _destination(route: dict[str, Any], parent: Any) -> str:
    """The provider a route runs on: its own, else the parent's (phase 1 refuses profile routes, provider.py)."""
    return str(route.get("provider") or getattr(parent, "provider", "") or "")
