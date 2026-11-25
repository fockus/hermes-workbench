"""The compaction plugin's spill module, the one home of the spill folder (shared-mechanisms.md).

code-mode never copies it: nested calls run inside its ``suppressed()`` so the spill hook leaves
their results to code-mode's own shaping, and overflow goes through its ``write`` / ``spill_root``.
Each Hermes profile in a process loads its own copies of both plugins (``hermes_plugins.<slug>``
for the first, ``…__home_<digest>`` for others), each spill module with its own suppression
ContextVar. So the spill module is the one of the profile whose plugin manager loaded this very
codemode copy, never the first one found: that profile's compaction, or None when it has none.
Imported as the repo package (dev, tests), ``compaction.plugin.spill`` serves. Without either, None.
"""

from __future__ import annotations

import contextlib
import importlib
import sys
from types import ModuleType
from typing import Any, ContextManager

_PACKAGE = __name__.rpartition(".")[0]


def compaction_spill() -> ModuleType | None:
    if _PACKAGE.startswith("hermes_plugins."):
        manager = _owner()
        loaded = _loaded(manager, lambda key, plugin: "compaction" in (key, plugin.manifest.name))
        module = getattr(loaded, "module", None)
        return sys.modules.get(module.__name__ + ".spill") if module is not None else None
    try:  # looked up like the live copy, so a type check of codemode never walks another plugin
        return importlib.import_module("compaction.plugin.spill")
    except ImportError:
        return None


def _owner() -> Any:
    """The plugin manager that loaded this codemode copy, among every profile's managers."""
    try:
        from hermes_cli import plugins
    except ImportError:
        return None
    managers = [plugins.get_plugin_manager(), *getattr(plugins, "_plugin_managers_by_home", {}).values()]
    for manager in managers:
        if _loaded(manager, lambda _key, plugin: getattr(plugin.module, "__name__", None) == _PACKAGE):
            return manager
    return None


def _loaded(manager: Any, match: Any) -> Any:
    for key, plugin in dict(getattr(manager, "_plugins", None) or {}).items():
        if getattr(plugin, "enabled", False) and match(key, plugin):
            return plugin
    return None


def suppressed() -> ContextManager[None]:
    """The spill hook's suppression block, or a no-op when compaction is absent."""
    spill = compaction_spill()
    if spill is None or not hasattr(spill, "suppressed"):
        return contextlib.nullcontext()
    return spill.suppressed()
