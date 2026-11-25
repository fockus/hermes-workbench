"""``bulk(items, write, verify, key)``: a batch of writes that never writes an item twice (spec Task 10;
REQ-027, REQ-028).

The two functions below are plain sandbox code: ``SOURCE`` (their own text) joins the script prelude
(prelude.py), so a script calls ``await bulk(...)`` with no host round trip, and the host imports the same
functions. They use only what Monty runs: no imports, no classes.

``key(item)`` is the item id; ``write(item, retry_key)`` writes, and raising means its response was lost;
``verify(item)`` re-reads the object and answers ``"applied"``, ``"not_applied"`` or ``"changed"`` (changed
outside the job since the frozen version). ``write`` and ``verify`` may be async (they await tools).
"""

from __future__ import annotations

import inspect

CATEGORIES = ("confirmed", "verify_failed", "unknown", "not_processed")


async def _cm_resolved(value):
    """The value of a write or verify call: awaited when the script's function is async."""
    if type(value).__name__ == "coroutine":
        return await value
    return value


async def bulk(items, write, verify, key):
    """Write each item once, in order, and say what happened to it.

    The list and every item are frozen first (a copy), so items added or edited during the run are not
    used. Each item is re-read before its write: one already applied (a retry after a lost response) is
    confirmed without a second write, one changed outside the job stops the batch. After the write it is
    re-read again; the read before the write is lenient (one that raises, or answers None/False/""/0, counts
    as "not_applied" and is journaled as pre_read_failed), the one after is strict. Returns the item keys in exactly one of confirmed, verify_failed, unknown (the write's
    response was lost, or the re-read failed: re-read it before any retry) and not_processed, plus the
    journal of every step. The retry key ``bulk:<key>`` is the same on every run of the same item.
    """
    for arg, name in ((write, "write"), (verify, "verify"), (key, "key")):
        # Monty has no callable(); a coroutine is what verify(item) gives where verify belongs
        if arg is None or type(arg).__name__ == "coroutine" or isinstance(arg, (str, int, float, list, dict, tuple, set)):
            raise TypeError(
                "bulk(items, write, verify, key): %s must be a function, got %s. Expected: "
                "async def write(item, retry_key); async def verify(item) -> \"applied\" | "
                "\"not_applied\"; key(item) -> the item id" % (name, type(arg).__name__)
            )
    frozen = [dict(item) if isinstance(item, dict) else item for item in items]
    out = {
        "confirmed": [],
        "verify_failed": [],
        "unknown": [],
        "not_processed": [],
        "journal": [],
    }
    journal = out["journal"]
    seen = []
    stopped = False
    keys = [key(item) for item in frozen]  # a bad key raises here, before the first write
    for item, k in zip(frozen, keys):
        if k in seen:  # before the stop check: a repeated key stays in its first category only
            journal.append(
                {
                    "key": k,
                    "event": "duplicate",
                    "detail": "the key came up before: not written again",
                }
            )
            continue
        seen.append(k)
        if stopped:
            out["not_processed"].append(k)
            continue
        retry_key = "bulk:" + str(k)
        wrote = False
        try:
            try:
                before = await _cm_resolved(verify(item))
            except Exception as exc:
                if str(exc).startswith("call limit of"):
                    raise
                journal.append({"key": k, "event": "pre_read_failed", "detail": str(exc)})
                before = "not_applied"
            if not before:
                journal.append(
                    {"key": k, "event": "pre_read_failed", "detail": "verify answered %r" % (before,)}
                )
                before = "not_applied"
            if before == "applied":
                journal.append(
                    {"key": k, "event": "already_applied", "detail": "not written again"}
                )
                out["confirmed"].append(k)
                continue
            if before != "not_applied":
                journal.append(
                    {
                        "key": k,
                        "event": "changed",
                        "detail": "re-read before the write answered %r: verify must return "
                        "\"applied\" or \"not_applied\" (\"changed\" if the item was edited "
                        "outside the job): the batch stopped here" % (before,),
                    }
                )
                out["not_processed"].append(k)
                stopped = True
                continue
            wrote = True
            try:
                await _cm_resolved(write(item, retry_key))
            except Exception as exc:
                journal.append(
                    {"key": k, "event": "lost", "retry_key": retry_key, "detail": str(exc)}
                )
                out["unknown"].append(k)
                continue
            try:
                after = await _cm_resolved(verify(item))
            except Exception as exc:
                journal.append(
                    {
                        "key": k,
                        "event": "unverified",
                        "retry_key": retry_key,
                        "detail": str(exc),
                    }
                )
                out["unknown"].append(k)
                continue
        except BaseException as exc:
            # only the script's call limit is absorbed: the batch stops, the keys stay accounted for and
            # the limit still holds (every later call raises again). A human refusal, an interrupt and
            # anything else go on up and stop the script. The prefix is limits.call_limit_stop's text
            # (sandbox code cannot import it); keep the two in step.
            if not str(exc).startswith("call limit of"):
                raise
            journal.append({"key": k, "event": "stopped", "detail": str(exc)})
            out["unknown" if wrote else "not_processed"].append(k)
            stopped = True
            continue
        if after == "applied":
            journal.append({"key": k, "event": "written", "retry_key": retry_key})
            out["confirmed"].append(k)
        elif after == "not_applied":
            journal.append({"key": k, "event": "not_applied", "retry_key": retry_key})
            out["verify_failed"].append(k)
        else:
            journal.append(
                {
                    "key": k,
                    "event": "changed",
                    "retry_key": retry_key,
                    "detail": "re-read after the write answered %r: verify must return "
                    "\"applied\" or \"not_applied\" (\"changed\" if the item was edited "
                    "outside the job): the batch stopped here" % (after,),
                }
            )
            out["verify_failed"].append(k)
            stopped = True
    return out


SOURCE = "\n\n" + inspect.getsource(_cm_resolved) + "\n\n" + inspect.getsource(bulk)
