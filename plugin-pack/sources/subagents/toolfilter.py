"""The typed-child tool filter on code-mode's tool-list hook (REQ-014, shared-mechanisms.md).

Hermes imports code-mode as ``hermes_plugins.codemode`` (``hermes_plugins.codemode__home_<digest>`` from a
second Hermes home), with only its own folder as search path, so the module is found in ``sys.modules`` —
the way orchestrator.py finds memory-bank — and never by ``import codemode``, which only works in a source
checkout (tests, development). Plugins load in any order, so the filter is attached lazily: at register
time and again at every main-agent model call and child start until each code-mode module has it.
"""

from __future__ import annotations

import sys
import threading
from typing import Any, Callable

CODEMODE = "hermes_plugins.codemode"
DEV_MODULE = "codemode"  # the source checkout's package: tests and development only


def codemode_modules() -> list[Any]:
    """The loaded code-mode plugin modules (package roots only, never their submodules)."""
    found = [module for name, module in list(sys.modules.items())
             if name == CODEMODE or (name.startswith(CODEMODE + "__home_") and "." not in name[len(CODEMODE):])]
    return found or [m for m in (sys.modules.get(DEV_MODULE),) if m is not None]


class CodeModeLink:
    """Registers ``tool_filter`` once with every code-mode module that offers ``register_tool_filter``."""

    def __init__(self, tool_filter: Callable[..., Any]):
        self.tool_filter = tool_filter
        self._lock = threading.Lock()
        self._attached: list[Any] = []  # the modules holding the filter (a reload is a new module)
        self._dev_import_tried = False

    def attach(self) -> bool:
        """True when at least one code-mode module carries the filter."""
        modules = codemode_modules() or self._dev_import()
        with self._lock:
            for module in modules:
                register = getattr(module, "register_tool_filter", None)
                if (callable(register) and not any(module is held for held in self._attached)
                        and register(self.tool_filter) is not False):
                    self._attached.append(module)
            return len(self._attached) > 0

    def hook(self, **_: Any) -> None:
        """pre_llm_call / subagent_start: attach to a code-mode loaded after this plugin."""
        self.attach()

    def _dev_import(self) -> list[Any]:
        if self._dev_import_tried:
            return []
        self._dev_import_tried = True
        try:
            import codemode  # noqa: PLC0415 - the source checkout's package; a live Hermes never finds it
        except ImportError:
            return []
        return [codemode]
