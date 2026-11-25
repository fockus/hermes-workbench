"""Agent definitions per spawning session (Astra review round 1, __init__.py:104).

A gateway manager serves sessions of several workspaces and profiles, so the definitions are read from the
calling session's own workspace (stock ``session_cwd``) and Hermes home (stock ``get_hermes_home``, which a
profile overrides per context). A spawn's pre_tool_call reads its definition once (enforce.py); its route and its
child's enforcement both use that snapshot (calls.py ``peek``).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from .registry import AgentDef, Registry

KEPT = 64  # workspace × profile registries remembered (a gateway serves many)


class Registries:
    """``find``/``names`` of the registry the calling session sees."""

    def __init__(self, cwd: Callable[[], str]):
        self.cwd = cwd
        self._lock = threading.Lock()
        self._by_key: dict[tuple[str, str], Registry] = {}

    def current(self) -> Registry:
        from hermes_constants import get_hermes_home

        cwd = self.cwd()
        key = (str(Path(cwd).resolve()) if cwd else "", str(Path(get_hermes_home()).resolve()))
        with self._lock:
            found = self._by_key.pop(key, None) or Registry(Path(key[0]) if key[0] else None,
                                                            hermes_home=Path(key[1]))
            self._by_key[key] = found  # newest last
            while len(self._by_key) > KEPT:
                self._by_key.pop(next(iter(self._by_key)))
        return found

    def find(self, name: str) -> AgentDef:
        return self.current().find(name)

    def names(self) -> list[str]:
        return self.current().names()
