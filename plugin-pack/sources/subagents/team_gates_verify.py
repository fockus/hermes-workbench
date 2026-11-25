"""A team card's verification command (Task 11; REQ-025): run in the workspace as its own process group, judged by
its exit code alone. Output is read in chunks and only a bounded tail is kept; the tail is decoded leniently and
redacted with Hermes's forced redaction, URL credentials included, before anyone (a Kanban comment, the teammate's
model) sees it. At the deadline, on an interrupt and after the command exits its group is killed, and so is every
process still holding the run's marker file, which each descendant inherits as an open descriptor (a child that left
the group with ``setsid`` too); the pipe is closed by the deadline even when a detached child still holds it. Split
from team_gates.py for size."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time

VERIFY_TIMEOUT = 600  # seconds one verification may run
TAIL = 1500  # characters of output kept for the reason
TAIL_BYTES = 16 * 1024  # bytes of raw output kept while it runs
GRACE = 2.0  # seconds the pipe may stay open after the command exited (a detached child holding it)
CHUNK = 64 * 1024


def verify(command: str, cwd: str, timeout: float = VERIFY_TIMEOUT) -> tuple[bool, str]:
    """``(passed, what happened)``: ``"exited N: <tail>"`` or ``"timed out after Ns: <tail>"``, the tail redacted;
    the command text is never part of it. No command passes."""
    if not command.strip():
        return True, "no verification command"
    with tempfile.NamedTemporaryFile(prefix="hermes-team-verify-") as mark:
        try:
            proc = subprocess.Popen(["/bin/sh", "-c", command], cwd=cwd, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
                                    pass_fds=(mark.fileno(),))
        except OSError as exc:
            return False, "the verification command could not run: %s" % _clean(str(exc).encode())
        tail = bytearray()
        try:
            code = _run(proc, tail, time.monotonic() + timeout)
        finally:
            _end(proc, os.path.realpath(mark.name))
    if code is None:
        return False, "timed out after %ss: %s" % (timeout, _clean(tail))
    return code == 0, "exited %d: %s" % (code, _clean(tail))


def _run(proc: subprocess.Popen, tail: bytearray, deadline: float) -> int | None:
    """Reads the output into the bounded ``tail`` until EOF; the exit code, or None at the deadline."""
    fd, exited_at = proc.stdout.fileno(), None
    with selectors.DefaultSelector() as selector:  # select.select fails on descriptors past FD_SETSIZE
        selector.register(fd, selectors.EVENT_READ)
        while True:
            now = time.monotonic()
            if now >= deadline:
                return None
            if proc.poll() is not None:
                exited_at = exited_at or now
                if now - exited_at >= GRACE:
                    break
            if selector.select(min(deadline - now, 0.25)):
                chunk = os.read(fd, CHUNK)
                if not chunk:
                    break
                tail += chunk
                del tail[:-TAIL_BYTES]
    try:
        return proc.wait(timeout=max(deadline - time.monotonic(), 0))
    except subprocess.TimeoutExpired:
        return None


def _end(proc: subprocess.Popen, mark: str) -> None:
    """Kills the command's whole group (a test runner's children too) and every descendant still holding ``mark``,
    reaps the command and closes the pipe."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    proc.stdout.close()
    # ponytail: a descendant that both leaves the session and closes its inherited descriptors escapes; ceiling: what
    # a marker descriptor can track without an OS job object; upgrade: a cgroup (Linux) or a launchd job (macOS)
    for _ in range(3):  # a survivor may fork once more between the scan and the kill
        found = _holders(mark) - {os.getpid()}
        for pid in found:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if not found:
            return
        time.sleep(0.05)


def _holders(path: str) -> set[int]:
    """Pids of the processes that hold ``path`` open: /proc on Linux, lsof elsewhere; empty when neither works."""
    if sys.platform.startswith("linux"):
        found = set()
        for pid in filter(str.isdigit, os.listdir("/proc")):
            try:
                if any(os.readlink("/proc/%s/fd/%s" % (pid, fd)) == path for fd in os.listdir("/proc/%s/fd" % pid)):
                    found.add(int(pid))
            except OSError:
                continue
        return found
    try:
        out = subprocess.run(["lsof", "-t", "--", path], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {int(line) for line in out.split() if line.isdigit()}


def _clean(raw: bytes | bytearray) -> str:
    """The redacted last ``TAIL`` characters, cut at line starts so no secret is split in half by the cut."""
    from agent.redact import redact_sensitive_text

    text = bytes(raw).decode("utf-8", errors="replace")
    if len(raw) >= TAIL_BYTES:
        text = text.partition("\n")[2]  # the first line may be a fragment
    text = redact_sensitive_text(text, force=True, redact_url_credentials=True).strip()
    if len(text) > TAIL:
        text = text[-TAIL:].partition("\n")[2] or text[-TAIL:]
    return text
