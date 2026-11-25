"""Evaluation harness of hermes-recall (spec Task 10; REQ-017, REQ-016): the embedding-model bake-off.

    python -m hermes_recall.eval --questions <jsonl> [--models frida,pplx-v1,pplx-context] [--profile default]
    python -m hermes_recall.eval --calibrate <pairs.jsonl> --model <name>   (eval_calibrate, REQ-007)
    python -m hermes_recall.eval --merge <fixture folder>                   (eval_merge, BM25 merge)

Corpus: the profile's documents (``sources.documents``) and sessions (``sources.sessions`` through
``message_chunks``), chunked into a fresh index in a temporary folder; the live
``$HERMES_HOME/recall/*.sqlite`` is never opened. Per model: ``embedders.load(name)``, the chunks embedded
in ``BATCH`` batches, then per question ``{"question", "source"}`` the top ``TOP`` chunks by dense
cosine; a hit when a top chunk's ref ends with ``source``. Latency is ``embed_query`` plus scoring.
Dense only on purpose: BM25 ranks the same for every model, so it would only blur the comparison.

Prints one line per model, ``<name> top5=<rate> p95_ms=<x> peak_mb=<y>`` (``<name> unavailable: <reason>``
for a model that cannot be loaded or run; the others still run), then merges the measured models into
``$HERMES_HOME/recall/eval.json`` = ``{<model name>: {top5, p95_ms, peak_mb}}`` (atomic; read by the
service's ``/v1/status`` as ``eval``). A question line that is not ``{question, source}`` (both non-blank)
exits 2; a corpus that cannot be read completely exits 1 before anything is measured or written.
"""

from __future__ import annotations

import argparse
import json
import math
import resource
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Sequence

import numpy as np

from hermes_recall import embedders, sources
from hermes_recall.chunker import chunk_document
from hermes_recall.eval_calibrate import calibrate, hermes_home, read_jsonl, write_json
from hermes_recall.indexer import BATCH
from hermes_recall.indexer_reconcile import day, message_chunks
from hermes_recall.store import Index, Source, open_index

TOP = 5
_RSS_UNIT = 1024 * 1024 if sys.platform == "darwin" else 1024  # ru_maxrss: bytes on macOS, KiB on Linux


class CorpusError(Exception):
    """The corpus cannot be read completely; a score over part of it would be published as the real one."""


def _is_question(row: object) -> bool:
    # a blank source would match every ref (any string ends with "")
    return isinstance(row, dict) and all(isinstance(row.get(k), str) and row[k].strip() for k in ("question", "source"))


def build_corpus(home: Path, profile: str, idx: Index) -> None:
    """Chunk the profile's documents and sessions into ``idx`` as the indexer would (no vectors)."""
    docs = sources.documents(home, profile)
    if docs is None:
        raise CorpusError("projects.db of profile %r cannot be read" % profile)
    for doc in docs:
        try:
            raw, mtime = doc.path.read_bytes(), doc.path.stat().st_mtime
        except OSError as exc:
            raise CorpusError("cannot read %s: %s" % (doc.path, exc)) from exc
        result = chunk_document(raw, source=doc.kind, ref=doc.ref, date=day(mtime), scope=doc.scope)
        idx.put_source(Source(doc.kind, doc.ref, doc.scope, doc.project_id, "eval", "", mtime), result.chunks).result()
    profile_home = sources.profiles(home).get(profile)
    for session in sources.sessions(profile_home) if profile_home else []:
        scope = sources.scope_label(session.project_id)
        for kind, items in (("session", [(text, ts) for _, text, ts in session.messages]),
                            ("compaction", session.compactions)):
            if items:
                source = Source(kind, session.id, scope, session.project_id, "eval", "", session.updated_at)
                idx.put_source(source, message_chunks(kind, session.id, scope, items)).result()


def embed_corpus(idx: Index, embedder) -> None:
    model = embedder.model_id
    while batch := idx.missing_vectors(model, BATCH):
        vectors = embedder.embed_documents([text for _, text in batch])
        if len(vectors) != len(batch):
            raise ValueError("%s returned %d vectors for %d texts" % (model, len(vectors), len(batch)))
        idx.set_vectors(model, zip([cid for cid, _ in batch], vectors)).result()


def p95(samples: Sequence[float]) -> float:
    """The nearest-rank 95th percentile: an observed sample, so a fixed set of latencies gives it exactly."""
    if not samples:
        return 0.0
    return sorted(samples)[math.ceil(0.95 * len(samples)) - 1]


def evaluate(idx: Index, embedder, questions: list[dict], clock: Callable[[], float]) -> tuple[float, list[float]]:
    """``(top-5 hit rate, latencies in ms)`` of dense cosine over ``idx``'s vectors of ``embedder``."""
    ids, mat = idx.vectors(embedder.model_id)
    refs = dict(idx.rows("SELECT c.id, s.ref FROM chunks c JOIN sources s ON s.id = c.source_id"))
    norms = np.linalg.norm(mat, axis=1, keepdims=True) if ids else np.ones((0, 1))
    unit = mat / np.where(norms == 0, 1.0, norms)
    found, latency = 0, []
    for q in questions:
        start = clock()
        qvec = np.asarray(embedder.embed_query(q["question"]), dtype=np.float32)
        top = np.argsort(-(unit @ qvec), kind="stable")[:TOP] if ids else []
        latency.append((clock() - start) * 1000.0)
        found += any(refs.get(ids[i], "").endswith(q["source"]) for i in top)
    return found / max(len(questions), 1), latency


def bake_off(home: Path, profile: str, questions: list[dict], names: list[str], loader: Callable,
             clock: Callable[[], float]) -> dict[str, dict]:
    measured: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="hermes-recall-eval-") as tmp:
        idx = open_index(Path(tmp), "eval")
        try:
            build_corpus(home, profile, idx)
            for name in names:
                try:
                    embedder = loader(name)
                    try:
                        embed_corpus(idx, embedder)
                        rate, latency = evaluate(idx, embedder, questions, clock)
                    finally:
                        embedder.unload()
                except Exception as exc:  # a broken or missing model is reported; the others still run
                    print("%s unavailable: %s" % (name, exc))
                    continue
                # ponytail: ru_maxrss is the process high-water mark, cumulative over the models of one run;
                # run one --models name per invocation for an exact per-model peak.
                peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / _RSS_UNIT
                measured[name] = {"top5": rate, "p95_ms": p95(latency), "peak_mb": round(peak)}
                print("%s top5=%.2f p95_ms=%.1f peak_mb=%d" % (name, rate, measured[name]["p95_ms"], round(peak)))
        finally:
            idx.close()
    return measured


def _save(measured: dict[str, dict]) -> None:
    path = hermes_home() / "recall" / "eval.json"
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    write_json(path, {**(saved if isinstance(saved, dict) else {}), **measured})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hermes_recall.eval", description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--questions", type=Path, help="JSONL of {question, source}")
    mode.add_argument("--calibrate", type=Path, metavar="PAIRS", help="JSONL of {history, message, shift}")
    mode.add_argument("--merge", type=Path, metavar="FOLDER", help="BM25 merge fixtures (*.jsonl)")
    parser.add_argument("--models", default=",".join(embedders.MODELS))
    parser.add_argument("--model", help="the model of --calibrate")
    parser.add_argument("--profile", default="default")
    return parser


def main(argv: list[str] | None = None, *, loader: Callable = embedders.load,
         clock: Callable[[], float] = time.perf_counter) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.calibrate is not None:
        if not args.model:
            parser.error("--calibrate needs --model")
        return calibrate(args.calibrate, args.model, loader)
    if args.merge is not None:
        from hermes_recall.eval_merge import report

        return report(args.merge)
    questions = read_jsonl(args.questions, _is_question, '{"question": str, "source": str}')
    names = [n.strip() for n in args.models.split(",") if n.strip()]
    try:
        measured = bake_off(hermes_home(), args.profile, questions, names, loader, clock)
    except CorpusError as exc:
        print("eval: %s" % exc, file=sys.stderr)
        return 1
    if measured:
        _save(measured)
    return 0


if __name__ == "__main__":
    sys.exit(main())
