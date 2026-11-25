"""How often this user's scripts call each SDK tool, so the catalog lists the most used first (REQ-009).

Kept in ``$HERMES_HOME/codemode/usage.json`` (0600, with ``usage.json.lock`` serialising writers);
a missing or unreadable file means no usage yet. Writers name the file they were given when they
were built (runtime.py), never the home of whatever runs at write time.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Iterable

_LOCK = threading.Lock()


def usage_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "codemode" / "usage.json"


def load(path: Path | None = None) -> dict[str, int]:
    try:
        data = json.loads((path or usage_path()).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, int)}


def record(names: Iterable[str], path: Path) -> None:
    """Add one use per name (qualified SDK names) to ``path`` and persist atomically. The
    read-modify-write is serialised across threads and across Hermes processes sharing the profile
    (a lock file)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _LOCK, open(path.with_name(path.name + ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        counts = load(path)
        for name in names:
            counts[name] = counts.get(name, 0) + 1
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".usage-")  # mkstemp creates it 0600
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(counts, fh, sort_keys=True)
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
