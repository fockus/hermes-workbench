"""BM25 cross-index merge strategies of hermes-recall, measured on fixtures (spec Task 10, stage 2).

    python -m hermes_recall.eval --merge recall/eval/merge

Every strategy is ``strategy(pools, query, allowed) -> [(key, index name, chunk id)]``, best first (lower
key is better), the list ``search`` fuses: ``pools`` is ``[(index name, Index)]``, ``allowed`` the scope set.

* ``local_rank``: the raw FTS5 rank of each index, sorted together (the behaviour before this measurement);
* ``shared_df``: BM25 with collection statistics shared by both indexes; the winner, so it lives in
  product code (``search.bm25_shared``) and is imported here;
* ``per_index_norm``: each index's ranks scaled to [0, 1] by its own best and worst, then merged.

A fixture ``*.jsonl`` holds ``{"index": "default"|"shared-wiki", "ref", "text"}`` lines (one chunk each, in
real store files under a temporary folder) and ``{"query", "answer": <ref>}`` lines. The metrics are top-1
and MRR of the labelled chunk; the winner is the best MRR (then top-1, then table order).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from hermes_recall.chunker import chunk_document
from hermes_recall.search import _bm25, bm25_shared
from hermes_recall.store import Index, Source, open_index

SHARED = "shared-wiki"


def local_rank(pools, query: str, allowed: set[str] | None) -> list[tuple[float, str, int]]:
    out = [(rank, name, cid) for name, idx in pools for cid, rank in _bm25(idx, query, allowed)]
    return sorted(out, key=lambda e: e[0])


def per_index_norm(pools, query: str, allowed: set[str] | None) -> list[tuple[float, str, int]]:
    out = []
    for name, idx in pools:
        hits = _bm25(idx, query, allowed)
        if hits:
            best, worst = hits[0][1], hits[-1][1]
            span = (worst - best) or 1.0
            out += [((rank - best) / span, name, cid) for cid, rank in hits]
    return sorted(out, key=lambda e: e[0])


STRATEGIES = {"shared_df": bm25_shared, "local_rank": local_rank, "per_index_norm": per_index_norm}


def load(path: Path, home: Path) -> tuple[list[tuple[str, Index]], list[tuple[str, str]]]:
    """The fixture's two indexes under ``home`` (the caller closes them) and its ``(query, answer)`` pairs."""
    pools = {name: open_index(home, name) for name in ("default", SHARED)}
    queries = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        row = json.loads(line) if line.strip() else None
        if row is None:
            continue
        if "query" in row:
            queries.append((row["query"], row["answer"]))
            continue
        wiki = row["index"] == SHARED
        kind, scope = ("wiki", "wiki:shared") if wiki else ("memory_bank", "general")
        chunks = chunk_document(row["text"], source=kind, ref=row["ref"], date="2026-01-01", scope=scope).chunks
        pools[row["index"]].put_source(Source(kind, row["ref"], scope, None, "eval", row["ref"], 0.0), chunks).result()
    return list(pools.items()), queries


def measure(folder: Path) -> dict[str, tuple[float, float, int]]:
    """``{strategy: (top-1, MRR, queries)}`` over every fixture of ``folder``."""
    hits: dict[str, list[float]] = {name: [] for name in STRATEGIES}
    with tempfile.TemporaryDirectory(prefix="hermes-recall-merge-") as tmp:
        for n, path in enumerate(sorted(Path(folder).glob("*.jsonl"))):
            pools, queries = load(path, Path(tmp) / str(n))
            try:
                for query, answer in queries:
                    for name, strategy in STRATEGIES.items():
                        ranked = strategy(pools, query, None)
                        rows = {p: idx.chunks([cid for _, q, cid in ranked if q == p]) for p, idx in pools}
                        refs = [rows[p][cid].ref for _, p, cid in ranked if cid in rows[p]]
                        hits[name].append(1.0 / (refs.index(answer) + 1) if answer in refs else 0.0)
            finally:
                for _, idx in pools:
                    idx.close()
    return {name: (sum(r == 1.0 for r in rr) / max(len(rr), 1), sum(rr) / max(len(rr), 1), len(rr))
            for name, rr in hits.items()}


def report(folder: Path) -> int:
    table = measure(folder)
    if not any(count for _, _, count in table.values()):  # a mistyped folder must not crown a winner of zeros
        print("%s: no labelled queries in *.jsonl" % folder, file=sys.stderr)
        return 2
    for name, (top1, mrr, count) in table.items():
        print("%s top1=%.3f mrr=%.3f queries=%d" % (name, top1, mrr, count))
    winner = max(table, key=lambda name: (table[name][1], table[name][0]))  # max keeps the first on a tie
    print("winner=%s (by MRR)" % winner)
    return 0
