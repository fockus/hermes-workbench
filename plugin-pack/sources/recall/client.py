"""HTTP client of the local hermes-recall service (spec Task 7): the standard library only, one explicit
timeout per call, no retries. Any connection error, timeout, non-2xx answer or body that is not JSON raises
``Unavailable``; its reason carries the service's ``error`` text when there is one."""

from __future__ import annotations

import http.client
import ipaddress
import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote, urlsplit

_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # turn bodies never go through a proxy


class Unavailable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Client:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url, self.timeout = base_url.rstrip("/"), timeout

    def search(self, profile: str, scope: dict, query: str, k: int, all_scopes: bool,
               timeout: float | None = None) -> dict:
        body = {"profile": profile, "scope": scope, "query": query, "k": k, "all_scopes": all_scopes}
        return self._dict(self._call("POST", "/v1/search", body, timeout))

    def index_turn(self, profile: str, session_id: str, project_id: str | None, messages: list) -> None:
        body = {"profile": profile, "session_id": session_id, "project_id": project_id, "messages": messages}
        self._call("POST", "/v1/index/turn", body)

    def embed(self, profile: str, query: str, timeout: float | None = None) -> list[float] | None:
        """The service's embedding of ``query`` (``embed_only``, k=0); None when it has none."""
        body = {"profile": profile, "query": query, "k": 0, "embed_only": True}
        vector = self._dict(self._call("POST", "/v1/search", body, timeout)).get("embedding")
        numbers = isinstance(vector, list) and vector and all(isinstance(x, (int, float)) for x in vector)
        return [float(x) for x in vector] if numbers else None

    def index_summary(self, kind: str, profile: str, session_id: str, summary: str) -> None:
        """``kind``: ``session-end`` (the model's summary of an ended session) or ``compaction`` (Hermes's own)."""
        self._call("POST", "/v1/index/" + kind, {"profile": profile, "session_id": session_id, "summary": summary})

    def delete_session(self, profile: str, session_id: str, timeout: float | None = None) -> int:
        path = "/v1/sessions/%s?profile=%s" % (quote(session_id, safe=""), quote(profile, safe=""))
        removed = self._dict(self._call("DELETE", path, None, timeout)).get("removed")
        return removed if isinstance(removed, int) else 0

    def status(self) -> dict:
        return self._dict(self._call("GET", "/v1/status"))

    @staticmethod
    def _dict(answer: Any) -> dict:
        if not isinstance(answer, dict):
            raise Unavailable("the service answered a JSON value that is not an object")
        return answer

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> Any:
        _require_loopback(self.base_url)
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base_url + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with _DIRECT.open(request, timeout=self.timeout if timeout is None else timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise Unavailable(_error_text(exc)) from exc
        except (OSError, http.client.HTTPException) as exc:  # URLError, timeouts and resets are OSErrors
            raise Unavailable("%s: %s" % (type(exc).__name__, exc)) from exc
        if not raw:
            raise Unavailable("the service answered an empty body")
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise Unavailable("the service answered a body that is not JSON") from exc


def _require_loopback(url: str) -> None:
    """The service is local and its requests carry turn text: only an http(s) URL of a loopback host is used."""
    reason = Unavailable("service_url must be an http(s) URL of a loopback host (127.0.0.1, ::1, localhost)")
    try:
        parts = urlsplit(url)
        host, _ = parts.hostname, parts.port
        loopback = host == "localhost" or (host is not None and ipaddress.ip_address(host).is_loopback)
    except ValueError:
        raise reason from None
    if parts.scheme not in ("http", "https") or not loopback:
        raise reason


def _error_text(exc: urllib.error.HTTPError) -> str:
    try:
        error = json.loads(exc.read()).get("error")
    except (OSError, http.client.HTTPException, ValueError, AttributeError):
        error = None
    return "HTTP %d: %s" % (exc.code, error) if error else "HTTP %d" % exc.code
