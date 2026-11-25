"""Format and redaction mark of a hermes-recall index (I-054; REQ-024).

An index file stamps ``format_version`` and ``redaction_version`` into ``index_meta``. The service opens an
index only when both equal this module's numbers; any other index (an older or newer mark, or data with no
mark) is *outdated*: it is not searched and not written until an operator rebuilds it, because its chunks
were redacted under other rules and re-redacting them in place would be a guess. Bump:

* ``FORMAT_VERSION`` when the chunk or table layout changes (store.SCHEMA, the chunk header/text shape);
* ``REDACTION_VERSION`` when the ``_redact`` arguments or any redaction rule the indexer relies on change.

CLI (stop the service first; the derived index is replaced, the sources are never touched)::

    python -m hermes_recall.index_version list [--home H]
    python -m hermes_recall.index_version rebuild <profile|shared-wiki> [--home H]

A rebuild touches only the index it names (``rebuild default`` leaves a healthy ``shared-wiki`` alone, and
the other way round). It builds the new index beside the old one and swaps it in only when every source was
read completely: an unreadable ``projects.db`` or ``state.db`` keeps the old index and exits 1. Accepted
``session_summary`` entries live only in the index (state.db holds no copy), so a rebuild that would drop
them is refused (exit 2) unless ``--drop-summaries`` says so. Exit 0 done, 1 the rebuild reported an
error, 2 bad usage.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Sequence

if TYPE_CHECKING:  # the store needs numpy; the server's ``--check`` must run without it
    from hermes_recall.store import Index

FORMAT_VERSION = 1
REDACTION_VERSION = 1
CURRENT = {"format_version": str(FORMAT_VERSION), "redaction_version": str(REDACTION_VERSION)}
_MARKS = "SELECT key, value FROM index_meta WHERE key IN ('format_version', 'redaction_version')"
Verdict = Literal["new", "current", "outdated", "incompatible"]


class IndexOutdated(Exception):
    """The index ``profile`` (a profile or ``shared-wiki``) carries a mark this code does not accept."""

    def __init__(self, profile: str, home: Path | str):
        self.profile = profile
        self.command = "python -m hermes_recall.index_version rebuild %s --home %s" % (
            shlex.quote(profile), shlex.quote(str(home)))
        super().__init__("index %s is outdated; stop the service and run: %s" % (profile, self.command))


def stamp(index: "Index") -> None:
    index.submit(lambda conn: conn.executemany(
        "INSERT OR REPLACE INTO index_meta(key, value) VALUES (?, ?)", list(CURRENT.items()))).result()


def _classify(found: dict[str, str], has_sources: bool) -> Verdict:
    """``new``: nothing to protect (no mark, no data); ``incompatible``: no or another ``format_version`` (data
    with no mark at all included): a table layout of unknown shape, which must not be queried or given our
    DDL; ``outdated``: our layout but not our redaction mark."""
    if found == CURRENT:
        return "current"
    if not found and not has_sources:
        return "new"
    if found.get("format_version") != CURRENT["format_version"]:
        return "incompatible"
    return "outdated"


def state(index: "Index") -> Literal["current", "outdated"]:
    """``current`` for an index marked with this code's numbers, and for an empty unmarked one (stamped
    here: nothing to protect); everything else is ``outdated``. A mark is never rewritten, so an index
    of a newer code is not downgraded."""
    verdict = _classify(dict(index.rows(_MARKS)), bool(index.rows("SELECT 1 FROM sources LIMIT 1")))
    if verdict == "new":
        stamp(index)
    return "current" if verdict in ("new", "current") else "outdated"


def _query(path: Path, sql: str) -> list[tuple]:
    """Rows of ``sql`` over a read-only connection (no DDL, no write); [] when the table is absent."""
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return []
    finally:
        conn.close()


def verdict(path: Path) -> Verdict:
    """Classify the index file at ``path`` before anything opens it for writing, so no DDL ever reaches a
    file of another layout. A missing file is ``new``."""
    if not path.exists():
        return "new"
    return _classify(dict(_query(path, _MARKS)), bool(_query(path, "SELECT 1 FROM sources LIMIT 1")))


def _summaries(path: Path) -> int | None:
    """How many accepted ``session_summary`` sources the index file holds; None when that cannot be read,
    which includes a file that has tables but no ``sources`` (an unknown schema never counts as zero)."""
    if not path.exists():
        return 0
    try:
        tables = {name for (name,) in _query(path, "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if tables and "sources" not in tables:
            return None
        rows = _query(path, "SELECT COUNT(*) FROM sources WHERE kind = 'session_summary'")
    except sqlite3.Error:
        return None
    return rows[0][0] if rows else 0


def _checkpoint(final: Path) -> None:
    """Fold the old file's WAL into it; raises unless that completed, so removing the sidecar files after
    it cannot lose a committed transaction if the swap is interrupted before ``os.replace``."""
    if not final.exists() or not final.with_name(final.name + "-wal").exists():
        return
    conn = sqlite3.connect(final)
    try:
        busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
    finally:
        conn.close()
    if busy:
        raise RuntimeError("the old index %s is in use: its WAL could not be checkpointed" % final.name)


def _swap(build: Path, final: Path) -> None:
    """Put the finished index of ``build`` in place of ``final`` (old WAL files first: they belong to the old
    file, and are removed only after a successful checkpoint of it)."""
    final.parent.mkdir(parents=True, exist_ok=True)
    _checkpoint(final)
    for suffix in ("-wal", "-shm"):
        final.with_name(final.name + suffix).unlink(missing_ok=True)
    os.replace(build / final.name, final)


def rebuild(home: Path, name: str, *, drop_summaries: bool = False):
    """Replace the index ``name`` (and only it) by one derived afresh from its sources through the normal
    indexer path (stock secret hydration and ``_redact`` included). The new index is built in a side
    directory and swapped in only when ``Report.errors`` is empty; otherwise the old file stays. Raises
    ``ValueError`` for an unknown profile or when ``session_summary`` entries would be lost without
    ``drop_summaries``. ponytail: no lock against a running service (ceiling: stop it first); upgrade
    path: an admin endpoint in the service that swaps the index under its own write queue."""
    from hermes_recall import sources
    from hermes_recall.indexer import Indexer
    from hermes_recall.store import index_path

    owner = "default" if name == sources.SHARED_INDEX else name  # the shared wiki is filled by default's reconcile
    if owner not in sources.profiles(home):
        raise ValueError("no such profile: %s" % name)
    final = index_path(home, name)
    held = _summaries(final)
    if held != 0 and not drop_summaries:
        raise ValueError("index %s holds %s accepted session_summary entries that exist only in the index; a "
                         "rebuild drops them. Pass --drop-summaries to rebuild anyway"
                         % (name, "unreadable" if held is None else held))
    final.parent.mkdir(parents=True, exist_ok=True)
    # one directory per run, so two runs on the same index never delete each other's half-built file
    with tempfile.TemporaryDirectory(prefix=".rebuild-%s-" % name, dir=final.parent, ignore_cleanup_errors=True) as tmp:
        build = Path(tmp)
        indexer = Indexer(home, index_dir=build, only_index=name)
        try:
            report = indexer.reconcile(owner) if name == sources.SHARED_INDEX else indexer.catch_up(only=owner)
        finally:
            indexer.close()
        if not report.errors:
            try:
                _swap(build, final)
            except (OSError, RuntimeError, sqlite3.Error) as exc:  # the old index stays, as for a read error
                report.errors[name] = "swap failed: %s: %s" % (type(exc).__name__, exc)
    return report


def _list(home: Path) -> int:
    from hermes_recall import sources
    from hermes_recall.store import index_path

    for name in [*(p for p in sources.profiles(home) if p != sources.SHARED_INDEX), sources.SHARED_INDEX]:
        path = index_path(home, name)
        if path.exists():
            print(name, "current" if verdict(path) in ("new", "current") else "outdated")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hermes_recall.index_version")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--home", default=None, help="Hermes root (default: $HERMES_HOME or ~/.hermes)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", parents=[common], help="show every index and whether it is current or outdated")
    rebuilding = sub.add_parser("rebuild", parents=[common], help="replace one index from its sources (stop the service first)")
    rebuilding.add_argument("profile")
    rebuilding.add_argument("--drop-summaries", action="store_true",
                            help="accept losing session_summary entries, which only the index holds")
    args = parser.parse_args(argv)
    home = Path(args.home or os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    if args.command == "list":
        return _list(home)
    try:
        report = rebuild(home, args.profile, drop_summaries=args.drop_summaries)
    except ValueError as exc:
        print("recall: %s" % exc, file=sys.stderr)
        return 2
    for name, error in report.errors.items():
        print("recall: %s: %s" % (name, error), file=sys.stderr)
    print("recall: rebuilt %s: %d sessions, %d documents" % (args.profile, report.sessions, report.indexed))
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
