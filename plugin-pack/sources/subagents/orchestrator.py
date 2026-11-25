"""The main agent as orchestrator (REQ-005..007): the Orchestrator SOUL brick, the console and the
stale-console reminder.

* ``render_soul_brick()`` is this plugin's soul-composer brick, None while orchestrator_mode is off for the
  current Hermes home (a second home in one process imports its own module copy). Without the composer (SOUL.md lacks
  its header) the brick is kept in ``$HERMES_HOME/SOUL.md`` between markers, rewritten only on change,
  under the writer contract SOUL.md's writers share (soul.py); a missing SOUL.md is never created
  (Hermes would lose its built-in identity).
* The bot's ``orchestrator-console.md`` reaches the model through ``pre_llm_call`` on the first call
  after session start and after each compaction, capped at 40 lines and 4000 bytes. Plugin prompt
  sections are frozen per session (``hermes_cli/plugins_dispatch.py:99-102``), so it never goes there.
* Launches and returns of the bot's subagents since the console last changed add one reminder line
  before the next reply.
* A kanban worker or a cron run has nobody to converse with: it gets neither the console nor the reminder.
* ``orchestrator_mode: off`` leaves stock Hermes as it is: no brick, no console, no reminder, and a block an earlier run
  left in SOUL.md is removed (only its marked lines, under the same writer contract).

The bank is the memory-bank plugin's own ``resolve_bank`` when that plugin is loaded, else the
session folder's own ``.memory-bank`` when it exists.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import soul as soul_writer
from .limiter import process_shared

logger = logging.getLogger("subagents.orchestrator")

BLOCK_START, BLOCK_END = "<!-- orchestrator:start -->", "<!-- orchestrator:end -->"
CONSOLE = "orchestrator-console.md"
CONSOLE_BYTES, CONSOLE_LINES = 4000, 40
MB_PLUGIN = "hermes_plugins.memory_bank"  # the memory-bank plugin as Hermes' loader names it
# Hermes homes whose latest Orchestrator was built with orchestrator_mode off; one set for every module copy
_OFF_HOMES: set[str] = process_shared("orchestrator_off_homes", set)


def _home() -> str:
    from hermes_constants import hermes_home_key  # the resolved key PluginManager binds during register(ctx)

    return hermes_home_key()


def render_soul_brick() -> str | None:
    """The Orchestrator rules between their markers (soul-composer brick protocol); None while
    orchestrator_mode is off. The mode is known only at register(ctx), so Orchestrator() records it per
    Hermes home in one process-wide set: the composer may call any home's module copy, and keying by home
    keeps one home's off from reaching another."""
    if _OFF_HOMES and _home() in _OFF_HOMES:
        return None
    body = (Path(__file__).with_name("soul_block.md")).read_text(encoding="utf-8").strip()
    return "%s\n%s\n%s" % (BLOCK_START, body, BLOCK_END)


def sync_soul(soul: Path, render: Callable[[], str | None] = render_soul_brick,
              wait: float = soul_writer.LOCK_WAIT) -> bool:
    """Keeps the brick in a hand-written SOUL.md under the shared writer contract (soul.py); True when rewritten."""
    return soul_writer.sync(soul, render, wait)


def drop_soul(soul: Path, wait: float = soul_writer.LOCK_WAIT) -> bool:
    """Removes the brick from a hand-written SOUL.md under the same writer contract; True when rewritten."""
    return soul_writer.sync(soul, lambda: None, wait)


def resolve_bank(cwd: str) -> Path | None:
    """The bot's Memory Bank for ``cwd``: the memory-bank plugin's resolver, else ``<cwd>/.memory-bank`` when it exists."""
    if not cwd:
        return None  # no workspace: no bank (never the daemon's working directory)
    module = sys.modules.get(MB_PLUGIN) or next(
        (m for name, m in list(sys.modules.items()) if name.startswith(MB_PLUGIN + "__home_")), None)
    resolve = getattr(module, "resolve_bank", None)
    if callable(resolve):
        try:
            return Path(resolve(cwd)[0])
        except (OSError, TypeError, ValueError, IndexError) as exc:
            logger.warning("subagents: memory-bank resolve_bank(%r) failed: %s", cwd, exc)
            return None
    folder = Path(cwd) / ".memory-bank"
    return folder if folder.is_dir() else None


def capped(text: str) -> str:
    """At most 40 lines and 4000 bytes of ``text``, the one note line that says what was cut included."""
    lines = text.rstrip("\n").splitlines()
    if len(lines) <= CONSOLE_LINES and len("\n".join(lines).encode("utf-8")) <= CONSOLE_BYTES:
        return "\n".join(lines)
    note = "[console truncated: %d of %d lines shown; read %s for the rest]"
    room = CONSOLE_BYTES - len((note % (len(lines), len(lines), CONSOLE)).encode("utf-8")) - 1  # the widest note
    kept = lines[:CONSOLE_LINES - 1]
    while len("\n".join(kept).encode("utf-8")) > room and len(kept) > 1:
        kept.pop()
    body = "\n".join(kept).encode("utf-8")[:room].decode("utf-8", errors="ignore")
    return "%s\n%s" % (body, note % (len(body.splitlines()), len(lines), CONSOLE))


class Orchestrator:
    """Hook handlers: on_session_start, post_compaction, subagent_start/subagent_stop, pre_llm_call."""

    def __init__(self, cwd: Callable[[], str], is_child: Callable[[str], bool] = lambda session_id: False,
                 unattended: Callable[[], bool] = lambda: False, enabled: bool = True):
        self.cwd = cwd
        self.enabled = enabled  # False for orchestrator_mode off
        if not enabled:
            _OFF_HOMES.add(_home())
        elif _OFF_HOMES:  # mode on with no home ever off touches nothing
            _OFF_HOMES.discard(_home())
        self.is_child = is_child
        self.unattended = unattended
        self._lock = threading.Lock()
        self._inject: set[str] = set()
        self._events: dict[str, list[float]] = {}

    @staticmethod
    def _soul() -> Path:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home()) / "SOUL.md"

    def on_load(self) -> None:
        """At plugin load, before any session: Hermes builds a session's system prompt from SOUL.md before
        on_session_start fires, so off mode removes the block here too."""
        if not self.enabled:
            drop_soul(self._soul())

    def on_session_start(self, session_id: str = "", **_: Any) -> None:
        if self.is_child(session_id):
            return
        if not self.enabled:
            drop_soul(self._soul())
            return
        sync_soul(self._soul())
        with self._lock:
            self._inject.add(session_id)

    def post_compaction(self, session_id: str = "", old_session_id: str = "", **_: Any) -> None:
        if self.enabled and not (self.is_child(session_id) or self.is_child(old_session_id)):
            with self._lock:
                self._inject.add(session_id)

    def subagent_event(self, parent_session_id: str = "", **_: Any) -> None:
        """A launch or a return of one of the bot's own subagents."""
        if self.enabled and parent_session_id and not self.is_child(parent_session_id):
            with self._lock:
                self._events.setdefault(parent_session_id, []).append(time.time())

    def pre_llm_call(self, session_id: str = "", **_: Any) -> dict[str, str] | None:
        if self.is_child(session_id):
            return None
        with self._lock:
            inject = session_id in self._inject
            self._inject.discard(session_id)
            events = self._events.pop(session_id, [])
        if not (inject or events) or self.unattended():
            return None
        bank = resolve_bank(self.cwd())
        if bank is None:
            return None
        console = bank / CONSOLE
        parts = []
        if inject and console.is_file():
            try:
                text = console.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:  # kept pending: a later call of this session tries again
                logger.warning("subagents: console %s unreadable: %s", console, exc)
                with self._lock:
                    self._inject.add(session_id)
            else:
                parts.append(capped("Orchestrator console (%s):\n%s" % (console, text)))
        changed = console.stat().st_mtime if console.is_file() else 0.0
        stale = [t for t in events if t > changed]
        if stale:
            parts.append("Reminder: %d subagent launch(es) or return(s) since %s last changed; update the console "
                         "before you reply." % (len(stale), console))
        return {"context": "\n\n".join(parts)} if parts else None
