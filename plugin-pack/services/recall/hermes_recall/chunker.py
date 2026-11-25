"""Markdown-aware chunker of hermes-recall (spec Task 2; REQ-001, REQ-011, REQ-025).

A document becomes chunks of at most ``max_tokens`` (400) tokens, header included, so a chunk fits
FRIDA's 512-token window. An ATX heading outside a code fence starts a new chunk; a chunk split for
size opens the next one with the tail of its lines, about ``overlap`` (20 %) of the budget. A line too
long for one chunk is cut into word pieces. Every chunk carries the header
``[{source} · {ref} · {date} · {scope}]``. A document is capped at ``cap`` (2000) chunks, and binary
input is skipped; both decisions come back as ``ChunkResult.note`` for the store to record.

Tokens are counted by a deterministic estimator, not a model tokenizer (no model files are installed;
exact tokenizer counts are the embedder's, RC-EMB): every word costs one token per 4 ASCII characters
or per 3 other characters (rounded up, at least 1), every punctuation mark one token. It errs high,
which keeps chunks inside the model window.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

MAX_TOKENS = 400
OVERLAP = 0.2
CAP = 2000
_TOKEN = re.compile(r"\w+|[^\w\s]")
_PIECE = re.compile(r"\s*(?:\w+|[^\w\s])")  # one estimator token with the whitespace before it
_HEADING = re.compile(r"^#{1,6}(\s|$)")
_FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    lines: str  # "first-last", 1-based line numbers in the document
    header: str
    text: str


@dataclass(frozen=True)
class ChunkResult:
    chunks: list[Chunk]
    note: str | None  # "capped: ..." or "binary: skipped", else None


@dataclass(frozen=True)
class _Unit:
    line: int
    text: str
    tokens: int
    heading: bool


def header(source: str, ref: str, date: str, scope: str) -> str:
    return "[%s · %s · %s · %s]" % (source, ref, date, scope)


def estimate_tokens(text: str) -> int:
    total = 0
    for tok in _TOKEN.findall(text):
        per = 4 if tok.isascii() else 3
        total += max(1, math.ceil(len(tok) / per)) if tok[0].isalnum() or tok[0] == "_" else 1
    return total


def is_binary(data: bytes) -> bool:
    if b"\0" in data:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def chunk_document(data: bytes | str, *, source: str, ref: str, date: str, scope: str,
                   max_tokens: int = MAX_TOKENS, overlap: float = OVERLAP, cap: int = CAP) -> ChunkResult:
    """Chunks of ``data`` (bytes are decoded as UTF-8; binary input yields no chunks and a note)."""
    if isinstance(data, bytes):
        if is_binary(data):
            return ChunkResult([], "binary: skipped")
        data = data.decode("utf-8")
    elif "\0" in data:
        return ChunkResult([], "binary: skipped")
    head = header(source, ref, date, scope)
    budget = max_tokens - estimate_tokens(head)
    if budget < 1:
        return ChunkResult([], "skipped: header leaves no room for the body within %d tokens" % max_tokens)
    tail_budget = max(1, int(budget * overlap))
    groups = _pack(_units(data, tail_budget, budget), budget, tail_budget)
    chunks = [Chunk(n, lines, head, text) for n, (lines, text) in enumerate(groups[:cap])]
    note = None
    if len(groups) > cap:
        note = "capped: indexed %d of %d chunks (limit %d)" % (cap, len(groups), cap)
    return ChunkResult(chunks, note)


def _units(text: str, piece: int, budget: int) -> list[_Unit]:
    """One unit per line; a line over ``budget`` tokens is cut into slices of about ``piece`` tokens."""
    units: list[_Unit] = []
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if _FENCE.match(line):
            fenced = not fenced
        heading = not fenced and bool(_HEADING.match(line))
        tokens = estimate_tokens(line)
        if tokens <= budget:
            units.append(_Unit(number, line, tokens, heading))
            continue
        words: list[str] = []
        size = 0
        first = heading
        for word in _pieces(line, piece):
            cost = estimate_tokens(word)
            if words and size + cost > piece:
                units.append(_Unit(number, "".join(words), size, first))
                words, size, first = [], 0, False
            words.append(word)
            size += cost
        if words:
            units.append(_Unit(number, "".join(words), size, first))
    return units


def _pieces(line: str, piece: int) -> list[str]:
    """Slices of ``line`` that concatenate back to it, each at most ``piece`` tokens."""
    out: list[str] = []
    stripped = line.rstrip()  # the trailing run has no token after it; scanning it with _PIECE is quadratic
    for match in _PIECE.finditer(stripped):
        text = match.group()
        if estimate_tokens(text) <= piece:
            out.append(text)
            continue
        step = piece * 3  # >= 3 characters per estimated token, ASCII or not
        out.extend(text[i:i + step] for i in range(0, len(text), step))
    if out:
        out[-1] += line[len(stripped):]
    return out


def _pack(units: list[_Unit], budget: int, tail_budget: int) -> list[tuple[str, str]]:
    groups: list[tuple[str, str]] = []
    current: list[_Unit] = []
    size = 0
    for unit in units:
        if current and (unit.heading or size + unit.tokens > budget):
            _emit(current, groups)
            current = [] if unit.heading else _tail(current, tail_budget, budget - unit.tokens)
            size = sum(u.tokens for u in current)
        current.append(unit)
        size += unit.tokens
    _emit(current, groups)
    return groups


def _tail(units: list[_Unit], target: int, room: int) -> list[_Unit]:
    """The last units of a chunk worth at most ``target`` tokens (and ``room``), never all of it.

    When the last unit alone is over that limit, the tail is a suffix of its text instead."""
    limit = min(target, room)
    taken: list[_Unit] = []
    size = 0
    for unit in reversed(units[1:]):
        if size + unit.tokens > limit:
            break
        taken.insert(0, unit)
        size += unit.tokens
    if not taken and limit > 0 and units and units[-1].tokens > limit:
        last = units[-1]
        suffix: list[str] = []
        for part in reversed(_pieces(last.text, limit)):
            cost = estimate_tokens(part)
            if size + cost > limit:
                break
            suffix.insert(0, part)
            size += cost
        if suffix:
            return [_Unit(last.line, "".join(suffix).lstrip(), size, False)]
    while taken and not taken[0].text.strip():
        taken.pop(0)
    return taken


def _emit(units: list[_Unit], groups: list[tuple[str, str]]) -> None:
    body = [u for u in units if u.text.strip()]
    if not body:
        return
    first, last = units.index(body[0]), len(units) - units[::-1].index(body[-1])
    parts: list[str] = []
    previous = None
    for unit in units[first:last]:
        if parts and previous is not None and unit.line == previous:
            parts[-1] += unit.text
        else:
            parts.append(unit.text)
        previous = unit.line
    groups.append(("%d-%d" % (body[0].line, body[-1].line), "\n".join(parts)))
