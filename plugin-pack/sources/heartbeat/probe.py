"""Contract probe of the stock surface the heartbeat calls (NFR-005).

Each seam is bound with the call shape the plugin uses; a missing module or function, or a signature that
no longer accepts that shape, disables only the rules and actions that need it, and the tick prints one
warning line for all of them. Functions are looked up on their module at probe time.
"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass

#: every action the executor may take; each one writes a ``[heartbeat]`` comment (REQ-013)
ACTIONS = frozenset({"reclaim", "nudge", "continue", "reassign_same_role", "assign", "stop", "ask_human",
                     "archive", "cancel", "reassign_other", "git"})


@dataclass(frozen=True)
class Seam:
    module: str
    name: str
    positional: int          # positional arguments the plugin passes
    keywords: tuple          # keyword arguments the plugin passes
    needs: frozenset         # rule and action names that stop working without it


_KB = "hermes_cli.kanban_db"
SEAMS = (
    Seam("hermes_cli.kanban_diagnostics", "compute_task_diagnostics", 3, ("now", "graph", "config"),
         frozenset({"stock_diagnostics"})),
    Seam("hermes_cli.kanban_diagnostics", "config_from_runtime_config", 1, (), frozenset({"stock_diagnostics"})),
    Seam("hermes_cli.config", "load_config_readonly", 0, (), frozenset({"stock_diagnostics"})),
    Seam("hermes_cli.kanban_db_boards", "list_boards", 0, ("include_archived",), frozenset({"boards"})),
    Seam(_KB, "reclaim_task", 2, ("reason",), frozenset({"reclaim", "nudge", "continue"})),
    Seam(_KB, "reassign_task", 3, ("reclaim_first", "reason"), frozenset({"reassign_same_role", "reassign_other"})),
    Seam(_KB, "assign_task", 3, (), frozenset({"assign"})),
    Seam(_KB, "block_task", 2, ("reason", "kind"), frozenset({"stop", "ask_human"})),
    Seam(_KB, "unblock_task", 2, (), frozenset({"answer"})),
    Seam(_KB, "add_comment", 4, (), ACTIONS | {"answer"}),
    Seam("cron.jobs", "create_job", 2, ("name", "script", "no_agent", "deliver"), frozenset({"install"})),
    Seam("cron.jobs", "update_job", 2, (), frozenset({"install"})),
)


@dataclass(frozen=True)
class ProbeResult:
    disabled: frozenset
    warning: str | None


def _mismatch(seam: Seam) -> str | None:
    """Why ``seam`` no longer fits the call the plugin makes, or None when it does."""
    try:
        fn = getattr(importlib.import_module(seam.module), seam.name, None)
    except ImportError as exc:
        return "module missing (%s)" % exc
    if not callable(fn):
        return "missing"
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):  # no introspectable signature (a C function): nothing to compare
        return None
    try:
        signature.bind(*([None] * seam.positional), **dict.fromkeys(seam.keywords))
    except TypeError:
        return "signature %s" % signature
    return None


def check(seams: tuple = SEAMS) -> ProbeResult:
    broken = [(seam, why) for seam in seams if (why := _mismatch(seam)) is not None]
    if not broken:
        return ProbeResult(frozenset(), None)
    disabled = frozenset().union(*(seam.needs for seam, _ in broken))
    found = "; ".join("%s.%s: %s" % (seam.module, seam.name, why) for seam, why in broken)
    return ProbeResult(disabled, "[heartbeat] warning: the stock contract changed (%s); disabled: %s"
                       % (found, ", ".join(sorted(disabled))))
