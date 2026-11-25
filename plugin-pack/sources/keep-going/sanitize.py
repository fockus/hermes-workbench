"""What the keep-going watchdog quotes from session content (judge-C KG4; loaded by path from ``wiring``).

- ``decoded(redact, text)``: a tool result redacted whole; a JSON one first field by field with its keys.
- ``revision(value)``: a todo revision as the watchdog may quote it.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Callable

REVISION = re.compile(r"[\w.:-]{1,32}")


def revision(value):
    """A todo revision as the watchdog may quote it: an int or a short token as is, anything else its digest."""
    if value is None or (isinstance(value, int) and not isinstance(value, bool)) or (
            isinstance(value, str) and REVISION.fullmatch(value)):
        return value
    return "#" + hashlib.sha1(repr(value).encode("utf-8", "replace")).hexdigest()[:12]


def decoded(redact: Callable[[str], str], text: str) -> str:
    """A tool result redacted whole; a JSON one first field by field as decoded text, a dict value also as the
    pair ``key: value``. compaction's ``spill.redact_fields`` does the same; plugins cannot import each other."""
    def field(key, value):
        value, prefix = redact(value), "%s: " % key
        pair = redact(prefix + value)
        return value if pair == prefix + value else pair[len(prefix):] if pair.startswith(prefix) else "[REDACTED]"

    def walk(value):
        if isinstance(value, dict):
            return {k: field(k, v) if isinstance(v, str) else walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return redact(value) if isinstance(value, str) else value
    try:
        data = json.loads(text)
    except ValueError:
        return redact(text)
    clean = walk(data)
    return redact(json.dumps(clean, ensure_ascii=False) if clean != data else text)
