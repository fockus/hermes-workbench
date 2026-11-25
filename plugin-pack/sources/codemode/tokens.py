"""Token counts as Hermes estimates them (``agent.model_metadata.estimate_tokens_rough``: CJK
characters one token each, other text by UTF-8 bytes), so code-mode's budgets (result shaping,
the SDK catalog) match what the context will be charged. Only where Hermes is not importable at
all (modes.py runs on a bare interpreter in its tests) the plain four-characters rule serves.
"""

from __future__ import annotations

import sys


def estimate(text: str) -> int:
    module = sys.modules.get("agent.model_metadata")
    if module is None:
        try:
            from agent import model_metadata
        except ImportError:
            return (len(text) + 3) // 4
        module = model_metadata
    return module.estimate_tokens_rough(text)
