"""The progress watchdog's Hermes wiring for keep-going (loaded by path from ``__init__``).

``register(ctx, settings, core)`` adds the watchdog hooks and returns the binder that marks a continuation
with the run that queued it; ``core`` is the keep-going module's namespace (its lock, per-session state,
clock and host seams), read at use so a test's or host's rebinding holds. ``effects.HermesEffects`` carries
out the watchdog's side effects for one Hermes session.

The run boundary goes through Hermes's hooks and fails closed (Astra review round 2). A run is admitted in
``pre_llm_call``: a stopped, expired or checkpointed task gets a veto (Hermes then makes no model request)
until genuine user input, except that a checkpoint admits the one continuation bound to it, its new run.
Only text that is exactly the current standalone continuation counts as one; a continuation merged with
other input is dropped and the rest is judged as input. Every tool call of a stopped run, or of a run past
its checkpoint, is refused in ``pre_tool_call``, for the owning run's delegated children too. The person
gets the stop report itself, posted to the session, or as the next turn's veto when posting fails. A
compaction that rotates the session id (``post_compaction``) carries the task and a child's parent along.
Each admission, stop, checkpoint and binding is stored in the task record (``runs``): a restart keeps the
stop, whether the person has seen its report, the checkpoint and the continuation bound to it.
Hermes has no hook that vetoes a model request inside a run, so a stopped run's own remaining requests end
when the model ends its turn (its tools are refused, the watchdog keeps counting).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from typing import Any, Callable

logger = logging.getLogger("keep-going.wiring")

MUTATING_TOOLS = frozenset({"write_file", "patch"})
WIND_DOWN = (" The run budget checkpoint was reached: call no more tools in this run. End your turn now with a "
             "short status; the task continues in a new run from the handoff and your todo list.")
STALE = " This continuation was queued by an earlier run and newer input has overtaken it; it was not run."
HELD = (" The task is at its run budget checkpoint: it continues in a new run from its own continuation, or "
        "when you send a message.")
RESEND = " Your message was not run; send it again to continue."
DROPPED = ("[keep-going] The keep-going continuation in this message was overtaken by newer input: ignore it "
           "and answer the rest.")
# A continuation as ``bind`` writes it: keep-going's prefix up to its run token.
CONTINUATION = re.compile(r"\[keep-going\][\s\S]*?\n\[keep-going continuation [0-9a-f]{12}\]")
TOKEN = re.compile(r"\[keep-going continuation ([0-9a-f]{12})\]")
EXCERPT_CHARS = 170  # with its digest, a buffered result stays within the watchdog's 200-character quote


def _binding(text: str) -> str | None:
    """What identifies a bound continuation in the task record: its run token and a digest of all of it."""
    tokens = TOKEN.findall(text)
    return "%s:%s" % (tokens[-1], hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()) if tokens else None


def _restore(st: dict, w: dict, record: dict) -> None:
    """A stored task record back into a session's state after a restart."""
    w["task_started"] = record["started"]
    if record["report"]:
        st["halted"] = record["report"]
        if record["unseen"]:
            st["undelivered"] = record["report"]
    if record["checkpoint"]:
        st["checkpoint"] = True
    if record["continuation"]:
        st["continuation"] = record["continuation"]


def _without_continuations(message):
    """``message`` with the continuations taken out of its text, its other content parts kept as they are."""
    if isinstance(message, list):
        return [dict(p, text=CONTINUATION.sub("", p["text"])) if isinstance(p, dict) and isinstance(p.get("text"), str)
                else p for p in message]
    return CONTINUATION.sub("", str(message or ""))


def register(ctx, settings: dict, core: Any) -> Callable[[str, str], str | None]:
    watchdog, runs, sanitize, fx = core._load("watchdog"), core._load("runs"), core._load("sanitize"), core._load("effects")
    stuck_turns = int(core._positive(settings.get("stuck_turns"), 8))
    stuck_seconds = core._positive(settings.get("stuck_seconds"), 1800)
    script = settings.get("handoff_script")
    script = script if isinstance(script, str) and script else None

    parents: dict[str, str] = {}  # ponytail: delegated child -> parent session, one entry per child for the process

    def bind(session_id: str, message: str) -> str | None:
        """``message`` marked as the continuation the session's next run must carry; it overtakes older ones.
        None after a vetoed turn: Hermes still ends it with ``post_llm_call``, and its veto must not replace
        the continuation it held back (a STALE/HELD loop)."""
        text = "%s\n[keep-going continuation %s]" % (message, uuid.uuid4().hex[:12])
        with core._lock:
            st = core._state(session_id)
            if st.get("vetoed"):
                return None
            st["continuation"] = _binding(text)
        persist(session_id)
        return text

    def persist(session_id: str) -> None:
        """The session's task record, as it is in memory, stored under its task key."""
        with core._lock:
            w, st = core._watch.get(session_id, {}), core._sessions.get(session_id, {})
            if "task_started" not in w:
                return
            record = {"started": w["task_started"], "report": st.get("halted"), "unseen": bool(st.get("undelivered")),
                      "checkpoint": bool(st.get("checkpoint")), "continuation": st.get("continuation")}
            runs.store(runs.task_key(session_id), record)  # in order: an older snapshot never lands last

    def veto(message: str) -> dict:
        return {"action": "block", "message": message}

    def on_run_start(*, session_id="", user_message="", parent_session_id="", platform="", conversation_history=None,
                     **kwargs):
        if parent_session_id and session_id:
            with core._lock:
                parents[session_id] = parent_session_id
                report = next((core._sessions[s]["halted"] for s in owners(session_id)
                               if core._sessions.get(s, {}).get("halted")), None)
            return veto(core._STOPPED.format(report=report)) if report else None
        if not session_id:
            return
        verdict = admit(session_id, user_message, platform, conversation_history)
        with core._lock:
            if verdict and verdict.get("action") == "block":
                core._state(session_id)["vetoed"] = kwargs.get("turn_id") or True
            else:
                core._state(session_id).pop("vetoed", None)
        persist(session_id)  # what the admission changed outlives a restart
        return verdict

    def admit(session_id, user_message, platform, conversation_history):
        budget, now, key = core._run_budget_seconds(), core._now(), runs.task_key(session_id)
        found = CONTINUATION.search(runs._text(user_message)) is not None
        others = _without_continuations(user_message) if found else None  # every other part, images included
        rest = runs._text(others).strip() if found else None
        with core._lock:
            st, w = core._state(session_id), core._watch.setdefault(session_id, {})
            if "task_started" not in w:  # a restart reads the task back
                _restore(st, w, runs.task_state(key, now, restart=False))
            ours = found and not rest and isinstance(user_message, str)
            binding = st.get("continuation") or ""
            current = ours and _binding(user_message) == binding
            merged = found and not ours and bool(binding) and binding.split(":")[0] in TOKEN.findall(
                runs._text(user_message))
            if current or merged:
                st["continuation"] = None  # consumed, or overtaken by the input it came with (its own token only)
        if ours and not current:
            return veto(core.PREFIX + STALE)
        real = not ours and runs.genuine(others if found else user_message, platform, conversation_history)
        kanban = os.environ.get("HERMES_KANBAN_TASK") or None
        with core._lock:
            st, w = core._state(session_id), core._watch.setdefault(session_id, {})
            if real:  # a genuine message starts a new task
                w["task_started"] = runs.task_state(key, now, restart=True)["started"]
                st["continuation"], st["halted"] = None, None  # the person's input overtakes and lifts
                st.pop("checkpoint", None)
                st.pop("handoff_failure", None)
                unseen = st.pop("undelivered", None)
                if unseen:
                    return veto(fx.REPORT.format(report=unseen) + RESEND)
            elif st.get("halted"):
                st.pop("undelivered", None)  # this veto delivers it
                return veto(fx.REPORT.format(report=st["halted"]))
            elif st.get("checkpoint"):
                if not current and now - w["task_started"] < watchdog.DAY_SECONDS:  # past 24 h it stops below
                    return veto(core.PREFIX + HELD)
                st.pop("checkpoint", None)  # its continuation opens the new run
            effects = fx.HermesEffects(ctx, session_id, script, core, persist)
            dog = watchdog.Watchdog(
                clock=lambda: core._now(), effects=effects, run_budget_seconds=budget, stuck_turns=stuck_turns,
                stuck_seconds=stuck_seconds, kanban_task_id=kanban, task_started_at=w["task_started"])
            w.update(results=[], mutated=False, first=True, effects=effects, dog=dog, kanban=kanban)
            total = now - w["task_started"]
        context = {"context": DROPPED} if found and not ours else None
        if total < watchdog.DAY_SECONDS:
            return context
        report = expire(w, total, "when this run began")  # already over: refused before its first request
        with core._lock:
            core._state(session_id).pop("undelivered", None)
        return veto(fx.REPORT.format(report=report))

    def expire(w: dict, total: float, when: str) -> str:
        """Stop the run of ``w`` at the 24 h limit outside the watchdog's own turns; the report."""
        w["dog"].stopped = True
        report = "Stopped: the task has run %.1f h in total (limit 24 h) %s." % (total / 3600, when)
        w["effects"].stop(report)
        if w.get("kanban"):
            w["effects"].block_task(w["kanban"], report)
        return report

    def on_tool(*, tool_name="", result="", session_id="", args=None, **kwargs):
        """A digest and a bounded excerpt of the turn's reportable event (path and result, redacted whole)."""
        path = args.get("path") if isinstance(args, dict) else None
        text = sanitize.decoded(core._redacted, result if isinstance(result, str) else repr(result))
        event = core._redacted("%s %s: %s" % (tool_name, core._redacted(path), text) if isinstance(path, str) and path
                               else text)
        flat = " ".join(event.split())
        quoted = "%s%s [#%s]" % (flat[:EXCERPT_CHARS], " …" if len(flat) > EXCERPT_CHARS else "",
                                 hashlib.sha1(event.encode("utf-8", "replace")).hexdigest()[:16])
        with core._lock:
            w = core._watch.get(session_id)
            if w is None or "dog" not in w:  # no run admitted yet (a stopped task after a restart)
                return
            w["results"].append(quoted)
            w["mutated"] = w["mutated"] or (tool_name in MUTATING_TOOLS and core._succeeded(result))

    def on_request(*, session_id="", **kwargs):
        """Each model request after the run's first one ends a turn: hand that turn's signals over. A run
        winding down after its checkpoint makes no progress by design (its tools are refused), so only its
        24 h limit is checked until it ends."""
        with core._lock:
            w = core._watch.get(session_id)
            if w is None or "dog" not in w:  # no run admitted yet (a stopped task after a restart)
                return
            ends_turn = not w.pop("first", False)
            winding = core._state(session_id).get("checkpoint") and not w["dog"].stopped
            signals = {"tool_results": tuple(w["results"]), "file_mutated": w["mutated"],
                       "todo_revision": sanitize.revision(core._sessions.get(session_id, {}).get("revision"))}
            w["results"], w["mutated"] = [], False
            total = core._now() - w["task_started"]
        if winding:
            if total >= watchdog.DAY_SECONDS:
                expire(w, total, "while the run wound down after its checkpoint")
        elif ends_turn:
            w["dog"].on_turn(**signals)  # outside the lock: the effects take it

    def owners(session_id: str) -> list[str]:
        """The session and the runs it was delegated from, nearest first (core lock held)."""
        chain = [session_id]
        while chain[-1] in parents and parents[chain[-1]] not in chain:  # every ancestor, a cycle ends it
            chain.append(parents[chain[-1]])
        return chain

    def on_tool_call(*, session_id="", **kwargs):
        with core._lock:
            states = [core._sessions.get(s, {}) for s in owners(session_id)]
            report = next((st["halted"] for st in states if st.get("halted")), None)
            winding = any(st.get("checkpoint") for st in states)
        if report:
            return veto(core._STOPPED.format(report=report))
        return veto(core.PREFIX + WIND_DOWN) if winding else None

    def on_compaction(*, session_id="", old_session_id="", **kwargs):
        """A compaction that rotated the session id: the task, its stop and the running watchdog move along."""
        if not session_id or not old_session_id or session_id == old_session_id:
            return
        with core._lock:
            if old_session_id in parents:  # a delegated child keeps its parent across the rotation
                parents[session_id] = parents[old_session_id]
            if old_session_id in core._children:  # and stays a child: it is never kept going
                core._children.add(session_id)
            w = core._watch.get(old_session_id)
            cold = w is None or "task_started" not in w  # e.g. a manual /compress right after a restart
            if not cold:
                core._watch[session_id] = w
                core._sessions[session_id] = core._state(old_session_id)
                if w.get("effects") is not None:
                    w["effects"].session_id = session_id
        if runs.task_key(session_id) == runs.task_key(old_session_id):
            return
        if cold:  # the stored record moves along, in memory first: a failed write keeps the stop
            record = runs.task_state(runs.task_key(old_session_id), core._now(), restart=False)
            with core._lock:
                _restore(core._state(session_id), core._watch.setdefault(session_id, {}), record)
        persist(session_id)

    ctx.register_hook("pre_llm_call", on_run_start)
    ctx.register_hook("post_compaction", on_compaction)
    ctx.register_hook("post_tool_call", on_tool)
    ctx.register_hook("pre_api_request", on_request)
    ctx.register_hook("pre_tool_call", on_tool_call)
    return bind


