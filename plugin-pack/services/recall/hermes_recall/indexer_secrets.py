"""Secret hydration and the "incomplete session" mark of the hermes-recall indexer (I-054), split out of
indexer_reconcile.py by size.

* ``owner_scope(indexer, profile)`` binds the owning profile (stock ``bind_profile_scope``) so Hermes's
  redactor masks that profile's secret-source values, after resolving its ``secrets:`` sources with the stock
  loader once per home. A failure (see ``_hydrate``) raises ``SecretsUnavailable`` before anything is chunked
  and is retried on the next pass;
* ``incomplete_on_failure`` / ``clear_incomplete``: a turn lost to such a failure marks its session in
  ``index_meta`` until catch-up re-reads the session whole from state.db.
"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from hermes_recall import sources

if TYPE_CHECKING:
    from hermes_recall.indexer import Indexer


class SecretsUnavailable(RuntimeError):
    """The secret sources of ``profile`` did not hydrate: indexing it now could write a value Hermes
    would have masked. Nothing of the profile is chunked; the next pass asks the stock loader again."""

    def __init__(self, profile: str, reason: str):
        super().__init__(profile, reason)
        self.profile, self.reason = profile, reason


def _secrets_config(home: Path) -> dict:
    """The ``secrets:`` section of ``home``'s config.yaml. A missing file is "no sources"; a file that cannot
    be read or parsed is an error here, whereas stock ``_load_secrets_config`` turns it into {} (also "no
    sources"), which would index the profile with its external secrets unmasked. Only the path and the error class are
    kept: a YAML error quotes the offending values (a duplicate ``api_key`` shows both) and this text reaches
    ``/v1/status`` and the log."""
    from hermes_cli.config_backend import config_exists, read_config_doc_readonly

    path = home / "config.yaml"
    try:
        if not config_exists(path):
            return {}
        doc = read_config_doc_readonly(path) or {}
        section = doc.get("secrets") or {}
    except Exception as exc:  # noqa: BLE001 - any read or parse failure means the sources are unknown
        raise RuntimeError("config.yaml of %s is unreadable: %s" % (home, type(exc).__name__)) from None
    if not isinstance(section, dict):
        raise RuntimeError("config.yaml of %s: secrets is not a mapping" % home)
    return section


def _hydrate(home: Path) -> None:
    """Run the stock hydration of ``home``; raise unless it is known to have succeeded. Stock fails open: it
    swallows an unreadable config or a source that did not apply and returns ``{}`` either way, and a second
    writer (``_apply_external_secret_sources``) marks the home in ``_APPLIED_HOMES`` even after a failed fetch.
    So the config is read here (an error raises), every explicitly enabled section must be a registered
    source (a source whose module failed to load is missing), the stock mark is cleared first so that only a
    mark set by this very call counts, and that mark is required whenever any source is enabled, also one
    that ``is_enabled()`` accepts without an ``enabled: true`` section (a ``vault_id``)."""
    from agent.secret_sources.registry import enabled_source_names
    from hermes_cli import env_loader
    from hermes_constants import hermes_home_key

    cfg = _secrets_config(home)
    wanted = {name for name, section in cfg.items() if isinstance(section, dict) and section.get("enabled")}
    enabled = enabled_source_names(cfg, home)
    missing = wanted - enabled
    if missing:
        raise RuntimeError("secret source(s) %s enabled in config.yaml but not available" % sorted(missing))
    env_loader.reset_secret_source_cache(home)  # drops a stale mark; this call re-pulls and re-marks
    env_loader.hydrate_profile_secret_sources(home)
    if enabled and hermes_home_key(home) not in env_loader._APPLIED_HOMES:
        raise RuntimeError("a configured secret source did not apply")


_HYDRATE_LOCKS: dict[str, threading.Lock] = {}  # the stock caches are process-wide, so one lock per home


def _hydrate_once(indexer: "Indexer", home: Path) -> None:
    """Check, hydrate and remember ``home`` under its lock: two passes retrying the same failed profile would
    otherwise race, the second resetting the mark the first just set and then failing with nothing applied."""
    with _HYDRATE_LOCKS.setdefault(str(home), threading.Lock()):
        if str(home) in indexer.hydrated:
            return
        _hydrate(home)
        indexer.hydrated.add(str(home))


@contextmanager
def owner_scope(indexer: "Indexer", profile: str) -> Iterator[None]:
    """Bind ``profile``'s home (stock ``bind_profile_scope``) for redaction. The registry of secret-source
    values lives in process memory, so this process (a launchd service) resolves the profile's ``secrets:``
    sources itself with the stock loader before its first index. A home is remembered only after a
    hydration that succeeded; a failure raises ``SecretsUnavailable`` before the yield, so the profile is
    not chunked and the next pass hydrates again. ``hydrate_secrets=False`` on the bind, as stock
    ``legacy_switch`` binds another profile: the launch profile ignores the flag."""
    home = sources.profiles(indexer.home).get(profile)
    if home is None:
        yield
        return
    from agent.runtime_scope import bind_profile_scope

    with bind_profile_scope(home, hydrate_secrets=False):
        try:
            _hydrate_once(indexer, home)
        except Exception as exc:  # noqa: BLE001 - stock may raise anything; all of it means "not hydrated"
            raise SecretsUnavailable(profile, repr(exc)) from exc
        yield


INCOMPLETE = "incomplete:"  # index_meta key prefix; the value is a token of the failure that set it


@contextmanager
def incomplete_on_failure(index, session_id: str) -> Iterator[None]:
    """A turn or summary skipped because the secrets did not hydrate is missing from the index, yet a later
    turn of the same session moves its mark past state.db's time, so catch-up would never read the gap
    again. Record the session as incomplete (durably, in the index); only a full re-read clears it."""
    try:
        yield
    except SecretsUnavailable:
        index.submit(lambda conn: conn.execute("INSERT OR REPLACE INTO index_meta(key, value) VALUES (?, ?)",
                                               (INCOMPLETE + session_id, uuid.uuid4().hex))).result()
        raise


def clear_incomplete(index, session_id: str, token: str | None = None) -> None:
    """Drop the mark; with ``token`` only the one a catch-up read (a failure that came later keeps its own)."""
    sql, args = "DELETE FROM index_meta WHERE key = ?", [INCOMPLETE + session_id]
    if token is not None:
        sql, args = sql + " AND value = ?", args + [token]
    index.submit(lambda conn: conn.execute(sql, args)).result()
