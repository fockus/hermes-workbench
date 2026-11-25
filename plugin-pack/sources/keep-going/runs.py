"""Which turns are the user's, and the 24 h task clock that survives restarts (REQ-019, REQ-021).

- ``genuine(user_message, platform, history)``: a message the person sent, text or only an image. Runtime
  turns are not, told by the metadata Hermes puts on the turn's user row (the last one in ``history``):
  a notification ``display_kind`` (heartbeat, process, delegation, internal gateway event) or a
  ``notification_category``; else by the stock headers: keep-going's own continuations, background and
  delegation notices, goal and kanban continuations, Hermes's ``[System: …]`` nudges. Cron runs and
  every turn of a kanban worker (all its messages come from kanban) are runtime turns too. Only a
  genuine message lifts a watchdog stop or starts a new task.
- ``task_state(key, now, restart)``: the task's record under ``key``: when it began, the watchdog's stop
  report, whether the person has yet to see it, its run-budget checkpoint and the binding of the
  continuation that opens its next run; ``store(key, record)`` writes it. It is kept in a 0600 JSON file per
  key under ``$HERMES_HOME/keep-going/`` (folder 0700, opened without following links; work happens
  relative to its descriptor, state files read without blocking and only as this user's regular files;
  only keep-going's own state files are ever removed, and never a stopped one), so a gateway restart or
  a week of quiet neither resets the 24 h total nor lifts a stop or a checkpoint. A record fits in the
  bytes read back: its report is cut to fit, the stop is a flag of its own. ``restart`` (a genuine
  message) starts the task anew at ``now``, not stopped.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

RUNTIME_PREFIXES = ("[keep-going]", "[ASYNC DELEGATION", "[System:", "[IMPORTANT:", "[CONTEXT", "[PRIOR CONTEXT",
                    "[Your active task list", "[Planning state preserved", "Cronjob Response:", "[kanban]",
                    "[Background process ", "[Continuing toward ")
# Stock user-row kinds of turns no person sent: a heartbeat wake (``hidden``) and the gateway's machinery
# kinds (gateway.response_filters.MACHINERY_DISPLAY_KINDS).
RUNTIME_KINDS = frozenset({"hidden", "internal_notification", "process_complete", "async_delegation_complete"})
IMAGE_PARTS = frozenset({"image_url", "input_image", "image"})
KEEP_SECONDS = 7 * 86400  # state files untouched this long are removed when another is written


def _text(message) -> str:
    if isinstance(message, list):  # multimodal content parts
        return "\n".join(str(part.get("text") or "") for part in message if isinstance(part, dict))
    return str(message or "")


def _turn_row(user_message, history) -> dict:
    """The turn's own user row: the last one Hermes appended to the history, else what the message carries."""
    for row in reversed(history or ()):
        if isinstance(row, dict) and row.get("role") == "user":
            return row
    return {"display_kind": getattr(user_message, "display_kind", None),
            "display_metadata": {"notification_category": getattr(user_message, "notification_category", None)}}


def genuine(user_message, platform: str = "", history=None) -> bool:
    if platform == "cron" or os.environ.get("HERMES_KANBAN_TASK"):
        return False
    row = _turn_row(user_message, history)
    metadata = row.get("display_metadata")
    if row.get("display_kind") in RUNTIME_KINDS or (isinstance(metadata, dict) and metadata.get("notification_category")):
        return False
    text = _text(user_message).strip()
    if text:
        return not text.startswith(RUNTIME_PREFIXES)
    return isinstance(user_message, list) and any(isinstance(p, dict) and p.get("type") in IMAGE_PARTS
                                                  for p in user_message)


def task_key(session_id: str) -> str:
    """A kanban worker's task spans its re-dispatched sessions; anything else is its session."""
    task = os.environ.get("HERMES_KANBAN_TASK")
    return "kanban:%s" % task if task else "session:%s" % session_id


def _state_dir() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "keep-going"


_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_STATE_NAME = re.compile(r"[0-9a-f]{20}\.json")  # what keep-going writes; nothing else is ever removed
_READ_CAP = 16384  # a state file is written to fit in what is read back
ESCAPED_MAX = 12  # bytes of one character in ASCII JSON at most (a non-BMP one is two \uXXXX escapes)
LOST_REPORT = "Stopped by the keep-going watchdog (its report was not kept)."
EMPTY = {"report": None, "unseen": False, "checkpoint": False, "continuation": None}


def _open_folder(folder: Path, create: bool) -> int:
    """The state folder, never through a link, owned by this user and 0700; ``OSError`` otherwise."""
    if create:
        folder.parent.mkdir(parents=True, exist_ok=True)
    parent = os.open(folder.parent, os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC)
    try:
        if create:
            try:
                os.mkdir(folder.name, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
        fd = os.open(folder.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC, dir_fd=parent)
    finally:
        os.close(parent)
    info = os.fstat(fd)
    if info.st_uid != os.getuid():
        os.close(fd)
        raise PermissionError("%s is not owned by this user" % folder)
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.fchmod(fd, 0o700)
    return fd


def _load(dir_fd: int, name: str) -> dict:
    """The JSON object in ``name`` when it is this user's regular file: opened without following a link
    or blocking (a FIFO), checked on its descriptor before any read, closed on every path."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _CLOEXEC, dir_fd=dir_fd)
    except OSError:
        return {}
    try:
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                return {}
            data = json.loads(handle.read(_READ_CAP))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read(folder: Path, name: str) -> dict:
    try:
        dir_fd = _open_folder(folder, create=False)
    except OSError:
        return {}
    try:
        return _load(dir_fd, name)
    finally:
        os.close(dir_fd)


def _stopped(record: dict) -> bool:
    return any(isinstance(v, dict) and v.get("stopped") for v in record.values())


def _write(folder: Path, name: str, data: bytes) -> None:
    dir_fd = _open_folder(folder, create=True)
    try:
        tmp = ".tmp-%s" % uuid.uuid4().hex
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _CLOEXEC, 0o600, dir_fd=dir_fd)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        cutoff = time.time() - KEEP_SECONDS
        for old in os.listdir(dir_fd):
            info = os.stat(old, dir_fd=dir_fd, follow_symlinks=False)
            if (_STATE_NAME.fullmatch(old) and stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                    and info.st_mtime < cutoff and not _stopped(_load(dir_fd, old))):  # a stop outlives retention
                os.unlink(old, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


def _name(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:20] + ".json"


def _fitted(key: str, record: dict) -> bytes:
    """The state file of ``key``'s record in at most ``_READ_CAP`` bytes: whole characters of the report are
    cut until it fits; the stop flag does not depend on what is left of it."""
    report = (record.get("report") or "")[:_READ_CAP]  # a character takes at least one byte
    record = dict(EMPTY, **record)
    record["stopped"] = bool(record["report"])
    while True:
        data = json.dumps({key: dict(record, report=report or None)}).encode("ascii")
        over = len(data) - _READ_CAP
        if over <= 0 or not report:
            return data
        report = report[:len(report) - max(1, over // ESCAPED_MAX)]


def store(key: str, record: dict) -> None:
    """``record`` (``started`` and the keys of ``EMPTY``) as the task state of ``key``."""
    folder = _state_dir()
    try:
        _write(folder, _name(key), _fitted(key, record))
    except OSError as exc:  # the state still holds in memory; only the restart memory is lost
        logger.warning("keep-going watchdog: cannot store the task state in %s: %s", folder, exc)


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parsed(record) -> dict | None:
    """A stored record as ``store`` takes it. A bare number is a start stored before stops were kept, a
    ``stopped`` string a report stored before the stop had a flag of its own."""
    if _number(record):
        return dict(EMPTY, started=float(record))
    if not isinstance(record, dict) or not _number(record.get("started")):
        return None
    report, stopped, binding = record.get("report"), record.get("stopped"), record.get("continuation")
    if not (isinstance(report, str) and report):
        report = stopped if isinstance(stopped, str) and stopped else LOST_REPORT if stopped else None
    return {"started": float(record["started"]), "report": report, "unseen": bool(report) and record.get("unseen") is True,
            "checkpoint": record.get("checkpoint") is True, "continuation": binding if isinstance(binding, str) else None}


def task_state(key: str, now: float, restart: bool) -> dict:
    """The record stored for ``key``; a new task started at ``now``, stored, when there is none or ``restart``."""
    record = None if restart else _parsed(_read(_state_dir(), _name(key)).get(key))
    if record is None:
        record = dict(EMPTY, started=now)
        store(key, record)
    return record
