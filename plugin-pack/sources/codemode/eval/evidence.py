"""Per-attempt evidence (AGR-103): what the model called, what came back, what it answered and why the
attempt was or was not accepted. Counters alone cannot tell a checker artefact from a real failure.
Every text is scrubbed (``codemode.result.scrub``) before it is cut, so a secret never straddles a cut.
``build`` returns the part the runner knows; ``run_eval`` adds ``task``, ``mode`` and ``rep``."""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from . import tally

MAX_CALLS = 40
HEAD = 200
ANSWER = 500


def model_calls(messages: Sequence[Any]) -> int:
    return sum(
        len(m.get("tool_calls") or [])
        for m in messages
        if isinstance(m, Mapping) and m.get("role") == "assistant"
    )


def turn_failure(out: Mapping[str, Any]) -> str | None:
    """Why stock returned this turn as failed or partial instead of raising, if it did."""
    if out.get("failed") or out.get("partial") or out.get("completed") is False:
        return "the turn did not complete: %s" % (out.get("error") or "failed")
    return None


def _head(value: Any, limit: int = HEAD) -> str:
    """``value`` scrubbed, then cut. A JSON text is decoded first and scrubbed field by field (a secret in
    a written file hides behind ``\\n`` escapes in the encoded text), then encoded again."""
    from ..result import _clean, scrub

    text = value if isinstance(value, str) else "" if value is None else str(value)
    try:
        decoded = json.loads(text)
    except ValueError:
        decoded = None
    if isinstance(decoded, (dict, list)):
        return json.dumps(_clean(decoded), ensure_ascii=False)[:limit]
    return scrub(text)[:limit]


def _tool_calls(messages: Sequence[Any], failed_ids: Mapping[str, str]) -> tuple[list[dict[str, Any]], int]:
    results = {str(m.get("tool_call_id")): m.get("content") for m in messages
               if isinstance(m, Mapping) and m.get("role") == "tool"}
    calls = [c for m in messages if isinstance(m, Mapping) and m.get("role") == "assistant"
             for c in m.get("tool_calls") or [] if isinstance(c, Mapping)]
    kept = []
    for call in calls[:MAX_CALLS]:
        function, call_id = call.get("function") or {}, str(call.get("id"))
        kept.append({"name": function.get("name") or "?", "args_head": _head(function.get("arguments")),
                     "result_head": _head(results.get(call_id)),
                     "failed": call_id in failed_ids or tally.failed(results.get(call_id))})
    return kept, len(calls)


def build(messages: Sequence[Any], failed_ids: Mapping[str, str], record: Mapping[str, Any], answer: str,
          accepted: bool, error: str | None) -> dict[str, Any]:
    """The evidence of one attempt from its ``messages``, the ``failed_ids`` of its tool-completion events,
    its finished run ``record``, the model's final ``answer``, the check's verdict and the run's ``error``."""
    tool_calls, total = _tool_calls(messages, failed_ids)
    return {
        "tool_calls": tool_calls,
        "tool_calls_total": total,
        "requests": record.get("requests", 0),
        "fresh_tokens": record["fresh_tokens"],
        "cached_tokens": record["cached_tokens"],
        "cost": record["cost"],
        "seconds": record["seconds"],
        "final_answer": _head(answer, ANSWER),
        "accepted": accepted,
        "reason": _head(error, ANSWER) if error else "accepted" if accepted else "check returned false",
    }
