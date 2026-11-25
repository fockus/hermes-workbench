"""One wiki-link hop for hermes-recall search (spec Task 5; REQ-011).

For each wiki hit, the pages its chunk links to - ``[[Page]]`` and relative Markdown links to ``.md`` -
are resolved against the wiki pages of the same index and the same scope (the source page's own wiki). A linked page already among the hits gets the
``graph`` channel; any other is added once (its first chunk), with channel ``graph`` and half the score
of the hit that led there. Pages outside the search's scope set are never added.
"""

from __future__ import annotations

import posixpath
import re
from urllib.parse import unquote
from dataclasses import dataclass, field

_LINK = re.compile(r"\[[^\]]*\]\(([^)\s#]+\.md)(?:#[^)\s]*)?\)|\[\[([^\]|#]+)")


@dataclass
class Candidate:
    """A hit while it is being ranked: the index it came from, its chunk row, RRF score and channels."""

    index: str
    row: object  # store.ChunkRow
    score: float = 0.0
    channels: list[str] = field(default_factory=list)


def links(text: str, ref: str) -> list[tuple[str, str]]:
    """``("path", resolved ref)`` for relative Markdown links, ``("name", "Page.md")`` for wiki links."""
    out = []
    for md, wiki in _LINK.findall(text):
        if md:
            if "://" not in md and not md.startswith("/"):
                out.append(("path", posixpath.normpath(posixpath.join(posixpath.dirname(ref), unquote(md)))))
        else:
            name = posixpath.basename(wiki.strip())
            out.append(("name", name if name.endswith(".md") else name + ".md"))
    return out


def _pages(idx) -> list[tuple[str, str, int]]:
    """``(ref, scope, first chunk id)`` of every wiki page of ``idx`` (wiki pages are stored from ordinal 0)."""
    return idx.rows(
        "SELECT s.ref, s.scope, c.id FROM chunks c JOIN sources s ON s.id = c.source_id WHERE s.kind = 'wiki' "
        "AND c.ordinal = 0 ORDER BY s.ref")


def _resolve(pages: list[tuple[str, str, int]], how: str, target: str, scope: str) -> tuple[str, int] | None:
    """``(ref, first chunk id)`` of the linked page inside ``scope`` (the source page's own wiki)."""
    for ref, page_scope, cid in pages:
        if page_scope == scope and (ref if how == "path" else posixpath.basename(ref)) == target:
            return ref, cid
    return None


def expand(indexes: dict, hits: list[Candidate], allowed: set[str] | None) -> list[Candidate]:
    """The candidates one hop adds after ``hits``; marks linked pages already in ``hits`` with ``graph``."""
    known: dict[tuple[str, str], Candidate] = {}  # (index, ref) -> first candidate of that page
    for c in hits:
        known.setdefault((c.index, c.row.ref), c)
    pages: dict[str, list] = {}
    extra: list[Candidate] = []
    for hit in [c for c in hits if c.row.kind == "wiki"]:
        for how, target in links(hit.row.text, hit.row.ref):
            if hit.index not in pages:
                pages[hit.index] = _pages(indexes[hit.index])
            page = _resolve(pages[hit.index], how, target, hit.row.scope)
            if page is None or page[0] == hit.row.ref:
                continue
            ref, cid = page
            found = known.get((hit.index, ref))
            if found is not None:
                if "graph" not in found.channels:
                    found.channels.append("graph")
                continue
            row = indexes[hit.index].chunks([cid]).get(cid)
            if row is None:  # replaced by the indexer since the page catalog was read
                continue
            if allowed is not None and row.scope not in allowed:
                continue
            known[(hit.index, ref)] = cand = Candidate(hit.index, row, hit.score / 2, ["graph"])
            extra.append(cand)
    return extra
