"""Gate commands of workflow agents (REQ-028): ``run(command, cwd, timeout, mask) -> {ok, output}``.

A gate is a shell command line the script author wrote (``gate: 'npm test'``). It runs as ``/bin/sh -c`` in
its own process group, which is killed when it outlives ``timeout`` and again once it ends, so no child of
the gate is left behind. Its stdout and stderr are read in chunks under the same deadline and only the last
``WINDOW`` bytes are kept, so a gate that floods output cannot exhaust memory. ``output`` is that window
passed through ``mask`` first and then cut to its last ``OUTPUT_CHARS`` characters, so the cut never splits
a ``password=`` from its value before the masker sees them.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from typing import Callable

from .limits import AGENT_TIMEOUT

OUTPUT_CHARS = 1500
WINDOW = 65536  # raw bytes kept for masking; a flood beyond it is dropped from the head
CHUNK = 65536
TIMEOUT = float(AGENT_TIMEOUT)


def run(command: str, cwd: str | None, timeout: float = TIMEOUT, mask: Callable[[str], str] = str) -> dict:
    try:
        proc = subprocess.Popen(["/bin/sh", "-c", command], cwd=cwd or None, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as exc:
        return {"ok": False, "output": _tail(mask("the gate did not start: %s" % exc))}
    try:
        kept, ended, cut = _read(proc, time.monotonic() + timeout)
    finally:
        _kill(proc)
        proc.stdout.close()
    text = kept.decode("utf-8", errors="replace")
    if cut:  # the window's first line may be the tail of a secret whose key was dropped: drop it too
        text = text.split("\n", 1)[-1]
    if not ended:
        proc.wait()
        return {"ok": False, "output": _tail(mask("%s\nthe gate timed out after %gs: %s" % (text, timeout, command)))}
    return {"ok": proc.returncode == 0, "output": _tail(mask(text))}


def _read(proc: subprocess.Popen, deadline: float) -> tuple[bytearray, bool, bool]:
    """The last ``WINDOW`` bytes the gate wrote, whether it ended before ``deadline``, whether bytes were dropped."""
    kept, fd, cut = bytearray(), proc.stdout.fileno(), False
    with selectors.DefaultSelector() as sel:
        sel.register(fd, selectors.EVENT_READ)
        while (left := deadline - time.monotonic()) > 0:
            if not sel.select(left):
                continue
            chunk = os.read(fd, CHUNK)
            if not chunk:  # the pipe closed; the gate may still run
                try:
                    proc.wait(max(0.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    return kept, False, cut
                return kept, True, cut
            kept += chunk
            if len(kept) > WINDOW:
                del kept[:len(kept) - WINDOW]
                cut = True
    return kept, False, cut


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass  # the group is already gone


def _tail(text: str) -> str:
    text = text.strip()
    return text if len(text) <= OUTPUT_CHARS else "…" + text[-(OUTPUT_CHARS - 1):]
