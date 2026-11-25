"""The Hermes agent host (workflow-engine Task 6: REQ-007, REQ-009, REQ-028, REQ-029, REQ-030).

``HermesAgentHost(api=None, find=None, root_task="workflows", gate_timeout=None, cwd=None)`` is the ``AgentHost`` that
``WorkflowEngine(host=None)`` uses (engine.py). Every agent runs through hermes-subagents' ``SubagentsAPI``
(decisions and evidence: workflows/DELEGATION.md). ``api`` is a factory ``cwd -> SubagentsAPI`` (default the
real one, imported at the first spawn); ``find`` is ``find(name, cwd) -> Path | None`` (default
``workflows.saved.find``, imported at call time); ``cwd`` is the workspace of gates and ``load`` (default the session's;
with neither they fail, never this process's cwd). ``root_task`` for the limiter is ``req.runId``, else the
constructor's. Called from bridge threads; results mirror runner/main.mjs:

  ``spawn(req)``  -> ``{ok: True, text}`` | ``{ok: False, error}`` | ``{ok: False, skipped: True, error}``
  ``resume(agent_id, prompt)`` -> as spawn: a fresh spawn carrying the agent's last answer
  ``abort(agent_id)`` -> None at once; the spawn of that id is cancelled, even one not registered yet
  ``gate(command, cwd)`` -> ``{ok, output}`` (workflows/gates.py)
  ``load(ref)``   -> ``{ok: True, script}`` | ``{ok: False, message}``

Agent ids (``<run id>/wf-agent-<n>``) are opaque here. Every text, error and gate output returned passes
through Hermes' stock redactor, as engine.py's summary does.
"""

from __future__ import annotations

import contextvars
import importlib
import json
import threading
from pathlib import Path
from typing import Any, Callable

from . import gates, mbteam_roles
from .engine import REDACTED, WITHHELD

HARNESSES = ("claude-code:", "codex:", "pi:")
ATTEMPTS = 3  # one answer and up to two schema retries (REQ-030)
KEPT = 256  # answers kept for resume
ABORTED = "aborted: the workflow stopped this agent"
NO_CWD = "no workspace: neither an explicit cwd nor a session cwd is set"


class HermesAgentHost:
    def __init__(self, api: Callable[[str | None], Any] | None = None,
                 find: Callable[[str, Path], Path | None] | None = None, root_task: str = "workflows",
                 gate_timeout: float | None = None, cwd: str | None = None):
        self._api, self._find, self._root, self._cwd = api, find, root_task, cwd
        self._gate_timeout = gate_timeout or gates.TIMEOUT
        self._lock = threading.Lock()
        self._aborted: set[str] = set()
        self._live: set[str] = set()
        self._wake: dict[str, threading.Event] = {}  # agent id -> set by its API call's end or by an abort
        self._answers: dict[str, tuple[dict, str]] = {}  # agent id -> (its request, its last answer)

    def spawn(self, req: dict) -> dict:
        return self._run(str(req.get("agentId") or ""), req, str(req.get("prompt") or ""))

    def resume(self, agent_id: str, prompt: str) -> dict:
        with self._lock:
            kept = self._answers.get(agent_id)
        if kept is None:
            return _failed("no answer of agent %s is kept to resume from" % agent_id)
        req, answer = kept
        # ponytail: SubagentsAPI.send cannot return a continued agent's answer, so resume is a fresh spawn carrying
        # the first prompt and the last answer; upgrade: the API's own resume (HANDOVER extract:api-resume).
        carried = "You worked on this task before:\n%s\n\nYour answer was:\n%s\n\nNow: %s" % (req.get("prompt"),
                                                                                           answer, prompt)
        return self._run(agent_id, req, carried)

    def abort(self, agent_id: str) -> None:
        # ponytail: a foreground spawn exposes no subagent_id until it ends, so there is nothing to give
        # SubagentsAPI.stop: the spawn is released at once and the child runs to its end, its answer dropped;
        # upgrade: a background spawn stopped with SubagentsAPI.stop(subagent_id).
        with self._lock:
            if agent_id in self._live or agent_id not in self._answers:  # a finished agent has nothing to stop
                self._aborted.add(agent_id)
                if agent_id in self._wake:
                    self._wake[agent_id].set()

    def gate(self, command: str, cwd: str | None = None) -> dict:
        where = cwd or self._cwd or _session_cwd()
        if not where:
            return {"ok": False, "output": NO_CWD}
        return gates.run(command, where, self._gate_timeout, _redacted)

    def load(self, ref: Any) -> dict:
        ref = ref if isinstance(ref, dict) else {}
        where = self._cwd or _session_cwd()
        if not where:
            return _missing(NO_CWD)
        cwd = Path(where).resolve()
        if isinstance(ref.get("name"), str) and ref["name"]:
            try:
                find = self._find or importlib.import_module(__package__ + ".saved").find
            except (ImportError, AttributeError) as exc:
                return _missing("saved workflows are not available: %s" % exc)
            found = find(ref["name"], cwd)
            return _read(Path(found)) if found else _missing("no saved workflow named %s" % ref["name"])
        if isinstance(ref.get("scriptPath"), str) and ref["scriptPath"]:
            target = (cwd / ref["scriptPath"]).resolve()
            if not target.is_relative_to(cwd):
                return _missing("%s is outside the session workspace %s" % (ref["scriptPath"], cwd))
            return _read(target)
        return _missing("a workflow reference needs a name or a scriptPath")

    def _run(self, agent_id: str, req: dict, prompt: str) -> dict:
        with self._lock:  # check and mark live in one step, so an abort from here on is never dropped
            if agent_id in self._aborted:
                self._aborted.discard(agent_id)
                return _skipped()
            self._live.add(agent_id)
        try:
            result = self._start(agent_id, req, prompt)
        finally:
            with self._lock:
                self._live.discard(agent_id)
        if result.get("ok"):
            with self._lock:
                self._answers[agent_id] = (req, result["text"])
                while len(self._answers) > KEPT:
                    del self._answers[next(iter(self._answers))]
        return result

    def _start(self, agent_id: str, req: dict, prompt: str) -> dict:
        model = str(req.get("model") or "").strip() or None
        if model and model.lower().startswith(HARNESSES) and not _importable("harnessrunner"):
            # ponytail: the API refuses this too (api.py NOT_INSTALLED); checked here so the reason never depends
            # on the API build. Upgrade: Task 14's router owns per-host routing.
            return _failed("harness-runner not installed: agent %s needs the %s harness (model %s)"
                           % (req.get("label") or agent_id, model.split(":", 1)[0], model))
        try:
            api = self._make_api(req.get("cwd"))
        except ImportError as exc:
            return _failed("hermes-subagents not installed: %s" % exc)
        return self._ask(api, agent_id, req, prompt, model)

    def _ask(self, api: Any, agent_id: str, req: dict, prompt: str, model: str | None) -> dict:
        schema = req.get("schema") or None
        kw = {"model": model, "effort": str(req.get("effort") or "").strip() or None, "schema": schema,
              "root_task": str(req.get("runId") or "") or self._root}
        ask, reason = prompt, ""
        agent_type = mbteam_roles.resolve(str(req.get("agentType") or "general-purpose"), api)
        for _ in range(ATTEMPTS if schema else 1):
            if self._cancelled(agent_id):  # an abort while the API was built, or between retries
                return _skipped()
            try:
                res = self._call(agent_id, api, agent_type, ask, kw)
            except Exception as exc:  # noqa: BLE001 - a child that crashes is this agent's failure (REQ-029)
                return _failed("%s: %s" % (type(exc).__name__, exc))
            if self._cancelled(agent_id) or res is None:
                return _skipped()
            answer = _answer(res)
            if res.status != "done" and not (schema and "schema errors" in str(res.line)):
                return _failed(str(res.line or "agent %s" % res.status))
            if not schema:
                return {"ok": True, "text": _redacted(answer)}
            errors, text = _check(answer, schema) if res.status == "done" else ([_redacted(str(res.line))], "")
            if not errors:
                return {"ok": True, "text": text}
            reason = "; ".join(errors)
            ask = "%s\n\n%s\n\nOUTPUT CONTRACT schema:\n%s" % (prompt, _retry(errors), json.dumps(schema))
        return _failed("schema: %s (after %d attempts)" % (reason, ATTEMPTS))

    def _call(self, agent_id: str, api: Any, agent_type: str, ask: str, kw: dict) -> Any:
        """``api.spawn`` run on its own thread, so an abort ends the wait: ``None`` when it did (the child runs on)."""
        box: dict[str, Any] = {}
        woken = threading.Event()
        with self._lock:
            if agent_id in self._aborted:  # an abort since the caller's check
                return None
            self._wake[agent_id] = woken

        def work() -> None:
            try:
                box["res"] = api.spawn(agent_type, ask, **kw)
            except BaseException as exc:  # noqa: BLE001 - handed to the waiting thread
                box["exc"] = exc
            finally:
                woken.set()

        threading.Thread(target=contextvars.copy_context().run, args=(work,), name="workflows-agent",
                         daemon=True).start()
        woken.wait()
        with self._lock:
            self._wake.pop(agent_id, None)
        if "exc" in box:
            raise box["exc"]
        return box.get("res")

    def _make_api(self, cwd: Any) -> Any:
        where = str(cwd) if cwd else None
        if self._api is not None:
            return self._api(where)
        from subagents.api import SubagentsAPI

        return SubagentsAPI(cwd=(lambda: where) if where else None)

    def _cancelled(self, agent_id: str) -> bool:
        with self._lock:
            if agent_id in self._aborted:
                self._aborted.discard(agent_id)
                return True
        return False


def _answer(res: Any) -> str:
    """The full answer: the step file's ``## Full answer`` (subagents/results.py), else its summary, else the line."""
    try:
        body = Path(res.step_file).read_text(encoding="utf-8") if res.step_file else ""
    except OSError:
        body = ""
    if "\n## Full answer\n" in body:
        return body.split("\n## Full answer\n", 1)[1].rstrip("\n")
    return body.removeprefix("## Summary\n").strip() if body else str(res.line or "")


def _check(answer: str, schema: dict) -> tuple[list[str], str]:
    """``([], json_text)`` for an answer that meets ``schema`` (stock's validator), else ``(errors, "")``."""
    try:
        from tools.delegation_output_schema import extract_json_candidate, validate_output
    except ImportError:
        return ["the Hermes output validator (tools.delegation_output_schema) is not importable"], ""
    valid, errors = validate_output(answer, schema)
    if not valid:
        return [str(e) for e in errors] or ["the answer does not match the schema"], ""
    return [], json.dumps(_masked(json.loads(extract_json_candidate(answer))), ensure_ascii=False)


def _masked(value: Any) -> Any:
    """``value`` with secret-named fields replaced and every string masked whole (engine._scrub cuts long ones)."""
    try:
        from agent.redact import is_secret_field_name
    except ImportError:
        return WITHHELD  # without field names a dumped secret can no longer be recognised
    if isinstance(value, dict):
        return {k: REDACTED if is_secret_field_name(k) else _masked(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_masked(v) for v in value]
    return _redacted(value) if isinstance(value, str) else value


def _retry(errors: list[str]) -> str:
    try:
        from tools.delegation_output_schema import build_retry_message
    except ImportError:
        return "Your answer failed the schema: %s. Answer with one JSON object." % "; ".join(errors)
    return build_retry_message(errors)


def _importable(module: str) -> bool:
    try:
        importlib.import_module(module)
    except ImportError:
        return False
    return True


def _read(path: Path) -> dict:
    try:
        return {"ok": True, "script": path.read_text(encoding="utf-8")}
    except (OSError, UnicodeDecodeError) as exc:
        return _missing("cannot read %s: %s" % (path, exc))


def _session_cwd() -> str:
    """The calling session's workspace (stock), else "": under the gateway this process's cwd is the install."""
    try:
        from hermes_cli.plugin_session import session_cwd
    except ImportError:
        return ""
    return session_cwd() or ""


def _redacted(text: str) -> str:
    """The whole ``text`` through the stock redactor (engine.py clips its summary first; an answer is not clipped)."""
    try:
        from agent.redact import redact_sensitive_text
    except ImportError:
        return WITHHELD
    return redact_sensitive_text(text, force=True, file_read=True, secret_file=True, redact_url_credentials=True)


def _failed(error: str) -> dict:
    return {"ok": False, "error": _redacted(error)}


def _missing(message: str) -> dict:
    return {"ok": False, "message": _redacted(message)}


def _skipped() -> dict:
    return {"ok": False, "skipped": True, "error": ABORTED}
