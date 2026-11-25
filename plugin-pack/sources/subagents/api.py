"""The agent API other plugins build on (REQ-031, REQ-032; design D-11/S6, D-22): workflow-engine, ultracode and
kanban-heartbeat spawn, message and list agents through ``SubagentsAPI`` instead of spawning on their own.

``spawn`` makes a typed stock ``delegate_task`` call (``agent_type``/``name``/``model_alias``/``effort``) through this
plugin's DelegationProvider, staged in the plugin's calls like a model's call, so its route admits it atomically in the
one limiter (``limiter()`` is ``limiter.get_limiter()``; run, queue or refuse at the root conversation's cap) and its
child keeps that slot until ``subagent_stop``. A ``claude-code:``/``codex:``/``pi:`` model runs through harness-runner
(``run_step``) in the calling session's workspace, its slot taken here (root: ``root_task``, else the caller's root
conversation), or fails "harness-runner not installed". A queued spawn never starts by itself: when its slot frees
its run turns ``promoted``. A finished run's full answer is in its step file. With no arguments the API uses the parts
the registered plugin publishes (``PluginParts``), so a named agent it spawns can be messaged with its history.
``send`` is the phase 1 delivery (messages.py): steer a running agent, continue an idle one with its transcript, the
text marked with its sender and never user approval (AGR-025).

Callers without a parent session (cron, socket, gateway outside a turn) cannot spawn through stock yet: it refuses a
call with no parent agent, so the spawn ends ``failed``; inside any agent turn (chat, cron job agent, kanban worker)
the turn's agent is the parent. ``send`` needs none: ``session=`` names the conversation when no turn is active.
"""

from __future__ import annotations

import contextvars
import copy
import importlib
import json
import logging
import sys
import threading
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Literal

from .api_hooks import ApiControls, schema_errors, spawn_folder, with_errors
from .api_step import runner_class, step_fields
from .callers import CallerMap, active_parent
from .calls import Calls
from .kanban_store import get_store
from .launch import LEDGER, Launcher, QueuedLaunch
from .limiter import QUEUED, REFUSED, Limiter, get_limiter, process_shared
from .messages import Messenger
from .names import Names
from .provider import HARNESS_PREFIXES
from .registry import AgentDef, UnknownAgentType
from .results import _step_file
from .stockcalls import bound_key, nothing_ran
from .workspaces import Registries

logger = logging.getLogger("subagents.api")

NOT_INSTALLED = "harness-runner not installed: agent %s needs the %s harness (model %s)"
NO_WORKSPACE = "no workspace: agent %s runs in the calling session's workspace and this session has none"
LINE_LIMIT = 200


@dataclass(frozen=True)
class AgentResult:
    line: str                      # one line
    status: Literal["done", "failed", "queued", "running", "promoted"]
    step_file: Path | None         # starts with "## Summary"
    subagent_id: str | None


@dataclass
class Runs:
    """Every API run of the process, oldest first; shared by every caller like the limiter."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    items: list[dict[str, Any]] = field(default_factory=list)
    stopped: dict[str, tuple[Any, Any]] = field(default_factory=dict)  # a child's stop its run had no id for yet


@dataclass
class PluginParts:
    """The registered plugin's Messenger, Launcher, workspace callable and spawn hook chain (enforcement, then names)
    (``process_shared("api_parts")``)."""

    messenger: Any = None
    launcher: Any = None
    cwd: Callable[[], str] | None = None
    pre_tool_call: Callable[..., Any] | None = None


def _session_cwd() -> str:
    """The calling session's workspace (stock), "" when it has none; never the gateway process's own cwd."""
    try:
        from hermes_cli.plugin_session import session_cwd
    except ImportError:
        return ""
    return session_cwd() or ""


def _one_line(text: Any) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= LINE_LIMIT else line[:LINE_LIMIT - 1] + "…"


class SubagentsAPI(ApiControls):
    """spawn / send / stop / resume / status / limiter for other plugins (design.md § Interfaces)."""

    def __init__(self, *, registry: Any = None, messenger: Messenger | None = None, launcher: Launcher | None = None,
                 cwd: Callable[[], str] | None = None):
        from hermes_constants import get_hermes_home

        parts = process_shared("api_parts", dict).get(str(get_hermes_home())) or PluginParts()  # one per profile
        self._cwd = cwd or parts.cwd or _session_cwd
        self.registry = registry if registry is not None else Registries(self._cwd)
        self._launcher = launcher or parts.launcher or Launcher(get_limiter(), CallerMap(), Calls())
        if launcher is None and parts.launcher is not None:  # the plugin's own parts: its spawn hooks too
            self._pre_tool_call = getattr(parts, "pre_tool_call", None)
        callers = self._launcher.callers
        self.messenger = messenger or parts.messenger or Messenger(Names(get_store(), callers), callers,
                                                                   slots=self._launcher)
        self._runs: Runs = process_shared("api_runs", Runs)

    def limiter(self) -> Limiter:
        return get_limiter()

    def status(self, root_task: str | None = None) -> list[dict]:
        with self._runs.lock:
            return [{**run, "schema": copy.deepcopy(run.get("schema"))} for run in self._runs.items
                    if root_task is None or run["root_task"] == root_task]

    def send(self, to: str, text: str, sender: str, *, session: str | None = None) -> None:
        """``text`` to agent ``to`` of ``session`` (default: the turn's, else the only one); RuntimeError if not."""
        parent = active_parent()
        where = session or str(getattr(parent, "session_id", "") or "") or self.messenger.names.sole_root()
        reply = self.messenger.deliver(to, text, sender, where)
        if "error" in reply:
            raise RuntimeError(reply["error"])

    def spawn(self, agent_type: str, prompt: str, *, name: str | None = None, background: bool = False,
              model: str | None = None, effort: str | None = None, schema: dict | None = None,
              root_task: str | None = None, cwd: str | None = None) -> AgentResult:
        schema = copy.deepcopy(schema)  # the run's contract: the caller's dict changed later never loosens it
        run = {"id": "api-%s" % uuid.uuid4().hex[:12], "agent_type": agent_type, "name": name, "prompt": prompt,
               "root_task": root_task, "model": model, "effort": effort, "background": background,
               "status": "queued", "line": "", "subagent_id": None, "step_file": None, "lease": None, "schema": schema}
        with self._runs.lock:
            self._runs.items.append(run)
        try:
            agent = self.registry.find(agent_type.strip())
        except UnknownAgentType as exc:
            return self._finish(run, "failed", str(exc))
        chosen = (model or agent.model or "").strip()
        harness = chosen.split(":", 1)[0] if chosen.lower().startswith(HARNESS_PREFIXES) else None
        workspace, refusal = spawn_folder(self._cwd, cwd, harness is None)
        if refusal:
            return self._finish(run, "failed", refusal)
        if harness is None:
            return self._delegate(run, agent, schema)
        refusal = self._vet(run)
        if refusal:
            return self._finish(run, "failed", refusal)
        if runner_class() is None:
            return self._finish(run, "failed", NOT_INSTALLED % (agent.name, harness, chosen))
        if not workspace:
            return self._finish(run, "failed", NO_WORKSPACE % agent.name)
        session = str(getattr(active_parent(), "session_id", "") or "")
        run["root_task"] = root = root_task or (self._launcher.callers.root_of(session) if session else "subagents-api")
        verdict = self._admit(run, harness, root, session)
        if verdict == REFUSED:
            return self._finish(run, "failed", "Not started: root task %s has reached the limit of %d agents."
                                % (root, get_limiter().per_root))
        if verdict == QUEUED:
            return self._queued(run, run["id"])
        if not background:
            return self._harness(run, agent, harness, chosen, schema, workspace)
        started = self._result(run, "running", "%s running in the %s harness" % (run["id"], harness))
        threading.Thread(target=contextvars.copy_context().run, args=(self._harness, run, agent, harness, chosen,
                         schema, workspace), name="subagents-api-%s" % run["id"], daemon=True).start()
        return started

    def _admit(self, run: dict, provider: str, root_task: str, session: str) -> str:
        """The run's slot in the one limiter; a queued run is published so whichever load frees a slot tells it."""
        item = QueuedLaunch(run["id"], session, run["prompt"],
                            str(run["name"] or run["agent_type"])[:80], self._promoted, contextvars.copy_context(),
                            bound_key())
        with LEDGER.lock:  # as launch.Launcher.admit: published before the limiter can promote it
            LEDGER.queued[run["id"]] = item
            verdict = get_limiter().acquire(run["id"], provider=provider, root_task=root_task)
            if verdict != QUEUED:
                del LEDGER.queued[run["id"]]
        return verdict

    def _promoted(self, item: QueuedLaunch) -> None:
        with self._runs.lock:
            run = next((r for r in self._runs.items if item.launch_id in (r["id"], r["lease"])), None)
            if run is not None and run["status"] == "queued":
                run.update(status="promoted", line="%s: a slot is free; not started, spawn it again" % item.launch_id)

    def _delegate(self, run: dict, agent: AgentDef, schema: dict | None) -> AgentResult:
        task = {"goal": run["prompt"], "agent_type": run["agent_type"]}
        task.update({key: run[src] for key, src in (("name", "name"), ("model_alias", "model"), ("effort", "effort"))
                     if run[src]})
        import tools.delegate_tool as delegate_tool
        parent = active_parent()
        session, calls = str(getattr(parent, "session_id", "") or ""), self._launcher.calls
        mod = sys.modules[type(calls).__module__]  # the launcher's own load: its CALL narrows the routes
        task, refusal = self._stage(run, task, agent, parent, session, str(getattr(parent, "_current_turn_id", "")
                                                                                or ""))
        if refusal:
            return self._finish(run, "failed", refusal)
        token = mod.CALL.set((session, run["id"]))  # stock routes and starts inside delegate_task: only this call's slot matches
        try:
            outcome = delegate_tool.delegate_task(tasks=[task], background=run["background"], output_schema=schema,
                                                  parent_agent=parent)
        except Exception as exc:  # stock refused the call; the caller is told, nothing runs
            outcome = json.dumps({"error": "%s: %s" % (type(exc).__name__, exc)})
        finally:
            mod.CALL.reset(token)
        text = outcome if isinstance(outcome, str) else json.dumps(outcome, default=str)
        noted = self._launcher.tool_result(text, session, run["id"])  # the call's end: unstarted slot freed
        calls.drop(session, run["id"])  # a call stock refused before routing it
        try:
            data = json.loads(noted or text)
        except ValueError:
            data = None
        return self._answer(run, data if isinstance(data, dict) else outcome)

    def _answer(self, run: dict, data: Any) -> AgentResult:
        """A spawn's or continue's stock answer: queued, failed, running in the background, or done with its entry."""
        if not isinstance(data, dict):
            return self._finish(run, "failed", "delegate_task answered %r" % data)
        if data.get("status") == "queued" and data.get("queued"):
            return self._queued(run, str(data["queued"][0].get("launch_id")))
        if nothing_ran(json.dumps(data)):
            return self._finish(run, "failed", data.get("error"))
        entries = data.get("results") if isinstance(data.get("results"), list) else []
        entry = entries[0] if entries and isinstance(entries[0], dict) else None
        if entry is None:
            return self._running(run, next(iter(data.get("subagent_ids") or []), None),
                                 "%s running (%s)" % (run["id"], data.get("delegation_id") or "started"))
        run["subagent_id"] = entry.get("subagent_id") or run["subagent_id"]
        ok = str(entry.get("status") or "completed") in ("completed", "done", "ok", "success")
        answer, errors = str(entry.get("summary") or entry.get("error") or ""), entry.get("schema_errors") or []
        if ok and "schema_valid" not in entry:  # stock checked no schema (a continue): checked here
            errors = schema_errors(run.get("schema"), answer)
        if entry.get("schema_valid") is False or errors:
            ok, answer = False, with_errors(errors, answer)
        return self._keep(run, "done" if ok else "failed", answer, answer)

    def _queued(self, run: dict, lease: str) -> AgentResult:
        """A queued spawn: its promotion turns this run ``promoted`` (the API's caller reads it in ``status``)."""
        run["lease"] = lease
        with LEDGER.lock:
            item = LEDGER.queued.get(lease)
            if item is not None:
                LEDGER.queued[lease] = replace(item, promoted=self._promoted)
        if item is None:  # only Launcher._start removes a queued lease: promoted before its callback was swapped
            self._promoted(SimpleNamespace(launch_id=lease))
        return self._finish(run, "queued", "Queued as %s (position %s) by the global agent limiter; not started."
                            % (lease, get_limiter().position(lease)))

    def _harness(self, run: dict, agent: AgentDef, harness: str, model: str, schema: dict | None,
                 workspace: str) -> AgentResult:
        try:
            module = importlib.import_module("harnessrunner")
            runner = runner_class(module)()
            fields = step_fields(run, agent, harness, model, schema, workspace)
            result = runner.run_step(getattr(module, "StepSpec", SimpleNamespace)(**fields))  # protocol is structural
        except Exception as exc:  # a refused or broken step is the run's failure, never the caller's crash
            return self._finish(run, "failed", "%s harness: %s: %s" % (harness, type(exc).__name__, exc))
        finally:  # the slot freed (the root's count stays); launches it lets in start as at any release
            self._launcher.promote(get_limiter().release(run["id"]))
        run["subagent_id"] = getattr(result, "session_id", None)
        errors = tuple(getattr(result, "schema_errors", ()) or ())
        status = "done" if getattr(result, "exit_status", 1) == 0 and not errors else "failed"
        from tools.delegation_redaction import redact_child_value  # as harness-runner's tool and stock results

        reply, structured, errors = redact_child_value([str(getattr(result, "reply", "") or ""),
                                                        getattr(result, "structured", None), list(errors)])
        answer = reply if structured is None else "%s\n\n```json\n%s\n```" % (
            reply, json.dumps(structured, indent=2, ensure_ascii=False, default=str))
        line = "schema errors: %s; %s" % ("; ".join(map(str, errors)), reply) if errors else reply
        return self._keep(run, status, line, answer)

    def _keep(self, run: dict, status: str, line: Any, answer: str) -> AgentResult:
        """A finished run: its full answer in a step file ("## Summary" first, 0600, pruned), its one line returned."""
        try:
            run["step_file"] = _step_file(str(run["name"] or run["id"]), status, run["prompt"], answer)
        except OSError as exc:
            return self._finish(run, "failed", "step file not written: %s" % exc)
        return self._finish(run, status, line)

    def _finish(self, run: dict, status: str, text: Any) -> AgentResult:
        if status == "failed":
            logger.warning("subagents: API run %s (%s) failed: %s", run["id"], run["agent_type"], _one_line(text))
        return self._result(run, status, "%s %s: %s" % (run["name"] or run["id"], status, _one_line(text)))

    def _result(self, run: dict, status: str, line: str) -> AgentResult:
        with self._runs.lock:
            if not (status == "queued" and run["status"] == "promoted"):  # promoted while it was being queued
                run["status"], run["line"] = status, _one_line(line)
            status, line, step = run["status"], run["line"], run["step_file"]
        return AgentResult(line, status, Path(step) if step else None, run["subagent_id"])

