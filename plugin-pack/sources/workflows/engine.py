"""The workflow engine API (REQ-036); ultracode's WorkflowPort adapts it.

``WorkflowEngine(host=None, on_progress=None)``: ``on_progress(run_id, entries)`` gets every progress batch of a
run (bridge.drive), from ``run`` and ``resume``.
  ``available()`` is True when ``node`` is on PATH and runner/main.mjs exists.
  ``run(script_or_name, args, budget_tokens, entry, approved=False) -> RunSummary`` runs one workflow in its
  own runner process (workflows/bridge.py) and stores it (workflows/store.py). A single-word argument is a
  saved workflow name, resolved through ``host.load({"name": ...})``; anything else is script source.
  ``resume(run_id, script=None) -> RunSummary`` returns the stored summary of a ``done`` run when no edited
  ``script`` is given. Any other run (unfinished, failed, stopped, or an edited script) is resumed through
  ``workflows.resume`` (Task 10, WE-RES), imported lazily: ``request(folder: Path, script: str | None) ->
  dict | str`` gives the runner request ``{script, args, runDir, budget, replay?}``, which is driven and
  stored through ``finish``, or a refusal reason (the run fails with it). Without that module a stored run
  returns its summary and an unfinished one is refused, as before.
  ``finish(store, folder, run_id, done, extra=None) -> RunSummary`` writes result.json from the runner's
  ``done`` (with ``errors {label: error}``, each masked and clipped to ERROR_CHARS) and summarizes it; the
  summary's second line is ``agent errors: <label>: <text>; ...``, clipped to ERROR_CHARS in total.

The host seam is ``AgentHost`` (design.md § Interfaces): ``spawn(req)``, ``abort(agent_id)``,
``resume(agent_id, prompt)``, ``gate(command, cwd)`` and ``load(ref)``, called from bridge threads, with
results that mirror the vendored runtime (bridge.py lists them). ``host=None`` means the Hermes agent host
on ``subagents.api.SubagentsAPI``: ``workflows.agents.HermesAgentHost()`` (Task 6, WE-HOST), imported on
the first run. Until that module exists a run with ``host=None`` fails naming it.

``entry`` and ``approved`` are stored with the run; the opt-in and size approval that read them are Task 7.
The summary (REQ-035) is at most SUMMARY_CHARS characters (~1000 tokens) and carries the run id, the result
path and the journal path; the value preview and the error pass through Hermes' stock redactor
(``agent.redact.redact_sensitive_text``, the one behind ``ctx.redact_text``), and are withheld when it is
not importable.
"""

from __future__ import annotations

import importlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from . import bridge, mbteam_roles
from .store import RunStore, new_run_id

Status = Literal["done", "failed", "stopped", "approval_needed"]
STATUS = {"completed": "done", "killed": "stopped"}
NAME = re.compile(r"[A-Za-z0-9][\w.-]{0,127}")
SUMMARY_CHARS, PREVIEW_CHARS, ERROR_CHARS, SCAN_CHARS = 5000, 1500, 500, 20000
DELIMITERS = ' \t\n",:{}[]'
HOST_MISSING = ("no agent host: the Hermes agent host (workflows/agents.py, Task 6 WE-HOST) is not built yet; "
                "pass WorkflowEngine(host=...)")
NO_NODE = "workflows need Node (`node` on PATH) and runner/main.mjs"
REDACTED = "***"  # the stock redactor's marker
WITHHELD = "(withheld: the Hermes redactor agent.redact is not importable)"


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    status: Status
    text: str
    result_path: Path | None
    journal_path: Path | None


class WorkflowEngine:
    def __init__(self, host: Any = None, on_progress: Callable[[str, list], None] | None = None):
        self._host, self._on_progress = host, on_progress

    def available(self) -> bool:
        return bridge.node() is not None and bridge.RUNNER.is_file()

    def run(self, script_or_name: str, args: Any = None, budget_tokens: int | None = None,
            entry: str = "user", approved: bool = False) -> RunSummary:
        if not self.available():
            return _failed(NO_NODE)
        host = self._agent_host()
        if host is None:
            return _failed(HOST_MISSING)
        source, error = _source(host, script_or_name, entry)
        if source is None:
            return _failed(_clip(_masked(error), ERROR_CHARS))
        refusal = mbteam_roles.run_refusal(source, args, host.gate)
        if refusal:
            return _failed(refusal)
        store = RunStore()
        store.prune()
        run_id = new_run_id()
        folder = store.create(run_id, source, {"args": args, "budget": budget_tokens, "entry": entry,
                                               "approved": approved})
        done = bridge.drive(host, {"script": source, "args": args, "runDir": str(folder),
                                   "budget": budget_tokens}, on_progress=self._progress(run_id))
        return finish(store, folder, run_id, done)

    def resume(self, run_id: str, script: str | None = None) -> RunSummary:
        store = RunStore()
        folder = store.folder(run_id)
        record = store.result(folder) if folder else None
        if record is not None and record.get("status") == "done" and script is None:
            return summarize(record, folder / "result.json", folder / "journal.jsonl")
        if folder is None:
            return _failed("not a run id (expected wf-<stamp>-<hex>)")
        if not (folder / "script.js").is_file():
            return _failed("no run %s in %s" % (run_id, store.root))
        journal = folder / "journal.jsonl"
        try:
            resume = importlib.import_module(__package__ + ".resume")
        except ModuleNotFoundError as exc:
            if exc.name != __package__ + ".resume":
                raise  # a module that is there but fails to import is a bug, not the missing Task 10
            if record is not None:
                return summarize(record, folder / "result.json", journal)
            # ponytail: an unfinished run is refused, not replayed; ceiling: a crashed run must be restarted.
            # Upgrade: Task 10 (WE-RES) replays journal.jsonl through the runner request's `replay`.
            return RunSummary(run_id, "failed", "workflow %s is unfinished; resuming it by prefix replay is "
                              "Task 10 (WE-RES), not built yet" % run_id, None, journal)
        host = self._agent_host() if self.available() else None
        if host is None:
            return RunSummary(run_id, "failed", "workflow failed: " + (HOST_MISSING if self.available() else NO_NODE),
                              None, journal)
        with bridge.run_lock(folder) as free:  # one holder from preparing the request through result.json
            if not free:
                return RunSummary(run_id, "failed", "workflow failed: run %s is already running" % run_id, None,
                                  journal)
            request = resume.request(folder, script)
            if isinstance(request, str):
                return RunSummary(run_id, "failed", _clip("workflow failed: " + request, SUMMARY_CHARS), None,
                                  journal)
            done = bridge.drive(host, request, on_progress=self._progress(run_id), locked=True)
            return finish(store, folder, run_id, done)

    def _progress(self, run_id: str) -> Callable[[list], None] | None:
        hook = self._on_progress
        return None if hook is None else (lambda entries: hook(run_id, entries))

    def _agent_host(self) -> Any:
        if self._host is None:
            try:
                agents = importlib.import_module(__package__ + ".agents")
            except ImportError:
                return None
            self._host = agents.HermesAgentHost(cwd=_folder())
        return self._host


def finish(store: RunStore, folder: Path, run_id: str, done: dict, extra: dict | None = None) -> RunSummary:
    """Store the runner's ``done`` as result.json and summarize it. ``extra`` is merged into the record, never
    over ``run_id``, ``status`` or ``errors``; every agent error is masked and clipped to ERROR_CHARS, and so is
    its label (a default label is the prompt's first line), suffixed ``#<n>`` when masking makes two alike."""
    errors = done.get("errors") if isinstance(done.get("errors"), dict) else {}
    masked: dict[str, str] = {}
    for label, text in errors.items():
        key = base = _clip(_masked(str(label)), ERROR_CHARS)
        n = 2
        while key in masked:
            key, n = "%s#%d" % (base, n), n + 1
        masked[key] = _clip(_masked(str(text)), ERROR_CHARS)
    record = {"value": done.get("value"), "error": done.get("error"), "agents": done.get("agentCount", 0),
              "replayed": done.get("replayedCount", 0), **(extra or {}),
              "run_id": run_id, "status": STATUS.get(done.get("status"), "failed"), "errors": masked}
    return summarize(record, store.finish(folder, record), folder / "journal.jsonl")


def summarize(record: dict, result_path: Path, journal_path: Path) -> RunSummary:
    lines = ["workflow %s %s" % (record["run_id"], record["status"])]
    errors = record.get("errors") if isinstance(record.get("errors"), dict) else {}
    if errors:
        joined = "; ".join("%s: %s" % (k, v) for k, v in errors.items())
        lines.append(_clip("agent errors: " + re.sub(r"[\r\n]+", " ", _masked(joined)), ERROR_CHARS))
    if record.get("error"):
        lines.append("error: " + _clip(_masked(str(record["error"])), ERROR_CHARS))
    lines.append("agents: %s (replayed %s)" % (record.get("agents", 0), record.get("replayed", 0)))
    tail = ["result: %s" % result_path, "journal: %s" % journal_path]
    room = SUMMARY_CHARS - len("\n".join(lines + tail)) - len("\nvalue: ")
    value = json.dumps(_scrub(record.get("value")), ensure_ascii=False, default=str)
    lines.append("value: " + _clip(_masked(value), max(0, min(PREVIEW_CHARS, room))))
    text = "\n".join(lines + tail)
    return RunSummary(record["run_id"], record["status"], text[:SUMMARY_CHARS], result_path, journal_path)


def _folder() -> str:
    """The user's folder for a bare engine: the session's workspace, else this process's cwd.
    ponytail: a bare engine is reached by the CLI and direct use, where the process cwd is the user's folder;
    ceiling: under the gateway it is the install, so Wiring always passes its own host with the session cwd."""
    try:
        agents = importlib.import_module(__package__ + ".agents")
    except ImportError:
        return os.getcwd()
    return agents._session_cwd() or os.getcwd()


def _source(host: Any, script_or_name: str, entry: str = "user") -> tuple[str | None, str]:
    if not NAME.fullmatch(script_or_name):
        return script_or_name, ""
    try:
        loaded = host.load({"name": script_or_name})
    except Exception as exc:  # noqa: BLE001 - a failing lookup fails the run
        return None, "loading workflow %s failed: %s" % (script_or_name, exc)
    if isinstance(loaded, dict) and loaded.get("ok") and isinstance(loaded.get("script"), str):
        return loaded["script"], ""
    if entry == "cli":  # the CLI runs in the user's folder; a host (a test double) may not know the saved files
        found = importlib.import_module(__package__ + ".saved").source(script_or_name, _folder())
        if found.get("ok"):
            return found["script"], ""
        loaded = found
    message = loaded.get("message") if isinstance(loaded, dict) else None
    return None, str(message or "no workflow named %s" % script_or_name)


def _scrub(value: Any) -> Any:
    """``value`` with secret-named fields replaced and every string masked, before it is dumped: the
    redactor no longer sees ``password: x`` once json.dumps has escaped its newlines."""
    try:
        from agent.redact import is_secret_field_name
    except ImportError:
        return WITHHELD  # without field names a dumped secret can no longer be recognised
    if isinstance(value, dict):
        return {k: REDACTED if is_secret_field_name(k) else _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return _masked(value) if isinstance(value, str) else value


def _masked(text: str) -> str:
    """``text`` through the stock redactor. A long text is cut at a delimiter before it is redacted, so
    the cut never leaves the head of a secret the redactor can no longer recognise."""
    if len(text) > SCAN_CHARS:
        head = text[:SCAN_CHARS]
        text = head[:max(head.rfind(c) for c in DELIMITERS) + 1] + "…"
    try:
        from agent.redact import redact_sensitive_text
    except ImportError:
        return WITHHELD
    return redact_sensitive_text(text, force=True, file_read=True, secret_file=True, redact_url_credentials=True)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:max(0, limit - 1)] + "…"


def _failed(reason: str) -> RunSummary:
    """A run that never started: no run id, no folder."""
    return RunSummary("", "failed", "workflow failed: " + reason, None, None)
