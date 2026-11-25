"""The parts of ``SubagentsAPI`` that tie it to the registered plugin (SA2-WIRE; REQ-031, I-034, I-050), kept out of
api.py for size.

* An API spawn is staged through the plugin's own ``pre_tool_call`` chain (enforcement, then names) with the parent's
  session and turn and the run id as its call id, so the definition body reaches the child's context, the name is
  checked and recorded, and a child that may not spawn is refused, exactly as for a model's call.
* A background API run settles at its child's ``subagent_stop`` (stock fires it with the child's session, status and
  summary, tools/delegate_tool_results.py:381-388): ``done`` or ``failed``, its line and its step file.
* ``stop`` and ``resume`` are stock ``delegate_task(action="stop"|"continue")`` for the turn's agent; a resume runs in
  the foreground and returns the agent's answer, its slot taken in the one limiter first.
"""

from __future__ import annotations

import copy
import json
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from .callers import Spawn, active_parent, agent_ref
from .messages import _marked
from .stockcalls import SPAWN

NO_HERMES_CWD = ("Not started (REQ-027): a Hermes child cannot be placed in a folder yet, stock seeds its cwd from "
                 "the parent's terminal record, which may be another folder; cwd %s")
STOPS_KEPT = 256  # stops of children whose run did not know its subagent id yet
OK = ("completed", "done", "ok", "success")


class ApiControls:
    """Mixed into ``SubagentsAPI``: staging through the plugin's hooks, settling at a stop, stop and resume."""

    _pre_tool_call: Callable[..., Any] | None = None

    def _stage(self, run: dict, task: dict, agent: Any, parent: Any, session: str, turn: str) -> tuple[dict, Any]:
        """Stages the run's one-task call; ``(final task, refusal or None)``. The API keeps the full answer (schema
        True: never shortened) and an explicit ``root_task`` counts for the slot."""
        calls = self._launcher.calls
        mod = __import__(type(calls).__module__, fromlist=["Call"])  # the launcher's own load: enforce checks Slot
        root = run["root_task"] or ""
        if self._pre_tool_call is None:  # an API built on its own parts: no plugin hooks to go through
            calls.stage(mod.Call(session, turn, run["id"], [mod.Slot(0, task, Spawn(
                agent, run["name"], True, agent_ref(parent), root=root))]))
            return task, None
        verdict = self._pre_tool_call(tool_name=SPAWN, args={"tasks": [task]}, session_id=session, turn_id=turn,
                                      tool_call_id=run["id"])
        if isinstance(verdict, dict) and verdict.get("action") == "block":
            calls.drop(session, run["id"])
            return task, verdict.get("message") or "refused by the plugin's spawn checks"
        if isinstance(verdict, dict) and isinstance((verdict.get("args") or {}).get("tasks"), list):
            task = verdict["args"]["tasks"][0]
        call = calls.find(session, run["id"])
        for slot in call.slots if call is not None else ():
            slot.spawn = replace(slot.spawn, agent=slot.spawn.agent or agent, schema=True, root=root)
        return task, None

    def _vet(self, run: dict) -> str | None:
        """A harness spawn passes the plugin's caller and name checks as a Hermes one does; no stock call stays
        staged for it. The refusal, or None."""
        if self._pre_tool_call is None:
            return None
        parent = active_parent()
        session, task = str(getattr(parent, "session_id", "") or ""), {"goal": run["prompt"],
                                                                         "agent_type": run["agent_type"]}
        if run["name"]:
            task["name"] = run["name"]
        verdict = self._pre_tool_call(tool_name=SPAWN, args={"tasks": [task]}, session_id=session,
                                      turn_id=str(getattr(parent, "_current_turn_id", "") or ""),
                                      tool_call_id=run["id"])
        self._launcher.calls.drop(session, run["id"])
        if isinstance(verdict, dict) and verdict.get("action") == "block":
            return verdict.get("message") or "refused by the plugin's spawn checks"
        return None

    def _running(self, run: dict, subagent_id: Any, line: str) -> Any:
        """A background run whose child runs now; a stop that came first settles it at once. Id, status and line are
        written under the lock that drains early stops, so a stop landing meanwhile finds the run running."""
        from .api import AgentResult, _one_line

        with self._runs.lock:
            run["subagent_id"] = subagent_id
            early = self._runs.stopped.pop(str(subagent_id), None) if subagent_id else None
            if early is None:
                run["status"] = "running"
                run["line"] = _one_line("%s running: %s" % (run["name"] or run["id"], _one_line(line)))
                return AgentResult(run["line"], "running", None, subagent_id)
        return self._keep(run, *settled(run, *early))

    def _settle(self, subagent_id: str, status: Any, summary: Any) -> None:
        with self._runs.lock:
            run = next((r for r in self._runs.items if r["background"] and r["status"] == "running"
                        and r["subagent_id"] == subagent_id), None)
            if run is None:
                self._runs.stopped[subagent_id] = (status, summary)
                while len(self._runs.stopped) > STOPS_KEPT:
                    self._runs.stopped.pop(next(iter(self._runs.stopped)))
                return
        self._keep(run, *settled(run, status, summary))

    def stop(self, subagent_id: str) -> None:
        """Stops agent ``subagent_id`` of the turn's agent (stock ``action="stop"``); RuntimeError if stock refused."""
        reply = self._control({"action": "stop", "subagent_id": subagent_id})
        if "error" in reply:
            raise RuntimeError(reply["error"])

    def resume(self, subagent_id: str, text: str, sender: str, *, schema: dict | None = None) -> Any:
        """Continues an idle agent with ``text`` (marked with its sender, never user approval) and returns its answer
        as an ``AgentResult``; ``failed`` when the schema is unusable, no slot is free or stock refused."""
        from tools.delegation_output_schema import coerce_output_schema

        schema, unusable = coerce_output_schema(copy.deepcopy(schema))  # stock checks a spawn's, never a continue's
        run = {"id": "api-%s" % uuid.uuid4().hex[:12], "agent_type": "continue", "name": None, "prompt": text,
               "root_task": None, "model": None, "effort": None, "background": False, "status": "queued", "line": "",
               "subagent_id": subagent_id, "step_file": None, "lease": None, "schema": schema}
        with self._runs.lock:
            self._runs.items.append(run)
        if unusable:
            return self._finish(run, "failed", unusable)
        session = str(getattr(active_parent(), "session_id", "") or "")
        refusal = self._launcher.reserve(subagent_id, session, run["id"])
        if refusal:
            return self._finish(run, "failed", refusal)
        outcome: Any = ""
        try:
            outcome = self._control({"action": "continue", "subagent_id": subagent_id, "background": False,
                                     "message": _marked(text, sender), "output_schema": schema})
        finally:
            self._launcher.end_continue(run["id"], json.dumps(outcome), session)
        return self._answer(run, outcome)

    def _control(self, call: dict) -> dict:
        import tools.delegate_tool as delegate_tool

        parent = active_parent()
        if parent is None:
            return {"error": "no agent turn is active: stock controls agents only for their parent agent"}
        try:
            outcome = delegate_tool.delegate_task(**{k: v for k, v in call.items() if v is not None},
                                                  parent_agent=parent)
            data = json.loads(outcome) if isinstance(outcome, str) else outcome
        except Exception as exc:  # stock refused; the caller is told
            return {"error": "%s: %s" % (type(exc).__name__, exc)}
        return data if isinstance(data, dict) else {"error": "delegate_task answered %r" % outcome}


def schema_errors(schema: Any, answer: str) -> list[str]:
    """Why ``answer`` does not match the requested ``schema`` (stock's own validator); [] when it does or when no
    schema was asked. Stock checks a spawn's answer, never a continue's, and a stop hook carries no verdict."""
    if not isinstance(schema, dict):
        return []
    from tools.delegation_output_schema import validate_output

    valid, errors = validate_output(answer, schema)
    return [] if valid else (list(errors) or ["the answer does not match the schema"])


def with_errors(errors: Any, answer: str) -> str:
    return "schema errors: %s\n\n%s" % ("; ".join(map(str, errors)), answer)


def settled(run: dict, status: Any, summary: Any) -> tuple[str, str, str]:
    """A background run's end at its child's stop: (status, line, answer); ``done`` only for a finished child whose
    answer matches the run's schema."""
    answer = str(summary or "") or "the agent stopped with status %s" % (status or "unknown")
    if str(status or "") not in OK:
        return "failed", answer, answer
    errors = schema_errors(run.get("schema"), answer)
    if errors:
        answer = with_errors(errors, answer)
        return "failed", answer, answer
    return "done", answer, answer


def stopped(callers: Any) -> Callable[..., None]:
    """The ``subagent_stop`` hook: the background API run of that child settles; any other child's stop is ignored."""

    def subagent_stop(child_session_id: str = "", child_status: Any = None, child_summary: Any = None,
                      **_: Any) -> None:
        child = callers.get(child_session_id) if child_session_id else None
        if child is None or not child.subagent_id:
            return
        from .api import SubagentsAPI

        SubagentsAPI()._settle(child.subagent_id, child_status, child_summary)

    return subagent_stop


def spawn_folder(session_cwd: Callable[[], str], cwd: str | None, hermes: bool) -> tuple[str, str]:
    """(the folder a spawn runs in, a refusal or ""). ``cwd`` is absolute or relative to the session workspace, an
    existing folder inside it; a Hermes child gets "" (stock starts it in the parent's cwd), and an explicit ``cwd``
    for it is refused until REQ-027: its real start folder cannot be guaranteed, not even for the workspace root."""
    if cwd is None:
        return ("" if hermes else session_cwd()), ""
    workspace = session_cwd()
    if not workspace:
        return "", "Not started: cwd %s needs a session workspace, and this session has none" % cwd
    root = Path(workspace).resolve()
    folder = (root / cwd).resolve()
    if not folder.is_relative_to(root):
        return "", "Not started: cwd %s is outside the session workspace %s" % (cwd, root)
    if not folder.is_dir():
        return "", "Not started: cwd %s is not an existing folder" % cwd
    if hermes:
        return "", NO_HERMES_CWD % cwd
    return str(folder), ""
