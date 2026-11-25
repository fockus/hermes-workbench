"""HTTP service of hermes-recall (spec Task 6; REQ-018, REQ-019, REQ-026, REQ-029; design § Contract).

    python -m hermes_recall.server [--port 8765] [--model frida] [--check]

A stdlib ``ThreadingHTTPServer`` on 127.0.0.1 (default port 8765). ``create_server(home, *, port=0,
embedder=None, reranker=None, missing=())`` returns it; the caller runs ``serve_forever`` and
``server_close()`` stops the background threads (server_engine). Every answer is JSON; ``error`` is a text.

* ``POST /v1/search`` ``{profile, scope:{kind, project_id}, query, k (0..20, default 8), all_scopes}`` ->
  ``200 {hits:[{source, ref, lines:[a, b]|null, scope, fragment, score, channels:[...]}], model, degraded}``;
  ``{profile, query, k:0, embed_only:true}`` -> ``{hits:[], embedding:[...]|null, model, degraded}``.
* ``POST /v1/index/turn`` ``{profile, session_id, project_id, messages:[{role, text, ts}]}`` -> ``202 {queued:n}``
  (n: user/assistant messages with text); ``POST /v1/index/session-end`` and ``/v1/index/compaction``
  ``{profile, session_id, summary}`` -> ``202 {queued:1}``. Writes run on the engine's job thread.
* ``DELETE /v1/sessions/{id}?profile=`` -> ``200 {removed:n}``.
* ``GET /v1/status`` -> ``{model, loaded, missing, indexes:[{profile, chunks, sources, last_indexed,
  last_error, outdated}], rebuild:{state, index, progress}, eval:{<model>: {top5, p95_ms, peak_mb}}, last_error}``.
* ``503 {error:"recall_unavailable"}`` until the start-up catch-up is done.
* ``409 {error:"index_outdated", profile, rebuild}`` when an index the request reads or writes (a profile's
  or ``shared-wiki``) carries a format/redaction mark this code does not accept (index_version): nothing is
  read or written until the operator runs the ``rebuild`` command line. Status lists it with ``outdated: true``.

Refused at the boundary: an ``Origin`` header or a ``Host`` other than ``127.0.0.1``/``localhost`` with
this port (403, DNS rebinding), a POST that is not ``application/json`` (415), a body over 1 MiB (413), and
with 400: a body that is not a JSON object, a ``profile`` that is not an existing profile name (never
``shared-wiki``), ``k`` outside 0..20, a non-boolean ``all_scopes``, a non-string ``query`` or ``summary``, a ``session_id`` that is
empty or over 200 characters, ``messages`` that are not ``{role, text, ts}`` objects, ``embed_only`` with k != 0.
Without onnxruntime, tokenizers or the model files the service runs BM25 only and status ``missing`` says
why; without numpy it does not start (exit 1).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import re
import signal
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence
from urllib.parse import parse_qs, unquote, urlsplit

from hermes_recall import sources
from hermes_recall.index_version import IndexOutdated

if TYPE_CHECKING:  # imported when a server is created: ``--check`` must run without numpy
    from hermes_recall.server_engine import Engine

log = logging.getLogger(__name__)
PORT = 8765
PACKAGES = ("numpy", "onnxruntime", "tokenizers")
MAX_BODY = 1 << 20
DRAIN_LIMIT = 8 << 20  # a larger refused body is not read: the connection is just closed
PROFILE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
SUMMARIES = {"/v1/index/session-end": "session_summary", "/v1/index/compaction": "compaction"}


class Refused(Exception):
    def __init__(self, code: int, error: str):
        super().__init__(error)
        self.code, self.error = code, error


def _need(condition: Any, error: str, code: int = 400) -> None:
    if not condition:
        raise Refused(code, error)


def _hit(hit: Any) -> dict:
    return {"source": hit.source, "ref": hit.ref, "lines": list(hit.lines) if hit.lines else None,
            "scope": hit.scope, "fragment": hit.fragment, "score": hit.score, "channels": list(hit.channels)}


class _Handler(BaseHTTPRequestHandler):
    server: "RecallServer"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("recall: %s " + fmt, self.address_string(), *args)

    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _guard(self) -> None:
        _need("Origin" not in self.headers, "cross-origin requests are refused", 403)
        port = self.server.server_address[1]
        allowed = {"%s%s" % (h, p) for h in ("127.0.0.1", "localhost") for p in ("", ":%d" % port)}
        _need(self.headers.get("Host") in allowed, "the Host header must name this loopback service", 403)

    def _body(self) -> dict:
        kind = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        _need(kind == "application/json", "Content-Type must be application/json", 415)
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            raise Refused(400, "Content-Length is required") from None
        _need(length >= 0, "Content-Length is required")
        if length > MAX_BODY:
            # Read (up to a bound) what the client is still sending, so it receives the 413 instead of a reset.
            left = min(length, DRAIN_LIMIT)
            while left > 0 and (chunk := self.rfile.read(min(left, 1 << 16))):
                left -= len(chunk)
            raise Refused(413, "the body is larger than 1 MiB")
        try:
            data = json.loads(self.rfile.read(length) or b"null")
        except ValueError:
            raise Refused(400, "the body is not JSON") from None
        _need(isinstance(data, dict), "the body must be a JSON object")
        return data

    def _profile(self, value: Any) -> str:
        _need(isinstance(value, str) and PROFILE.fullmatch(value) and value != sources.SHARED_INDEX
              and value in sources.profiles(self.server.engine.home), "profile must name an existing profile")
        return value

    @staticmethod
    def _session(value: Any) -> str:
        _need(isinstance(value, str) and 0 < len(value) <= 200, "session_id must be 1..200 characters")
        return value

    def _route(self, method: str) -> None:
        try:
            self._guard()
            engine = self.server.engine
            if not engine.ready.is_set():
                return self._send(503, {"error": "recall_unavailable"})
            parts = urlsplit(self.path)
            if method == "GET" and parts.path == "/v1/status":
                return self._send(200, engine.status())
            if method == "DELETE" and parts.path.startswith("/v1/sessions/"):
                profile = self._profile((parse_qs(parts.query).get("profile") or [None])[0])
                session_id = self._session(unquote(parts.path[len("/v1/sessions/"):]))
                return self._send(200, {"removed": engine.delete_session(profile, session_id)})
            if method == "POST" and parts.path == "/v1/search":
                return self._send(200, self._search(engine, self._body()))
            if method == "POST" and (parts.path == "/v1/index/turn" or parts.path in SUMMARIES):
                return self._send(202, self._index(engine, parts.path, self._body()))
            return self._send(404, {"error": "no such endpoint: %s %s" % (method, parts.path)})
        except Refused as refused:
            return self._send(refused.code, {"error": refused.error})
        except IndexOutdated as outdated:  # operator action: ``rebuild`` is a command line, not a retry
            return self._send(409, {"error": "index_outdated", "profile": outdated.profile,
                                    "rebuild": outdated.command})
        except Exception:  # noqa: BLE001 - logged here; the client gets no internals
            log.exception("recall: %s %s failed", method, self.path)
            return self._send(500, {"error": "internal error"})

    def _search(self, engine: "Engine", body: dict) -> dict:
        from hermes_recall.search import MAX_K

        profile = self._profile(body.get("profile"))
        k = body.get("k", 8)
        _need(isinstance(k, int) and not isinstance(k, bool) and 0 <= k <= MAX_K, "k must be an integer 0..%d" % MAX_K)
        query = body.get("query", "")
        _need(isinstance(query, str), "query must be a string")
        scope = body.get("scope") or {}
        _need(isinstance(scope, dict), "scope must be an object")
        if body.get("embed_only"):
            _need(k == 0, "embed_only needs k=0")
            embedding = engine.embed_query(query)
            return {"hits": [], "embedding": embedding, "model": engine.status()["model"], "degraded": embedding is None}
        all_scopes = body.get("all_scopes", False)
        _need(isinstance(all_scopes, bool), "all_scopes must be a boolean")
        result = engine.search(profile, scope, query, k, all_scopes)
        return {"hits": [_hit(h) for h in result.hits], "model": result.model, "degraded": result.degraded}

    def _index(self, engine: "Engine", path: str, body: dict) -> dict:
        profile, session_id = self._profile(body.get("profile")), self._session(body.get("session_id"))
        if path in SUMMARIES:
            summary = body.get("summary")
            _need(isinstance(summary, str), "summary must be a string")
            engine.index_summary(profile, session_id, summary, SUMMARIES[path])
            return {"queued": 1}
        project_id, messages = body.get("project_id"), body.get("messages")
        _need(project_id is None or isinstance(project_id, str), "project_id must be a string or null")
        _need(isinstance(messages, list) and all(
            isinstance(m, dict) and isinstance(m.get("role"), str) and isinstance(m.get("text"), str)
            and isinstance(m.get("ts"), (int, float)) and not isinstance(m.get("ts"), bool) for m in messages),
            "messages must be a list of {role, text, ts}")
        engine.index_turn(profile, session_id, project_id, messages)
        return {"queued": sum(1 for m in messages if m["role"] in ("user", "assistant") and m["text"].strip())}

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def do_DELETE(self) -> None:
        self._route("DELETE")


class RecallServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], start_engine: Callable[[], "Engine"]):
        self.engine: "Engine | None" = None
        super().__init__(address, _Handler)  # bind first: a busy port starts no catch-up or index
        try:
            self.engine = start_engine()
        except BaseException:
            super().server_close()
            raise

    def server_close(self) -> None:
        super().server_close()
        if self.engine is not None:
            self.engine.close()


def create_server(home: Path, *, port: int = 0, embedder: Any = None, reranker: Any = None,
                  missing: Sequence[str] = ()) -> RecallServer:
    from hermes_recall.server_engine import Engine

    return RecallServer(("127.0.0.1", port), lambda: Engine(Path(home), embedder, reranker, missing))


def missing_packages(packages: Sequence[str] = PACKAGES) -> list[str]:
    return [name for name in packages if importlib.util.find_spec(name) is None]


def open_embedder(name: str) -> tuple[Any, list[str]]:
    """The embedder ``name``, or None with the reasons it cannot run (missing packages or model files)."""
    missing = ["missing package: %s" % p for p in missing_packages()]
    if missing:
        return None, missing
    from hermes_recall import embedders

    try:
        return embedders.load(name), []
    except (OSError, ValueError) as exc:
        return None, [str(exc)]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hermes_recall.server")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--model", default="frida")
    parser.add_argument("--check", action="store_true", help="list missing runtime packages and exit")
    args = parser.parse_args(argv)
    if args.check:
        missing = missing_packages(PACKAGES)
        for name in missing:
            print("missing: %s" % name)
        if not missing:
            print("ok: %s" % ", ".join(PACKAGES))
        return 1 if missing else 0
    if missing_packages(("numpy",)):  # the store and search keep vectors in numpy, BM25-only mode included
        print("recall: numpy is required to serve; run --check", file=sys.stderr)
        return 1
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    embedder, missing = open_embedder(args.model)
    for reason in missing:
        log.warning("recall: BM25 only: %s", reason)
    server = create_server(home, port=args.port, embedder=embedder, missing=missing)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log.info("recall: serving on 127.0.0.1:%d", server.server_address[1])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
