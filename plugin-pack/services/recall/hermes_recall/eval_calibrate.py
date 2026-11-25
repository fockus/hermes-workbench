"""Drift calibration of hermes-recall (spec Task 10; REQ-007, design C5).

    python -m hermes_recall.eval --calibrate <pairs.jsonl> --model <name>

Each line of the pairs file is ``{"history": [<=5 user messages], "message": str, "shift": bool}``.
drift = cosine(``embed_query(message)``, mean of ``embed_query(h)`` over the last 5 of ``history``). A
message under ``MIN_WORDS`` words is skipped and counted: the trigger ignores those (REQ-007). The
threshold maximises F1 of "shift when cosine < t" over the midpoints between the sorted cosines; a tie
goes to the smaller t.

Prints ``drift threshold=<t> f1=<f> pairs=<n> skipped=<m>`` (``n`` = pairs scored) and writes
``$HERMES_HOME/recall/drift.json`` = ``{model_id, threshold, f1, pairs, skipped}``, read by the trigger (RC-TRG).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Callable, Sequence

import numpy as np

MIN_WORDS = 8
HISTORY = 5


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def write_json(path: Path, data: dict) -> None:
    """Write ``data`` to ``path`` atomically (temporary file in the same folder, then ``os.replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def read_jsonl(path: Path, check: Callable[[object], bool], shape: str) -> list[dict]:
    """The JSON objects of ``path``, one per non-empty line; a line ``check`` rejects exits 2 with its number."""
    out = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            row = None
        if not check(row):
            print("%s:%d: expected %s" % (path, n, shape), file=sys.stderr)
            raise SystemExit(2)
        out.append(row)
    return out


def _is_pair(row: object) -> bool:
    return (isinstance(row, dict) and isinstance(row.get("message"), str) and isinstance(row.get("shift"), bool)
            and isinstance(row.get("history"), list) and bool(row["history"])
            and all(isinstance(h, str) for h in row["history"]))


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    norm = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b) / norm if norm else 0.0


def drift(embedder, history: Sequence[str], message: str) -> float:
    centre = np.mean([np.asarray(embedder.embed_query(h), dtype=np.float64) for h in history[-HISTORY:]], axis=0)
    return _cosine(np.asarray(embedder.embed_query(message), dtype=np.float64), centre)


def _f1(cosines: Sequence[float], shifts: Sequence[bool], t: float) -> float:
    tp = sum(1 for c, s in zip(cosines, shifts) if c < t and s)
    fp = sum(1 for c, s in zip(cosines, shifts) if c < t and not s)
    fn = sum(1 for c, s in zip(cosines, shifts) if c >= t and s)
    return 2 * tp / (2 * tp + fp + fn) if tp else 0.0


def best_threshold(cosines: Sequence[float], shifts: Sequence[bool]) -> tuple[float, float]:
    """``(t, F1)`` maximising F1 of "shift when cosine < t"; candidates are the midpoints between the sorted
    distinct cosines, ascending, so the first best is the smallest t."""
    values = sorted(set(cosines))
    if len(values) < 2:
        raise ValueError("calibration needs at least two distinct cosines, got %d" % len(values))
    best = (0.0, -1.0)
    for lo, hi in zip(values, values[1:]):
        t = (lo + hi) / 2
        f1 = _f1(cosines, shifts, t)
        if f1 > best[1]:
            best = (t, f1)
    return best


def calibrate(pairs_path: Path, model: str, loader: Callable) -> int:
    pairs = read_jsonl(pairs_path, _is_pair, '{"history": [str, ...], "message": str, "shift": bool}')
    embedder = loader(model)
    cosines, shifts, skipped = [], [], 0
    try:
        for pair in pairs:
            if len(pair["message"].split()) < MIN_WORDS:
                skipped += 1
                continue
            cosines.append(drift(embedder, pair["history"], pair["message"]))
            shifts.append(pair["shift"])
    finally:
        embedder.unload()
    try:
        t, f1 = best_threshold(cosines, shifts)
    except ValueError as exc:
        print("%s: %s" % (pairs_path, exc), file=sys.stderr)
        return 2
    print("drift threshold=%.3f f1=%.3f pairs=%d skipped=%d" % (t, f1, len(cosines), skipped))
    write_json(hermes_home() / "recall" / "drift.json",
               {"model_id": embedder.model_id, "threshold": t, "f1": f1, "pairs": len(cosines), "skipped": skipped})
    return 0
