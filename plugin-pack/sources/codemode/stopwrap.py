"""The program Monty starts as its worker binary, so a worker never outlives its host.

A Monty worker running a script does not read its pipes, so when the host dies it keeps running
(tests/codemode/test_stop.py). ``stopwrap.py <monty binary> <args...>`` spawns the real worker with
this process's stdin and stdout (Monty talks to it directly) and waits: when the host is gone (this
process's parent changed, however the host ended) or SIGTERM arrives (``Repl.kill``), it kills the
worker, reaps it and exits; a worker that exits by itself ends the wrapper with its status. Run
with ``python -I -S``; stdlib only. ``launcher`` is the host side: the executable Monty is given.
"""

import os
import select
import shlex
import signal
import sys
import tempfile

KILLED = 128 + signal.SIGKILL
POLL_SECONDS = 0.2  # how soon a host death is noticed
TERMINATED: list[
    bool
] = []  # SIGTERM arrived (main installs the handler before the worker exists)


def launcher(binary: str) -> str:
    """The executable that starts ``binary`` under this wrapper, in a new ``0700`` folder the
    caller removes."""
    folder = tempfile.mkdtemp(prefix="codemode-")
    path = os.path.join(folder, "worker")
    words = (sys.executable, "-I", "-S", os.path.abspath(__file__), binary)
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700), "w") as fh:
        fh.write('#!/bin/sh\nexec %s "$@"\n' % " ".join(shlex.quote(w) for w in words))
    return path


def serve(child: int, parent: int, wake: int) -> int:
    """Wait for the worker to exit, a SIGTERM or the host's death; the wrapper's exit status."""
    while True:
        pid, status = os.waitpid(child, os.WNOHANG)
        if pid:
            code = os.waitstatus_to_exitcode(status)
            return code if code >= 0 else 128 - code
        if TERMINATED or os.getppid() != parent:
            os.kill(child, signal.SIGKILL)
            os.waitpid(child, 0)
            return KILLED
        if select.select([wake], [], [], POLL_SECONDS)[0]:
            os.read(wake, 512)


def main(argv: list[str]) -> int:
    binary, args = argv[1], argv[2:]
    parent = os.getppid()
    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_w, False)
    signal.set_wakeup_fd(wake_w)
    signal.signal(signal.SIGCHLD, lambda *_: None)  # wakes select when the worker exits
    signal.signal(signal.SIGTERM, lambda *_: TERMINATED.append(True))
    child = os.posix_spawn(binary, [binary, *args], os.environ)
    null = os.open(os.devnull, os.O_RDWR)
    os.dup2(null, 0)  # the worker's pipes close with the worker, not with this process
    os.dup2(null, 1)
    return serve(child, parent, wake_r)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
