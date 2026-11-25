"""cache-monitor: per request, log the prompt-cache share and the first part of the prefix that changed.

Writes one JSON line per model request to $HERMES_HOME/logs/cache-monitor.jsonl:
{"ts", "session", "request", "model", "prompt_tokens", "cached_tokens", "cache_share", "break"}
where "break" is null for an append-only request, "first_request", "instructions", "tools", "field:<name>" or "input[i]".
The hook payload is truncated by Hermes (HERMES_PLUGIN_PAYLOAD_MAX_CHARS), which is enough to find the
first diverging part of a prefix.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

_lock = threading.Lock()
_last: dict[str, dict] = {}      # ponytail: last request shape per session, kept for the gateway's lifetime
_pending: dict[str, dict] = {}   # api_request_id -> partial row until post_api_request


def _log_path() -> Path:
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    except ImportError:
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "logs" / "cache-monitor.jsonl"


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]


def _shape(request: dict) -> dict:
    if isinstance(request.get("body"), dict):  # Hermes wraps the provider payload
        request = request["body"]
    items = request.get("input") or request.get("messages") or []
    return {
        "instructions": _digest(request.get("instructions") or request.get("system")),
        "tools": _digest(request.get("tools")),
        "input": [_digest(item) for item in items] if isinstance(items, list) else [],
        "fields": {key: _digest(value) for key, value in request.items()
                   if key not in ("input", "messages", "instructions", "system", "tools")},
    }


def _first_break(previous: dict | None, current: dict) -> str | None:
    if previous is None:
        return "first_request"
    for part in ("instructions", "tools"):
        if previous[part] != current[part]:
            return part
    for key in sorted(set(previous["fields"]) | set(current["fields"])):
        if previous["fields"].get(key) != current["fields"].get(key):
            return f"field:{key}"
    for index, item in enumerate(previous["input"]):
        if index >= len(current["input"]) or current["input"][index] != item:
            return f"input[{index}]"
    return None


def _first_diff(old: str, new: str) -> int:
    limit = min(len(old), len(new))
    return next((i for i in range(limit) if old[i] != new[i]), limit)


def _on_request(*, session_id="", api_request_id="", model="", request=None, system_prompt=None, **kwargs):
    if not isinstance(request, dict) or not api_request_id:
        return
    shape = _shape(request)
    # Hook payload strings are cut at 8000 chars; the full system prompt comes as its own argument.
    full_prompt = system_prompt if isinstance(system_prompt, str) else None
    with _lock:
        previous = _last.get(session_id)
        brk, context = _first_break(previous, shape), None
        old_prompt = previous.get("system_prompt") if previous else None
        if full_prompt is not None and old_prompt is not None and old_prompt != full_prompt:
            at = _first_diff(old_prompt, full_prompt)
            window = full_prompt[max(0, at - 40):at + 40]
            # The prompt carries bank context and user facts: log a hash unless debugging is opted in.
            brk = f"system_prompt@{at}"
            context = window if os.environ.get("CACHE_MONITOR_DEBUG") == "1" else _digest(window)
        shape["system_prompt"] = full_prompt
        _last[session_id] = shape
        _pending[api_request_id] = {"session": session_id, "request": api_request_id, "model": model,
                                    "break": brk, "break_context": context}


def _on_response(*, api_request_id="", usage=None, **kwargs):
    with _lock:
        row = _pending.pop(api_request_id, None)
    if row is None or not isinstance(usage, dict):
        return
    prompt = int(usage.get("prompt_tokens") or 0)
    cached = int(usage.get("cache_read_tokens") or 0)
    row.update(ts=round(time.time(), 3), prompt_tokens=prompt, cached_tokens=cached,
               cache_share=round(cached / prompt, 3) if prompt else 0.0)
    path = _log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass  # observability only: never disturb a turn


def register(ctx):
    ctx.register_hook("pre_api_request", _on_request)
    ctx.register_hook("post_api_request", _on_response)
