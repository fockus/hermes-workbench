"""File reconciliation and start-up catch-up of the hermes-recall indexer (spec Task 4; REQ-014,
REQ-015, REQ-023), split out of indexer.py by size.

* ``reconcile(indexer, profile)``: every document of ``sources.documents``; its sha256 and scope
  unchanged -> nothing; else it is re-chunked and replaced, which drops its old chunks and vectors, so
  only its new chunks wait for embedding (REQ-014). A bank or wiki source whose path is gone (deleted
  or renamed) is deleted from the index (REQ-023); a file that exists but cannot be read, or a profile
  whose ``projects.db`` cannot be read, prunes nothing. Cap and binary notes come from ``chunk_document``.
* ``catch_up(indexer)``: per profile, each session of ``state.db`` with no indexed mark or updated after
  its own mark (``sources.mtime`` of kinds ``session`` and ``compaction`` per ref: the session's
  ``updated_at`` when catch-up wrote it, the newest turn or summary time when a live call did) is
  re-indexed from ``state.db`` (turns and compaction summaries replaced); a session indexed but gone
  from a readable ``state.db`` is deleted (REQ-022); then the profile is reconciled (REQ-015).

Every text is redacted with Hermes's own redactor, forced on, before it is chunked (REQ-024): Hermes
stores ``state.db`` content and the notes unredacted. ``owner_scope`` runs it inside the stock
``bind_profile_scope`` of the profile that owns the source, so that profile's registered secret-source
values are masked even when another profile's process indexes it. A session's ``/undo`` changes no
timestamp, only ``sessions.rewind_count``: catch-up records it in the session source's hash as
``"<rewind_count>:<sha256>"`` (a live-written ``""`` counts as 0) and reloads a session whose count moved;
a kind with no visible rows left is deleted.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from hermes_recall import sources
from hermes_recall.chunker import Chunk, chunk_document, is_binary
from hermes_recall.indexer_secrets import INCOMPLETE, SecretsUnavailable, clear_incomplete, owner_scope
from hermes_recall.store import Source

if TYPE_CHECKING:
    from hermes_recall.indexer import Indexer

FILE_KINDS = ("memory_bank", "wiki")
SESSION_KINDS = ("session", "session_summary", "compaction")


@dataclass
class Report:
    indexed: int = 0  # documents (re)chunked
    unchanged: int = 0  # documents whose hash and scope matched
    removed: int = 0  # documents gone from disk, deleted from the index
    sessions: int = 0  # sessions (re)indexed by catch-up
    pruned: int = 0  # sessions gone from state.db, deleted by catch-up
    errors: dict[str, str] = field(default_factory=dict)  # profile -> why catch-up skipped it

    def add(self, other: "Report") -> "Report":
        self.indexed += other.indexed
        self.unchanged += other.unchanged
        self.removed += other.removed
        self.sessions += other.sessions
        self.pruned += other.pruned
        self.errors.update(other.errors)
        return self


def day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _redact(text: str) -> str:
    from agent.redact import redact_sensitive_text

    return redact_sensitive_text(text, force=True, file_read=True, secret_file=True, redact_url_credentials=True)


def message_chunks(kind: str, ref: str, scope: str, items: Iterable[tuple[str, float]]) -> list[Chunk]:
    """Chunks of each ``(text, ts)`` under a header with that message's date, numbered in order."""
    out: list[Chunk] = []
    for text, ts in items:
        for chunk in chunk_document(_redact(text), source=kind, ref=ref, date=day(ts), scope=scope).chunks:
            out.append(replace(chunk, ordinal=len(out)))
    return out


def _index_name(profile: str, doc: sources.Doc) -> str:
    return sources.SHARED_INDEX if doc.scope == sources.SHARED else profile


def _shared_docs(home: Path) -> list[sources.Doc]:
    """The shared wiki alone, for a pass that builds only that index: ``sources.documents("default")`` reads
    ``projects.db`` first, and a corrupt one must not block a rebuild of a readable shared wiki.
    ponytail: ``_wiki`` is sources' private lister; upgrade path: a public ``shared_documents`` in sources."""
    return [sources.Doc("wiki", str(p), p, sources.SHARED, None) for p in sources._wiki(home / "llm-wiki")]


def reconcile(indexer: "Indexer", profile: str) -> Report:
    with owner_scope(indexer, profile):
        return _reconcile(indexer, profile)


def _reconcile(indexer: "Indexer", profile: str) -> Report:
    """The profile's index and, for ``default``, the shared wiki. An outdated index is skipped and named in
    ``Report.errors``; the owner's others still reconcile, so a deleted wiki page leaves the shared index."""
    report = Report()
    names = [profile] + ([sources.SHARED_INDEX] if profile == "default" else [])
    if indexer.only_index is not None:
        names = [name for name in names if name == indexer.only_index]
    for name in list(names):
        if indexer.is_outdated(name):
            names.remove(name)
            report.errors[name] = "index_outdated"
    if not names:
        return report
    seen: dict[str, set[tuple[str, str]]] = {name: set() for name in names}
    docs = _shared_docs(indexer.home) if names == [sources.SHARED_INDEX] else sources.documents(indexer.home, profile)
    for doc in docs or []:
        if indexer.stop.is_set():
            return report  # stopping: the listing was not walked, so nothing is pruned
        name = _index_name(profile, doc)
        if name not in seen:
            continue  # an index this pass skips or does not build
        try:
            raw = doc.path.read_bytes()
            mtime = doc.path.stat().st_mtime
        except OSError:
            if doc.path.exists():
                seen[name].add((doc.kind, doc.ref))  # unreadable (e.g. a TCC denial), not deleted: keep it
                report.errors.setdefault(name, "incomplete read: %s unreadable" % doc.path)  # a rebuild must not swap this in
            continue  # vanished since listing: pruned below like any gone path
        seen[name].add((doc.kind, doc.ref))
        digest = hashlib.sha256(raw).hexdigest()
        index = indexer.index(name)
        if index.rows("SELECT 1 FROM sources WHERE kind = ? AND ref = ? AND hash = ? AND scope = ? "
                      "AND project_id IS ?", (doc.kind, doc.ref, digest, doc.scope, doc.project_id)):
            report.unchanged += 1
            continue
        result = chunk_document(raw if is_binary(raw) else _redact(raw.decode("utf-8")), source=doc.kind, ref=doc.ref, date=day(mtime), scope=doc.scope)
        source = Source(doc.kind, doc.ref, doc.scope, doc.project_id, sources.machine(), digest, mtime)
        index.put_source(source, result.chunks, note=result.note).result()
        report.indexed += 1
    if docs is None:  # projects.db unreadable: the listing is incomplete, so nothing is pruned or called done
        report.errors[profile] = "incomplete read: projects.db unreadable"
        return report
    for name in names:
        index = indexer.index(name)
        for kind, ref in index.rows("SELECT kind, ref FROM sources WHERE kind IN (?, ?)", FILE_KINDS):
            if (kind, ref) not in seen[name]:
                index.delete_source(kind, ref).result()
                report.removed += 1
    return report


_MARKS = ("SELECT ref, MAX(mtime), MAX(CASE WHEN instr(hash, ':') > 0 THEN "
          "CAST(substr(hash, 1, instr(hash, ':') - 1) AS INTEGER) ELSE 0 END) "
          "FROM sources WHERE kind IN ('session', 'compaction') GROUP BY ref")


# The select ``sources.sessions`` runs for its headers, with no rows: when it fails, ``sessions`` returns []
# although ``session_ids`` (a plain ``SELECT id``) works, which a rebuild would take for an empty history.
_HEADS_PROBE = ("SELECT s.id, s.cwd, %s, s.rewind_count FROM sessions s LIMIT 0" % sources._UPDATED)


def _put_session(indexer: "Indexer", profile: str, session: sources.Session) -> bool:
    index = indexer.index(profile)
    scope = sources.scope_label(session.project_id)
    put = False
    for kind, items in (("session", [(text, ts) for _, text, ts in session.messages]),
                        ("compaction", session.compactions)):
        if not items:  # a successful read with nothing visible left (all undone): drop what was indexed
            if index.rows("SELECT 1 FROM sources WHERE kind = ? AND ref = ?", (kind, session.id)):
                index.delete_source(kind, session.id).result()
                put = True
            continue
        digest = "%d:%s" % (session.rewind_count, hashlib.sha256(repr(items).encode("utf-8")).hexdigest())
        source = Source(kind, session.id, scope, session.project_id, sources.machine(), digest, session.updated_at)
        index.put_source(source, message_chunks(kind, session.id, scope, items)).result()
        put = True
    return put


def _catch_up_profile(indexer: "Indexer", profile: str, profile_home: Path, report: Report) -> bool:
    """One profile's catch-up into ``report``; False when stopping (nothing further is done)."""
    if indexer.is_outdated(profile):  # no session work; the owner's other indexes (shared wiki) still reconcile
        report.add(indexer.reconcile(profile))
        return True
    index = indexer.index(profile)
    recorded = index.rows(_MARKS)
    pending = {key[len(INCOMPLETE):]: token for key, token in
               index.rows("SELECT key, value FROM index_meta WHERE key LIKE ?", (INCOMPLETE + "%",))}
    marks = {ref: mtime for ref, mtime, _ in recorded if ref not in pending}  # no mark: read in full
    rewinds = {ref: rewound for ref, _, rewound in recorded}
    with owner_scope(indexer, profile):
        for session in sources.sessions(profile_home, marks=marks, rewinds=rewinds):
            if indexer.stop.is_set():
                return False  # stopping: no prune, no reconcile
            report.sessions += _put_session(indexer, profile, session)
            if session.id in pending:  # read whole from state.db: the gap a failed turn left is closed
                clear_incomplete(index, session.id, pending[session.id])
    # Indexed refs first, then state.db: a ref indexed live after this read is not pruned by mistake.
    indexed = [ref for (ref,) in index.rows("SELECT DISTINCT ref FROM sources WHERE kind IN (?, ?, ?)", SESSION_KINDS)]
    live = sources.session_ids(profile_home)
    if live is None and (profile_home / "state.db").is_file():
        report.errors[profile] = "incomplete read: state.db unreadable"
    if live is not None and sources._read(profile_home / "state.db", _HEADS_PROBE) is None:
        report.errors[profile] = "incomplete read: session headers of state.db unreadable"  # e.g. no rewind_count
    if live is not None:
        for ref in indexed:
            if ref not in live:
                indexer.delete_session(profile, ref)
                report.pruned += 1
    report.add(indexer.reconcile(profile))
    return True


def catch_up(indexer: "Indexer", only: str | None = None) -> Report:
    """Every profile, or just ``only``. A profile whose secrets did not hydrate, whose index is outdated or
    whose sources could not be read completely is named in ``Report.errors``; the others go on."""
    report = Report()
    for profile, profile_home in sources.profiles(indexer.home).items():
        if indexer.stop.is_set():
            return report
        if only is not None and profile != only:
            continue
        try:
            if not _catch_up_profile(indexer, profile, profile_home, report):
                return report
        except SecretsUnavailable as exc:
            report.errors[profile] = repr(exc)  # the next pass hydrates again
    return report
