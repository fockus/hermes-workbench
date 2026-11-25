"""What a codemode call returns to the model (spec Task 8; REQ-014, REQ-015, REQ-016, REQ-032).

Only the script's last expression (JSON, up to RESULT_TOKENS), its print output (up to LOG_TOKENS,
with the executor's warnings first), one metrics line and, on failure, the diagnostic with the
nested calls already started (the tool error text rides in the diagnostic) reach the context.
Raw nested results never do: the bridge hands them to the script only. A text over its cap keeps
its head and tail around a note; the whole text goes to a 0600 file through the compaction spill
module (spill_link.py, ``$HERMES_HOME/cache/recall-spill/<session>/``, its retention and cap) and
the path rides in the result. Without compaction the note says the text was not saved.

The script's value is redacted decoded, before its one JSON encoding: every string and key gets
Hermes's execute_code redaction, forced, then its strict derived-text pass (``scrub``; quoting would
hide newlines from the line rules), and each string under a dict key is checked again as that
key/value pair, so an opaque ``password`` value is masked too. The encoded JSON is never redacted
again (a rule reading ``KEY=***\\n...`` across an escaped newline would break the JSON); an overflow
preview is a JSON envelope of the encoded text's head and tail. The spill module redacts what it
stores once more, so a result is spilled only in a form that stays valid JSON through it (``=``
written as ``\\u003d`` when needed), else not at all. Logs and the error are text: scrubbed whole,
then cut and spilled. A key JSON cannot encode (a tuple) is written as its repr; keys that would
collide make the object a list of ``[key, value]`` pairs.
"""

from __future__ import annotations

import json
import sys
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import spill_link
from .repl import RunOutcome
from .tokens import estimate

RESULT_TOKENS = 10_000
LOG_TOKENS = 2_000  # print output, and the error text the same way
ERROR_FIELD_CHARS = 2048  # stock registry dispatch cuts a JSON "error" past this (tools.registry)
STARTED_CHARS = 400  # room the started-calls summary may take of it


@dataclass(frozen=True)
class ScriptResult:
    result_json: str | None
    logs: str
    metrics: str
    error: str | None
    spill_paths: list[Path] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "result_json": self.result_json,
                "logs": self.logs,
                "metrics": self.metrics,
                "error": self.error,
                "spill_paths": [str(p) for p in self.spill_paths],
            },
            ensure_ascii=False,
        )


def shape(outcome: RunOutcome, session_id: str) -> ScriptResult:
    spill = _Spill(session_id)
    result_json = None
    # a script that caught the call-limit stop and finished keeps its value (bulk's categories)
    if outcome.error is None or (outcome.stopped == "calls" and outcome.value is not None):
        result_json = spill.fit_json(json.dumps(_clean(outcome.value), ensure_ascii=False), RESULT_TOKENS)
    skipped = not_run(outcome.refused)
    logs = (
        "".join("codemode warning: %s\n" % w for w in outcome.warnings)
        + ("codemode warning: %s\n" % skipped.lstrip("\n") if skipped and outcome.error is None else "")
        + outcome.logs
    )
    error = None
    if outcome.error is not None:  # the summary rides inside the error field Hermes keeps
        summary = skipped + started(outcome.calls)
        error = spill.fit(outcome.error, LOG_TOKENS, "error", _error_chars() - len(summary)) + summary
    return ScriptResult(
        result_json,
        spill.fit(logs, LOG_TOKENS, "logs"),
        metrics(outcome),
        error,
        spill.paths,
    )


def started(calls: list[str]) -> str:
    """The nested calls a failed script had started, in first-call order with counts."""
    if not calls:
        return "\nnested calls started: none"
    counts = Counter(calls)
    names = ", ".join(
        name if n == 1 else "%s x%d" % (name, n) for name, n in counts.items()
    )
    head = "\nnested calls started (%d): " % len(calls)
    room = STARTED_CHARS - len(head)
    return head + (names if len(names) <= room else names[: room - 1] + "…")


def not_run(refused: list[str]) -> str:
    """The nested calls that were refused or never started, with why; also on a successful run."""
    if not refused:
        return ""
    head = "\nnested calls not run (%d): " % len(refused)
    text, room = scrub("; ".join(refused)), STARTED_CHARS - len(head)  # rides after fit()'s scrub
    return head + (text if len(text) <= room else text[: room - 1] + "…")


def _error_chars() -> int:
    """How much of a JSON "error" field stock registry dispatch passes on unchanged."""
    cap = getattr(sys.modules.get("tools.registry"), "_MAX_TOOL_ERROR_CHARS", ERROR_FIELD_CHARS)
    return cap if isinstance(cap, int) and cap > 0 else ERROR_FIELD_CHARS


def metrics(outcome: RunOutcome) -> str:
    return "calls %d · duration %.2fs · limits %gs / %d calls" % (
        len(outcome.calls),
        outcome.seconds,
        outcome.limits.seconds,
        outcome.limits.calls,
    )


def scrub(text: str) -> str:
    """Hermes's execute_code redaction, forced, then its strict derived-text pass."""
    from agent.redact import redact_code_output, redact_derived_text

    return redact_derived_text(redact_code_output(text, force=True))


def _clean(value: Any) -> Any:
    """``value`` redacted decoded, with only JSON-encodable keys, ready for its one encoding."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        pairs = [(_key(k), _entry(_key(k), v)) for k, v in value.items()]
        keys = [k for k, _ in pairs]
        return dict(pairs) if len(set(keys)) == len(keys) else [list(p) for p in pairs]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_clean(v) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return scrub(str(value))


def _entry(key: Any, value: Any) -> Any:
    """A dict value, a string one also redacted as its key/value pair (an opaque password).

    The value of a credential field (``{"Authorization": ["Bearer …"]}``) is masked whole: the pair
    rules keep a short or scheme-prefixed string, and list items lose the field name. Numbers stay:
    ``input_cost_per_token`` is a price."""
    from agent.redact import is_secret_field_name

    if is_secret_field_name(key) and not (value is None or isinstance(value, (bool, int, float))):
        return "***"
    clean = _clean(value)
    if not isinstance(clean, str):
        return clean
    # "=" escaped: the decoded value already had the assignment rules; only the pair rules remain
    pair = json.dumps({str(key): clean}, ensure_ascii=False).replace("=", "\\u003d")
    masked = scrub(pair)
    if masked == pair:
        return clean
    try:
        [(_, out)] = json.loads(masked).items()
    except (ValueError, AttributeError):
        return "***"  # masked in a way that cannot be read back: withhold the value
    return out if isinstance(out, str) else "***"


def _key(key: Any) -> Any:
    if isinstance(key, str):
        return scrub(key)
    return key if key is None or isinstance(key, (bool, int, float)) else scrub(repr(key))


class _Spill:
    """Caps the texts of one result; the overflowing ones go whole to the session's spill folder."""

    def __init__(self, session_id: str):
        self.session_id, self.paths = session_id, list[Path]()
        self._stem = "codemode-%s-" % uuid.uuid4().hex[:12]

    def fit(self, text: str, cap_tokens: int, name: str, max_chars: int | None = None) -> str:
        """``text`` scrubbed, within ``cap_tokens`` (Hermes's estimate) and ``max_chars``; an
        overflowing text keeps its head and tail around a note, and goes whole to the spill folder."""
        text = scrub(text)
        limit = len(text) if max_chars is None else max(max_chars, 0)
        if len(text) <= limit and estimate(text) <= cap_tokens:
            return text
        where = self._write(text, name)
        return _cut(text, cap_tokens, limit, lambda head, tail: "%s\n[codemode: %d of %d characters cut from the "
                    "middle; %s]\n%s" % (head, len(text) - len(head) - len(tail), len(text), where, tail))

    def fit_json(self, text: str, cap_tokens: int) -> str:
        """Encoded JSON (already redacted) within ``cap_tokens``; an overflowing one becomes a
        valid JSON preview envelope, and goes whole to the spill folder when it stays valid there."""
        if estimate(text) <= cap_tokens:
            return text
        where = self._write_json(text)
        return _cut(text, cap_tokens, len(text), lambda head, tail: json.dumps({"codemode_preview": {
            "head": head, "tail": tail, "omitted_chars": len(text) - len(head) - len(tail),
            "of_chars": len(text), "full_text": where}}, ensure_ascii=False))

    def _write_json(self, text: str) -> str:
        spill = spill_link.compaction_spill()
        stored = getattr(spill, "redact", None)
        for candidate in (text, text.replace("=", "\\u003d")):  # "=" only occurs inside JSON strings
            try:
                json.loads(stored(candidate) if callable(stored) else candidate)
            except ValueError:
                continue
            return self._write(candidate, "result")
        return "the full result was not saved (it would not stay valid JSON through the spill redaction)"

    def _write(self, text: str, name: str) -> str:
        spill = spill_link.compaction_spill()
        if spill is None:
            return "the full text was not saved (the compaction spill folder is not available)"
        try:
            path = Path(
                spill.write(text, session_id=self.session_id, name=self._stem + name)
            )
        except (OSError, ValueError) as exc:
            return "the full text was not saved (%s)" % type(exc).__name__
        self.paths.append(path)
        return "full text: %s" % path


def _cut(text: str, cap_tokens: int, limit: int, frame: Any) -> str:
    """``frame(head, tail)`` with the longest equal head and tail of ``text`` that fit."""
    ratio = max(estimate(text), 1) / max(len(text), 1)  # tokens per character of this text
    keep = int(min(cap_tokens / ratio, limit, len(text) - 1) // 2)
    while True:
        out = frame(text[:keep], text[len(text) - keep:] if keep else "")
        if keep == 0 or (len(out) <= limit and estimate(out) <= cap_tokens):
            return out
        keep = keep * 9 // 10 if keep > 10 else 0
