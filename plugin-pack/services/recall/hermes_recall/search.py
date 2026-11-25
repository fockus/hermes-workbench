"""Hybrid search of hermes-recall (spec Task 5; REQ-009, REQ-010, REQ-011, REQ-012; design C3).

    search(indexes, *, profile, scope, query, k=8, all_scopes=False, embedder=None, reranker=None) -> Result

``indexes(name) -> Index`` is passed by the service (the profile's index and ``shared-wiki``); search
opens nothing itself. ``scope`` is the request's ``{kind, project_id}``: ``folder``/``project`` with a
project id search ``project:<id>``, anything else ``general``; ``wiki:shared`` is always added
(REQ-009). ``all_scopes=True`` searches every scope of the profile's index plus the shared wiki (REQ-010).

BM25 (FTS5) and dense cosine lists, each limited to the scope set, are fused by RRF (k=60); a hit's
``channels`` names every list that found it. Each channel is ranked once across both indexes (BM25 by
FTS5's ``bm25()`` with N, document frequencies and average length shared by both indexes, dense by
cosine), so the shared wiki's best chunk does not tie with the profile's best. Dense is skipped and ``degraded=True`` when there is no
embedder, an index's ``model_id()`` differs from the embedder's, or an index with chunks has no vectors;
``degraded=True`` also when hits were found and none of them came from dense (the scope is not embedded yet).
Wiki hits are expanded by one hop of wiki links (``search_graph``). ``reranker(query, fragments)``
reorders the top 20 fused hits by its scores (REQ-012); scores stay the RRF scores.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Sequence

import numpy as np

from hermes_recall.search_graph import Candidate, expand

RRF_K = 60
MAX_K = 20
RERANK_TOP = 20
CANDIDATES = 50  # length of each channel's list before fusion
FRAGMENT = 400
SHARED_WIKI = "shared-wiki"
SHARED_SCOPE = "wiki:shared"
_WORD = re.compile(r"\w+")
_TERM = re.compile(r"[^\W_]+")  # unicode61 tokens: letters and digits; "_" separates
K1, B = 1.2, 0.75  # FTS5 bm25() defaults

Reranker = Callable[[str, list[str]], Sequence[float]]


@dataclass(frozen=True)
class Hit:
    source: str  # "session", "session_summary", "compaction", "memory_bank", "wiki", ...
    ref: str
    lines: tuple[int, int] | None
    scope: str
    fragment: str
    score: float
    channels: tuple[str, ...]  # ("bm25", "dense", "graph")


@dataclass(frozen=True)
class Result:
    hits: list[Hit]
    model: str | None
    degraded: bool


def scope_set(scope: dict, all_scopes: bool) -> set[str] | None:
    """The scopes a search may return; ``None`` means every scope of the profile's index."""
    if all_scopes:
        return None
    project = scope.get("project_id") if scope.get("kind") in ("folder", "project") else None
    return {"project:%s" % project if project else "general", SHARED_SCOPE}


def search(indexes: Callable, *, profile: str, scope: dict, query: str, k: int = 8, all_scopes: bool = False,
           embedder=None, reranker: Reranker | None = None) -> Result:
    k = max(0, min(int(k), MAX_K))
    model = getattr(embedder, "model_id", None)
    pools = [(name, indexes(name)) for name in dict.fromkeys((profile, SHARED_WIKI))]
    ready = {name: _dense_ready(idx, model) for name, idx in pools}
    degraded = model is None or False in ready.values()
    if k == 0:
        return Result([], model, degraded)
    allowed = scope_set(scope, all_scopes)
    qvec = None
    if model is not None and True in ready.values():
        qvec = np.asarray(embedder.embed_query(query), dtype=np.float32)
    lists: dict[str, list[tuple[float, str, int]]] = {"bm25": bm25_shared(pools, query, allowed), "dense": []}
    rows: dict[str, dict] = {}
    for name, idx in pools:
        ids = {cid for _, owner, cid in lists["bm25"] if owner == name}
        if qvec is not None and ready[name]:
            dense = _dense(idx, model, qvec, allowed)
            lists["dense"] += [(key, name, cid) for cid, key in dense]
            ids.update(cid for cid, _ in dense)
        rows[name] = idx.chunks(ids)
    fused: dict[tuple[str, int], Candidate] = {}
    for channel, entries in lists.items():  # one RRF rank per channel across both indexes (lower key is better)
        for rank, (_, name, cid) in enumerate(sorted(entries, key=lambda e: e[0])[:CANDIDATES], start=1):
            # the indexer may have replaced or rescoped the chunk since the channel query
            if cid not in rows[name] or (allowed is not None and rows[name][cid].scope not in allowed):
                continue
            cand = fused.setdefault((name, cid), Candidate(name, rows[name][cid]))
            cand.score += 1.0 / (RRF_K + rank)
            cand.channels.append(channel)
    if not any("dense" in c.channels for c in fused.values()):
        degraded = True  # only BM25 answered for this scope set
    ranked = sorted(fused.values(), key=lambda c: -c.score)
    if reranker is not None and ranked:
        top = ranked[:RERANK_TOP]
        scores = list(reranker(query, [c.row.text[:FRAGMENT] for c in top]))
        order = sorted(range(len(top)), key=lambda i: -scores[i])
        ranked = [top[i] for i in order] + ranked[RERANK_TOP:]
    ranked = ranked[:k]
    ranked += expand(dict(pools), ranked, allowed)
    return Result([_hit(c) for c in ranked], model, degraded)


def _dense_ready(idx, model: str | None) -> bool | None:
    """Whether dense can answer for ``idx`` (same model, vectors present); None for an index without chunks."""
    if not idx.rows("SELECT 1 FROM chunks LIMIT 1"):
        return None
    if model is None:
        return False
    return idx.model_id() == model and bool(idx.rows("SELECT 1 FROM vectors WHERE model_id = ? LIMIT 1", (model,)))


def _scope_sql(allowed: set[str] | None) -> tuple[str, list[str]]:
    if allowed is None:
        return "", []
    return " AND s.scope IN (%s)" % ",".join("?" * len(allowed)), sorted(allowed)


def _match(query: str) -> str:
    """The FTS5 expression of ``query``: each ``\\w+`` word a quoted phrase (``foo_bar`` is the phrase ``foo bar``), ORed."""
    return " OR ".join('"%s"' % w for w in _WORD.findall(query))


def _bm25(idx, query: str, allowed: set[str] | None) -> list[tuple[int, float]]:
    """``(chunk id, FTS5 rank)`` in BM25 order (lower rank is better), scope-filtered inside the query (``Index.fts`` has no scope filter, and
    cutting its list before filtering would let other scopes crowd the current one out)."""
    if not _WORD.search(query):
        return []
    where, args = _scope_sql(allowed)
    return [(cid, rank) for cid, rank in idx.rows(
        "SELECT f.rowid, f.rank FROM chunks_fts f JOIN chunks c ON c.id = f.rowid JOIN sources s ON s.id = c.source_id "
        "WHERE chunks_fts MATCH ?" + where + " ORDER BY f.rank LIMIT ?", [_match(query), *args, CANDIDATES])]


def _fold(term: str) -> str:
    """unicode61 ``remove_diacritics=1``: Latin letters lose their diacritics, other scripts keep them (ё, й)."""
    if term.isascii():
        return term
    return "".join(unicodedata.normalize("NFD", c)[0] if "\u00c0" <= c <= "\u024f" or "\u1e00" <= c <= "\u1eff"
                   else c for c in term)


def terms(text: str) -> list[str]:
    """Tokens of ``text`` as FTS5's unicode61 tokenizer makes them."""
    return [_fold(t) for t in _TERM.findall(text.lower())]


def _varint(blob: bytes, pos: int) -> tuple[int, int]:
    """One SQLite varint (FTS5's on-disk integers) at ``pos``: ``(value, next position)``."""
    value = 0
    for i in range(8):
        byte = blob[pos + i]
        value = (value << 7) | (byte & 0x7F)
        if byte < 0x80:
            return value, pos + i + 1
    return (value << 8) | blob[pos + 8], pos + 9


def _totals(idx, phrases: list[tuple[str, ...]]) -> tuple[int, int, list[int]]:
    """``(rows, tokens, document frequency per phrase)`` of ``chunks_fts``; rows and tokens from FTS5's averages
    record (``chunks_fts_data`` id 1). One statement, one snapshot: an indexer commit between separate reads
    could give a df above N, and the idf's log would raise."""
    counts = "".join(", (SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?)" for _ in phrases)
    block, *df = idx.rows("SELECT (SELECT block FROM chunks_fts_data WHERE id = 1)" + counts,
                          ['"%s"' % " ".join(p) for p in phrases])[0]
    if not block:
        return 0, 0, df
    rows, pos = _varint(block, 0)
    return rows, _varint(block, pos)[0], df


def _phrases(query: str) -> list[tuple[str, ...]]:
    """The phrases of ``_match(query)`` as unicode61 token tuples, in query order (FTS5 scores repeats again)."""
    return [p for p in (tuple(terms(w)) for w in _WORD.findall(query)) if p]


def _hits(tokens: list[str], phrase: tuple[str, ...]) -> int:
    """Occurrences of ``phrase`` in ``tokens`` (FTS5 phrase instances, overlaps included)."""
    n = len(phrase)
    return sum(1 for i in range(len(tokens) - n + 1) if tuple(tokens[i:i + n]) == phrase)


def bm25_shared(pools, query: str, allowed: set[str] | None) -> list[tuple[float, str, int]]:
    """The best ``CANDIDATES`` ``(key, index name, chunk id)``, best first: FTS5's ``bm25()`` as one table over
    every pool would give it (N, document frequency per phrase and average length summed over the pools).
    Every scope-filtered match of every pool is scored before the cut: an index's own top list is ordered by
    its own statistics and can miss the shared best. Ranks of separate indexes are not comparable: a sparse
    wiki's own document frequency would let a weak wiki chunk outrank a strong profile chunk (eval_merge)."""
    phrases = _phrases(query)
    if not phrases:
        return []
    where, args = _scope_sql(allowed)
    # ponytail: tokenises every matching chunk per search, linear in the matches; past ~50k matches per query,
    # keep a persistent fts5vocab(instance) table in the store and take term frequencies from SQL.
    found = [(name, idx.rows(
        "SELECT f.rowid, f.body, d.sz FROM chunks_fts f JOIN chunks_fts_docsize d ON d.id = f.rowid "
        "JOIN chunks c ON c.id = f.rowid JOIN sources s ON s.id = c.source_id WHERE chunks_fts MATCH ?" + where,
        [_match(query), *args])) for name, idx in pools]
    if not any(rows for _, rows in found):
        return []
    n = tokens = 0
    df: Counter = Counter()
    unique = sorted(set(phrases))  # fts5vocab needs a TEMP table, which the query_only reader refuses
    for _, idx in pools:
        rows, size, counts = _totals(idx, unique)
        n, tokens = n + rows, tokens + size
        df.update(dict(zip(unique, counts)))
    avgdl = tokens / n if n else 1.0
    idf = {p: max(math.log((n - df[p] + 0.5) / (df[p] + 0.5)), 1e-6) for p in df}  # FTS5 floors idf at 1e-6
    out = []
    for name, rows in found:
        for cid, body, size in rows:
            body_terms = terms(body)
            tf = {p: _hits(body_terms, p) for p in set(phrases)}
            norm = K1 * (1 - B + B * _varint(size, 0)[0] / avgdl)
            out.append((-sum(idf[p] * tf[p] * (K1 + 1) / (tf[p] + norm) for p in phrases if tf[p]), name, cid))
    return sorted(out, key=lambda e: e[0])[:CANDIDATES]


def _dense(idx, model: str, qvec: np.ndarray, allowed: set[str] | None) -> list[tuple[int, float]]:
    """``(chunk id, -cosine)``, best first."""
    # ponytail: brute-force cosine over all chunks of the scope; switch to an ANN index (hnswlib or sqlite-vec) when a scope passes ~200k chunks.
    ids, mat = idx.vectors(model)
    if not ids or mat.shape[1] != qvec.shape[0]:
        return []
    ids = np.asarray(ids)
    if allowed is not None:
        where, args = _scope_sql(allowed)
        inside = {cid for (cid,) in idx.rows(
            "SELECT c.id FROM chunks c JOIN sources s ON s.id = c.source_id WHERE 1 = 1" + where, args)}
        keep = np.fromiter((cid in inside for cid in ids), dtype=bool, count=len(ids))
        ids, mat = ids[keep], mat[keep]
    if not len(ids):
        return []
    norms = np.linalg.norm(mat, axis=1) * (np.linalg.norm(qvec) or 1.0)
    cosine = (mat @ qvec) / np.where(norms == 0, 1.0, norms)
    return [(int(ids[i]), -float(cosine[i])) for i in np.argsort(-cosine, kind="stable")[:CANDIDATES]]


def _hit(cand: Candidate) -> Hit:
    row = cand.row
    lines = tuple(int(x) for x in row.lines.split("-")) if row.lines else None
    return Hit(source=row.kind, ref=row.ref, lines=lines, scope=row.scope, fragment=row.text[:FRAGMENT],
               score=cand.score, channels=tuple(cand.channels))
