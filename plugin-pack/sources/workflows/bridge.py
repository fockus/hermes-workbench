"""Plugin side of the runner's stdio line protocol (the header of runner/main.mjs is the contract).

``drive(host, request)`` starts one ``node runner/main.mjs`` process for one run (REQ-032), writes the
run request, serves every runner call through the ``AgentHost`` and returns the runner's ``done`` message
with ``errors {label: error}`` added (always present): every ``workflow_agent`` progress entry that carries a
non-empty ``error`` (failed, skipped or gate-failed), keyed by its label (else its agent id); a second agent
with a label already used is ``<label>#<index>``, and a resumed call of the same agent overwrites its own row.
A malformed progress line is ignored, never fails the run.

Seams for resume and the UI (WE-RES, WE-UI):
  ``drive(host, request, runner=RUNNER, on_progress=None)``: ``on_progress(entries)`` gets every progress batch
  on the serve loop, in order; an exception in it is swallowed (a card never fails a run).
  ``live() -> list[str]``: the run ids this process is driving. ``control(run_id, msg) -> bool`` writes one
  runner control line (``{"op": "abort"}``, ``{"op": "skip", "index": n}`` or ``{"op": "skip", "agent": id}``
  with the runner's unscoped agent id) to that run's runner; False when the run is not live here.
  Run lock: ``drive`` holds ``flock(LOCK_EX | LOCK_NB)`` on ``<runDir>/run.lock`` (0600) for the whole run; a
  run already held is ``done {status: "failed", error: "run <id> is already running"}`` at once.
  ``run_lock(run_dir)`` holds it for a block (yields False, holding nothing, when another holder has it), so a
  caller keeps it from preparing a run through storing its result: ``drive(..., locked=True)`` then takes none.
  A session is in ``live()`` only once its run line is written, so a control never gets ahead of it.
  ``held(run_dir) -> bool`` tells whether a process or session drives that run; the OS drops the lock when
  the process dies, so a crashed run reads as not held.
Each call is served on its own thread, so ``parallel()`` agents run concurrently; replies are JSON lines
written with ``allow_nan=False`` (the runner fails a run on a line that is not a JSON object).

Agent ids cross the host boundary as ``<run id>/<agent id>`` (the run id is the name of ``runDir``), so one
host shared by concurrent runs tells their ``wf-agent-<n>`` agents apart; the runner and journal keep the
unscoped ids. Each call runs in a copy of the caller's context (ContextVars such as the Hermes home override).

Host calls and their replies (result shapes mirror the vendored runtime):
  ``spawn{req}`` -> ``host.spawn({**req, agentId: <scoped>, runId: <run id>})`` -> ``{ok, text?, error?, skipped?,
  tokens?, ...}``; ``runId`` lets HermesAgentHost use the run id as the limiter's ``root_task``, so the
  hermes-subagents lifetime cap per root counts per run
  ``resume{agent, prompt}`` -> ``host.resume(agent, prompt)`` -> as spawn
  ``gate{command, agent, cwd?}`` -> ``host.gate(command, cwd)`` -> ``{ok, output}``
  ``load{ref}`` -> ``host.load(ref)`` -> ``{ok: True, script}`` or ``{ok: False, message}``
  ``abort{agent}`` (notification) -> ``host.abort(agent)``; the host returns without waiting for the
  child, and an id whose spawn has not registered yet is remembered and cancels that spawn when it registers
A host call that raises, or returns what JSON cannot carry, is answered ``{id, ok: False, error}``.

A runner that exits without ``done`` (crash, kill) gives a ``failed`` done naming its exit code, and the
agents it still had in flight are aborted. No runner outlives ``drive``: it is waited for, then killed.
"""

from __future__ import annotations

import collections
import contextlib
import contextvars
import fcntl
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

RUNNER = Path(__file__).resolve().parent / "runner" / "main.mjs"
EXIT_WAIT = 5.0
CONTROLS = ("abort", "skip")
LOCK_NAME, LOCK_MODE = "run.lock", 0o600
_LIVE: dict[str, "_Session"] = {}
_LIVE_LOCK = threading.Lock()


def node() -> str | None:
    return shutil.which("node")


def drive(host: Any, request: dict, runner: Path = RUNNER, on_progress: Callable[[list], None] | None = None,
          locked: bool = False) -> dict:
    try:
        first = json.dumps({"op": "run", **request}, allow_nan=False) + "\n"
    except (TypeError, ValueError) as exc:
        return _done_failed("the run request is not JSON: %s" % exc)
    run_dir = Path(str(request.get("runDir") or ""))
    with contextlib.nullcontext(True) if locked else run_lock(run_dir) as free:
        if not free:
            return _done_failed("run %s is already running" % run_dir.name)
        proc = subprocess.Popen([node() or "node", str(runner)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1)
        session = _Session(host, proc, run_dir.name, on_progress)
        try:
            session.write(first)
            session.register()
            return {**session.serve(), "errors": dict(session.errors)}
        finally:
            session.close()


@contextlib.contextmanager
def run_lock(run_dir: Path):
    lock = _lock(Path(run_dir))
    try:
        yield lock != -1
    finally:
        if lock not in (None, -1):
            os.close(lock)  # closing the descriptor releases the flock


def live() -> list[str]:
    with _LIVE_LOCK:
        return list(_LIVE)


def control(run_id: str, msg: dict) -> bool:
    if not isinstance(msg, dict) or msg.get("op") not in CONTROLS:
        raise ValueError("a control is {op: %s}, not %r" % ("|".join(CONTROLS), msg))
    line = json.dumps(msg, allow_nan=False) + "\n"
    with _LIVE_LOCK:
        session = _LIVE.get(run_id)
    if session is None:
        return False
    session.write(line)
    return True


def held(run_dir: Path) -> bool:
    try:
        fd = os.open(Path(run_dir) / LOCK_NAME, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False  # never locked, or no such run
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)  # also drops the probe's own lock
    return False


def _lock(run_dir: Path) -> int | None:
    """The descriptor holding ``run.lock``; -1 when another holder has it; None when runDir is no folder
    (the runner then fails the run itself)."""
    if not run_dir.name or not run_dir.is_dir():
        return None
    fd = os.open(run_dir / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, LOCK_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return -1
    return fd


def _done_failed(reason: str) -> dict:
    return {"op": "done", "status": "failed", "error": reason, "errors": {}}


class _Session:
    def __init__(self, host: Any, proc: subprocess.Popen, scope: str, on_progress: Callable[[list], None] | None):
        self.host, self.proc, self.scope, self.on_progress = host, proc, scope, on_progress
        self.lock = threading.Lock()
        self.live: dict[int, str] = {}  # call id -> agent id of a spawn/resume in flight
        self.errors: dict[str, str] = {}  # row key -> agent error
        self.rows: dict[str, str] = {}  # runner agent id -> its row key in errors
        self.stderr: collections.deque[str] = collections.deque(maxlen=20)
        self.drain = threading.Thread(target=self._drain, daemon=True)
        self.drain.start()

    def register(self) -> None:
        with _LIVE_LOCK:
            _LIVE[self.scope] = self

    def write(self, line: str) -> None:
        with self.lock:
            try:
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
            except (OSError, ValueError):
                pass  # the runner is gone; serve() sees its stdout close

    def serve(self) -> dict:
        for line in self.proc.stdout:
            try:
                msg = json.loads(line, parse_constant=_no_constant)
            except ValueError:
                msg = None
            if not isinstance(msg, dict):
                return self._failed("the runner sent a line that is not a JSON object: %r" % line[:200])
            op = msg.get("op")
            if op == "done":
                return msg
            if op == "progress":
                self._call(lambda: self._progress(msg.get("entries")))
                if self.on_progress is not None:
                    self._call(lambda: self.on_progress(msg.get("entries")))
            elif op == "abort":
                self._call(lambda: self.host.abort(self._scoped(msg.get("agent"))))
            elif op in ("spawn", "resume", "gate", "load") and "id" in msg:
                if op == "spawn":
                    self.live[msg["id"]] = self._scoped((msg.get("req") or {}).get("agentId", ""))
                elif op == "resume":
                    self.live[msg["id"]] = self._scoped(msg.get("agent", ""))
                threading.Thread(target=contextvars.copy_context().run, args=(self._answer, msg), daemon=True).start()
        return self._failed("the runner exited without a done line (exit code %s)" % self._reap())

    def close(self) -> None:
        """Abort the agents still in flight (every exit path of drive), then end the runner."""
        with _LIVE_LOCK:
            if _LIVE.get(self.scope) is self:
                del _LIVE[self.scope]
        for agent in [a for a in list(self.live.values()) if a]:
            self._call(lambda agent=agent: self.host.abort(agent))
        with self.lock:
            try:
                self.proc.stdin.close()
            except (OSError, ValueError):
                pass
        self._reap()
        self.drain.join(1)
        self.proc.stdout.close()

    def _reap(self) -> int:
        """The runner's exit code, killing it when it does not end within EXIT_WAIT."""
        try:
            return self.proc.wait(EXIT_WAIT)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.wait()

    def _answer(self, msg: dict) -> None:
        op, ident = msg["op"], msg["id"]
        try:
            result = self._serve_call(op, msg)
            line = json.dumps({"id": ident, "ok": True, "result": result}, allow_nan=False)
        except Exception as exc:  # noqa: BLE001 - any host failure fails this call, not the run
            line = json.dumps({"id": ident, "ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
        finally:
            self.live.pop(ident, None)
        self.write(line + "\n")

    def _progress(self, entries: Any) -> None:
        for entry in entries if isinstance(entries, list) else ():
            if not isinstance(entry, dict) or entry.get("type") != "workflow_agent" or not entry.get("error"):
                continue
            agent = str(entry.get("agentId") or "")
            key = self.rows.get(agent)
            if key is None:
                label = str(entry.get("label") or agent)
                key, n = label, 1
                while key in self.errors:  # a suffixed key may be another agent's label
                    key = "%s#%s" % (label, entry.get("index") if n == 1 else "%s.%d" % (entry.get("index"), n))
                    n += 1
                self.rows[agent] = key
            self.errors[key] = str(entry["error"])

    def _serve_call(self, op: str, msg: dict) -> Any:
        if op == "spawn":
            return self.host.spawn({**msg["req"], "agentId": self._scoped(msg["req"].get("agentId")),
                                    "runId": self.scope})
        if op == "resume":
            return self.host.resume(self._scoped(msg["agent"]), msg["prompt"])
        if op == "gate":
            return self.host.gate(msg["command"], msg.get("cwd"))
        return self.host.load(msg["ref"])

    def _scoped(self, agent: Any) -> str:
        return "%s/%s" % (self.scope, agent) if agent else ""

    def _failed(self, reason: str) -> dict:
        self.proc.kill()
        tail = "".join(self.stderr).strip()[-500:]
        return {"op": "done", "status": "failed", "error": reason + ("; stderr: %s" % tail if tail else "")}

    def _drain(self) -> None:
        for line in self.proc.stderr:
            self.stderr.append(line)
        self.proc.stderr.close()

    @staticmethod
    def _call(fn) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 - an abort that fails must not end the bridge loop
            pass


def _no_constant(name: str) -> Any:
    raise ValueError("%s is not JSON" % name)
