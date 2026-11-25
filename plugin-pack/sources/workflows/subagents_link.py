"""The ``SubagentsAPI`` class of the subagents plugin this profile loaded.

Hermes imports a directory plugin as ``hermes_plugins.<name>`` (``hermes_plugins.<name>__home_<digest>`` from a
second Hermes home), with only its own folder as search path, so ``import subagents`` works in a source checkout
only. The class is read from the profile's plugin manager, else from ``sys.modules`` the way soul-composer finds
a provider, and only then from the checkout.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Mapping
from typing import Any, Callable

NAME = "subagents"
LOADED = "hermes_plugins." + NAME


def api_class(ctx: Any) -> Callable[..., Any]:
    """``SubagentsAPI``; raises ImportError when no subagents plugin is loaded or importable."""
    module = _loaded(ctx)
    if module is None:
        return importlib.import_module(NAME + ".api").SubagentsAPI
    return importlib.import_module(module.__name__ + ".api").SubagentsAPI


def _loaded(ctx: Any) -> Any:
    plugins = getattr(getattr(ctx, "_manager", None), "_plugins", None)
    if isinstance(plugins, Mapping):  # the context's own home decides: never another home's module
        entry = next((p for key, p in plugins.items()
                      if key == NAME or getattr(getattr(p, "manifest", None), "name", None) == NAME), None)
        return getattr(entry, "module", None) if getattr(entry, "enabled", False) else None
    return sys.modules.get(LOADED) or next(
        (sys.modules[key] for key in sorted(sys.modules)
         if key.startswith(LOADED + "__home_") and "." not in key[len(LOADED):]), None)
