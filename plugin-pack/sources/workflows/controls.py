"""Run controls (REQ-025) behind ``/workflow``, ``hermes workflow`` and the uc socket (REQ-033).

``Controls(engine, store=None, ask=None, host=None)``: ``engine`` is the WorkflowEngine, ``store`` a RunStore
(default: a fresh one per call, so a profile switch is honoured), ``ask`` the uc socket client
(``workflows.uc_socket.ask``) used to stop a run another process drives, ``host`` the engine's agent host.
WE-WIRE feeds it from the engine's ``on_progress`` and registers ``command``, ``cli`` and ``handle``.
  ``feed(run_id, entries)`` keeps label/index -> agent id per run (for skip and steer).
  ``list_runs()``: the live runs (``bridge.live()``) as ``running``, then the last LIST_RUNS stored runs with
  their status (without result.json: ``running`` while another process holds its run.lock, else ``unfinished``). ``show(run_id)``: the stored summary
  (``engine.summarize``), the agent errors, result.json's ``diagram`` in a mermaid block and the kept worktree
  branches (worktrees.jsonl); a live run reads ``running``. ``stop(run_id)``: abort a live run here, else
  ``ask(default_path(), {"op": "stop", "run_id"})``. ``skip(run_id, label_or_index)``: skip one running agent (its
  ``agent()`` is null); an unknown or finished one is refused. ``resume(run_id)``: ``engine.resume`` on a daemon thread; a live run is refused.
  ``steer(run_id, label, text)``: ``host.steer(<run id>/<agent id>, text)`` when the host has it.
  ``command(raw) -> str``: ``/workflow list | show <id> | stop <id> | skip <id> <label|n> | resume <id>``.
  ``handle(request) -> dict``: the uc socket handler: ``run`` (entry ``socket``), ``list``, ``show``, ``stop``.
``cli(argv, out=sys.stdout, controls=None) -> int``: ``hermes workflow run <file|name> [--args JSON] | list |
show <id> | stop <id> | resume <id>``; ``run`` uses entry ``cli``; ``resume`` runs to its end in this process; never reads stdin or a tty; exit 0 when the
run is ``done`` (or the command succeeded), else 1.
"""

from __future__ import annotations

import argparse
import contextvars
import json
import sys
import threading
from pathlib import Path
from typing import Any, Callable, TextIO

from . import bridge
from .engine import summarize
from .store import RUN_ID, RunStore

LIST_RUNS = 20
USAGE = "usage: /workflow list | show <id> | stop <id> | skip <id> <label|n> | resume <id>"
NO_STEER = "steering needs the host's SubagentsAPI send (not built yet)"


class Refused(str):
    """A control's answer that refused the request (cli exit code 1)."""


class Controls:
    def __init__(self, engine: Any, store: RunStore | None = None, ask: Callable[..., dict] | None = None,
                 host: Any = None):
        self.engine, self._store, self._ask, self.host = engine, store, ask, host
        self._agents: dict[str, dict[str, str]] = {}
        self._running: dict[str, dict[int, tuple[str, str]]] = {}  # run -> index -> (agent id, label)
        self._lock = threading.Lock()

    @property
    def store(self) -> RunStore:
        return self._store or RunStore()

    def feed(self, run_id: str, entries: list) -> None:
        with self._lock:
            known, running = self._agents.setdefault(run_id, {}), self._running.setdefault(run_id, {})
            for entry in entries if isinstance(entries, list) else ():
                if isinstance(entry, dict) and entry.get("type") == "workflow_agent" and entry.get("agentId"):
                    agent, label = str(entry["agentId"]), str(entry.get("label"))
                    known[str(entry.get("index"))] = known[label] = agent
                    if isinstance(entry.get("index"), int):
                        if entry.get("state") == "start":
                            running[entry["index"]] = (agent, label)
                        else:
                            running.pop(entry["index"], None)
            for gone in [r for r in self._agents if r != run_id and r not in bridge.live()]:
                del self._agents[gone]
                self._running.pop(gone, None)

    def list_runs(self) -> str:
        live = bridge.live()
        lines = ["%s running" % run_id for run_id in live]
        root = self.store.root
        folders = sorted((p for p in root.iterdir() if RUN_ID.fullmatch(p.name) and p.is_dir()),
                         key=lambda p: p.name, reverse=True) if root.is_dir() else []
        for folder in [f for f in folders if f.name not in live][:LIST_RUNS]:
            record = self.store.result(folder)
            status = record.get("status", "unknown") if record else "running" if bridge.held(folder) else "unfinished"
            lines.append("%s %s" % (folder.name, status))
        return "\n".join(lines) or "no workflow runs"

    def show(self, run_id: str) -> str:
        folder, refusal = self._folder(run_id)
        if refusal:
            return refusal
        if run_id in bridge.live():
            return "workflow %s running" % run_id
        record = self.store.result(folder)
        if record is None:
            return "workflow %s %s" % (run_id, "running" if bridge.held(folder) else "unfinished")
        lines = [summarize(record, folder / "result.json", folder / "journal.jsonl").text]
        errors = record.get("errors") if isinstance(record.get("errors"), dict) else {}
        lines += ["errors:"] + ["- %s: %s" % item for item in errors.items()] if errors else []
        if isinstance(record.get("diagram"), str) and record["diagram"].strip():
            lines += ["```mermaid", record["diagram"].strip(), "```"]
        branches = _branches(folder / "worktrees.jsonl")
        lines += ["kept branches:"] + ["- %s (%s)" % b for b in branches] if branches else []
        return "\n".join(lines)

    def stop(self, run_id: str) -> str:
        folder, _ = self._folder(run_id)
        text = self._stop_here(run_id)
        if folder is None or not isinstance(text, Refused) or self._ask is None:
            return text
        from .uc_socket import default_path
        try:
            path = default_path()
        except ValueError as exc:  # a HERMES_HOME too long for AF_UNIX: no socket to reach
            return Refused("%s; %s" % (text, exc))
        reply = self._ask(path, {"op": "stop", "run_id": run_id})
        text = str(reply.get("text") or reply.get("error") or reply)
        return text if reply.get("ok") else Refused(text)

    def _stop_here(self, run_id: str) -> str:
        """Stop a run this process drives; the socket handler uses only this, so it never asks itself."""
        _, refusal = self._folder(run_id)
        if refusal:
            return refusal
        if bridge.control(run_id, {"op": "abort"}):
            return "stopping %s" % run_id
        return Refused("run %s is not running in this process" % run_id)

    def skip(self, run_id: str, label_or_index: str | int) -> str:
        if run_id not in bridge.live():
            return Refused("run %s is not running in this process" % run_id)
        key = str(label_or_index).strip()
        with self._lock:
            running = dict(self._running.get(run_id, {}))
        if key.isdigit() and int(key) in running:
            sent = bridge.control(run_id, {"op": "skip", "index": int(key)})
        elif agent := next((a for a, label in running.values() if label == key), None):
            sent = bridge.control(run_id, {"op": "skip", "agent": agent})
        else:
            return Refused("no running agent %s in run %s" % (key, run_id))
        return "skipping %s in %s" % (key, run_id) if sent else Refused("run %s is not running" % run_id)

    def resume(self, run_id: str) -> str:
        refusal = self._unresumable(run_id)
        if refusal:
            return refusal
        threading.Thread(target=contextvars.copy_context().run, args=(self.engine.resume, run_id),
                         name="workflows-resume", daemon=True).start()  # keeps the caller's profile
        return "resuming %s" % run_id

    def _unresumable(self, run_id: str) -> Refused | None:
        folder, refusal = self._folder(run_id)
        if refusal:
            return refusal
        if run_id in bridge.live() or bridge.held(folder):
            return Refused("run %s is running; stop it first" % run_id)
        return None

    def steer(self, run_id: str, label: str, text: str) -> str:
        steer = getattr(self.host, "steer", None)
        if not callable(steer):
            # ponytail: no steering through HermesAgentHost; ceiling: a running agent cannot be redirected.
            # Upgrade: SubagentsAPI send (I-050, SA2-WIRE) gives the host a steer.
            return Refused(NO_STEER)
        with self._lock:
            agent = self._agents.get(run_id, {}).get(str(label))
        if agent is None:
            return Refused("no agent %s in run %s" % (label, run_id))
        steer("%s/%s" % (run_id, agent), text)
        return "steered %s in %s" % (label, run_id)

    def command(self, raw: str) -> str:
        words = str(raw or "").split()
        verb, rest = (words[0].lower(), words[1:]) if words else ("", [])
        if verb == "list" and not rest:
            return self.list_runs()
        if verb in ("show", "stop", "resume") and len(rest) == 1:
            return getattr(self, verb)(rest[0])
        if verb == "skip" and len(rest) >= 2:
            return self.skip(rest[0], " ".join(rest[1:]))
        return USAGE

    def handle(self, request: dict) -> dict:
        op = request.get("op")
        if op == "run":
            if not isinstance(request.get("script"), str):
                return {"ok": False, "error": "run needs a script"}
            summary = self.engine.run(request["script"], request.get("args"), request.get("budget"), entry="socket")
            reply = {"ok": summary.status == "done", "run_id": summary.run_id, "status": summary.status,
                     "text": summary.text}
            return reply if reply["ok"] else {**reply, "error": summary.text}
        if op == "list":
            return {"ok": True, "text": self.list_runs()}
        if op in ("show", "stop"):
            text = (self.show if op == "show" else self._stop_here)(str(request.get("run_id") or ""))
            return {"ok": not isinstance(text, Refused), "text": str(text)}
        return {"ok": False, "error": "unknown op %r (run | list | show | stop)" % op}

    def _folder(self, run_id: str) -> tuple[Path | None, Refused | None]:
        folder = self.store.folder(str(run_id))
        if folder is None:
            return None, Refused("not a run id (expected wf-<stamp>-<hex>): %s" % str(run_id)[:80])
        if not folder.is_dir():
            return None, Refused("no run %s" % run_id)
        return folder, None


def _branches(path: Path) -> list[tuple[str, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    kept = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("changed") and row.get("branch"):
            kept.append((str(row["branch"]), str(row.get("label") or "")))
    return kept


def cli(argv: list[str], out: TextIO = sys.stdout, controls: Controls | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes workflow", add_help=False)
    sub = parser.add_subparsers(dest="verb", required=True)
    run = sub.add_parser("run")
    run.add_argument("target")
    run.add_argument("--args", default=None)
    sub.add_parser("list")
    for verb in ("show", "stop", "resume"):
        sub.add_parser(verb).add_argument("run_id")
    try:
        ns = parser.parse_args(argv)
    except SystemExit:
        print("usage: hermes workflow run <file|name> [--args JSON] | list | show <id> | stop <id> | resume <id>",
              file=out)
        return 1
    if controls is None:
        from .engine import WorkflowEngine
        from .uc_socket import ask
        controls = Controls(WorkflowEngine(), ask=ask)
    if ns.verb == "run":
        try:
            args = json.loads(ns.args) if ns.args is not None else None
        except ValueError as exc:
            print("--args is not JSON: %s" % exc, file=out)
            return 1
        target = Path(ns.target)
        source = target.read_text(encoding="utf-8") if target.is_file() else ns.target
        summary = controls.engine.run(source, args, None, entry="cli")
        print(summary.text, file=out)
        return 0 if summary.status == "done" else 1
    if ns.verb == "resume":  # in this process, to its end: a daemon thread would die with the CLI
        refusal = controls._unresumable(ns.run_id)
        summary = None if refusal else controls.engine.resume(ns.run_id)
        print(refusal or summary.text, file=out)
        return 0 if summary is not None and summary.status == "done" else 1
    text = controls.list_runs() if ns.verb == "list" else getattr(controls, ns.verb)(ns.run_id)
    print(text, file=out)
    return 1 if isinstance(text, Refused) else 0
