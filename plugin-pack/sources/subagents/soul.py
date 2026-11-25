"""The Orchestrator block inside a hand-written ``$HERMES_HOME/SOUL.md``: locked, re-checked, atomic, byte-exact.

The writer contract every SOUL.md writer shares (soul-composer, the memory-bank plugin's
hermes/memory-bank/soul.py, this plugin):

* the block is rendered first, before any lock;
* ``fcntl.flock(LOCK_EX)`` on ``<SOUL.md>.lock`` (same folder; a regular file owned by this user, mode 0600)
  is held from the last read to the publication, taken with a bounded wait (``LOCK_NB`` retries for 10 s;
  a busy lock leaves the file to its holder and a later session start tries again); it is never deleted;
* under the lock the bytes are read again (``O_NOFOLLOW``, a regular-file check on the descriptor before it is
  wrapped) and left alone when they changed since the first read or when the first physical line is the
  soul-composer build header (a BOM and indentation allowed): soul-composer then owns the file;
* the new bytes go to a ``mkstemp`` file (0600) in the same folder that gets SOUL.md's owner, group and mode
  through its descriptor and is fsynced; an owner or group that cannot be carried over leaves the file alone;
* publication is an atomic name exchange (macOS ``renamex_np(RENAME_SWAP)``, Linux
  ``renameat2(RENAME_EXCHANGE)``), never ``os.replace``: Hermes' own SOUL editor saves with atomic_write_text
  and takes no lock, and a replace would destroy a save landing just before it. The exchange leaves the
  displaced file at the temp name; when its (device, inode, mtime_ns, size) is not what was read under the
  lock, it is exchanged straight back with a warning (the other save wins), and a displaced file that is
  neither the one read nor ours is never deleted. Where no exchange exists, nothing is published;
* exactly one ordered marker pair, each on its own line, is replaced; any other marker layout leaves the file
  untouched with a warning; every byte outside the block is kept (CRLF, a BOM; a marker right after a BOM
  starts a line); a file without markers gets the block appended; a None render removes the marked lines
  (orchestrator_mode off) and leaves a file without markers alone;
* an unreadable, non-UTF-8, symlinked, read-only (no write bit) or missing SOUL.md is never written.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import logging
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger("subagents.soul")

START, END = b"<!-- orchestrator:start -->", b"<!-- orchestrator:end -->"
BOM = b"\xef\xbb\xbf"
HEADER = re.compile(rb"(?:\xef\xbb\xbf)?[ \t]*<!-- soul-composer: build [0-9a-f]+ -->[ \t]*")
LOCK_WAIT, LOCK_STEP = 10.0, 0.05  # seconds
AT_FDCWD, RENAME_EXCHANGE, RENAME_SWAP = -100, 2, 2  # linux/fcntl.h, linux/fs.h; macOS sys/stdio.h
NO_EXCHANGE = {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}


class Busy(Exception):
    """The lock stayed held past the wait."""


def lock_path(soul: Path) -> Path:
    return soul.with_name(soul.name + ".lock")


def composed(data: bytes) -> bool:
    """True when the first physical line is soul-composer's build header."""
    return HEADER.fullmatch(data.split(b"\n", 1)[0].rstrip(b"\r")) is not None


def _line_start(data: bytes, at: int) -> bool:
    return at == 0 or data[at - 1:at] == b"\n" or (at == len(BOM) and data.startswith(BOM))


def _line_end(data: bytes, at: int) -> bool:
    return data[at:at + 1] in (b"", b"\n") or data[at:at + 2] == b"\r\n"


def with_block(data: bytes, block: str | None) -> bytes | None:
    """``data`` carrying ``block`` (without the block's lines when it is None), every byte outside it kept; None
    when the markers are ambiguous."""
    newline = data.find(b"\n")
    eol = b"\r\n" if newline > 0 and data[newline - 1:newline] == b"\r" else b"\n"
    new = b"" if block is None else block.encode("utf-8").replace(b"\n", eol)
    starts, ends = data.count(START), data.count(END)
    if starts == ends == 0 and block is None:
        return data
    if starts == ends == 0:
        gap = b"" if not data else eol if data.endswith(b"\n") else eol * 2
        return data + gap + new + eol
    start, end = data.find(START), data.find(END)
    if starts == ends == 1 and start < end and _line_start(data, start) and _line_end(data, start + len(START)) \
            and _line_start(data, end) and _line_end(data, end + len(END)):
        rest = end + len(END)
        if block is None:  # the block's lines go with their line ends
            rest += 2 if data[rest:rest + 2] == b"\r\n" else 1 if data[rest:rest + 1] == b"\n" else 0
        return data[:start] + new + data[rest:]
    return None


def _read(soul: Path) -> tuple[bytes, os.stat_result] | None:
    """(bytes, stat) of a regular UTF-8 SOUL.md; None (with a warning unless it is missing) otherwise."""
    try:
        fd = os.open(soul, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None  # no SOUL.md: Hermes keeps its built-in identity, which a block-only file would replace
    except OSError as exc:  # a symlink fails with ELOOP
        logger.warning("subagents: Orchestrator block skipped, %s unreadable: %s", soul, type(exc).__name__)
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            logger.warning("subagents: Orchestrator block skipped, %s is not a regular file", soul)
            return None
        with os.fdopen(fd, "rb") as file:
            fd = -1
            data = file.read()
    finally:
        if fd >= 0:
            os.close(fd)
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("subagents: Orchestrator block skipped, %s is not UTF-8", soul)
        return None
    return data, st


def _identity(st: os.stat_result) -> tuple[int, ...]:
    """What a concurrent change to SOUL.md can alter: its object, content stamp, size, mode and owner."""
    return st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size, st.st_mode, st.st_uid, st.st_gid


@contextmanager
def _locked(soul: Path, wait: float) -> Iterator[None]:
    fd = os.open(lock_path(soul), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o077:
            raise PermissionError(errno.EPERM, "the lock file is not a private regular file of this user",
                                  str(lock_path(soul)))
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Busy() from None
                time.sleep(LOCK_STEP)
        yield
    finally:
        os.close(fd)  # releases the lock


def _prepared(soul: Path, data: bytes, before: os.stat_result) -> Path | None:
    """A fsynced copy with SOUL.md's owner, group and mode, in its folder; None when those cannot be kept."""
    fd, name = tempfile.mkstemp(dir=soul.parent, prefix=".%s." % soul.name, suffix=".tmp")  # 0600
    tmp = Path(name)
    try:
        own = os.fstat(fd)
        if (own.st_uid, own.st_gid) != (before.st_uid, before.st_gid):
            os.fchown(fd, before.st_uid, before.st_gid)
        os.fchmod(fd, stat.S_IMODE(before.st_mode))  # after fchown, which may clear setuid/setgid bits
        with os.fdopen(fd, "wb") as file:
            fd = -1
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
    except OSError as exc:
        if fd >= 0:
            os.close(fd)
        tmp.unlink(missing_ok=True)
        logger.warning("subagents: Orchestrator block skipped, %s cannot be prepared: %s", soul, exc)
        return None
    return tmp


def exchange(a: Path, b: Path) -> bool:
    """Swaps the names ``a`` and ``b`` in one atomic step; False where this platform or file system cannot."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if sys.platform == "darwin":
            swap = libc.renamex_np
            swap.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            done = swap(os.fsencode(a), os.fsencode(b), RENAME_SWAP)
        elif sys.platform.startswith("linux"):
            swap = libc.renameat2
            swap.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            done = swap(AT_FDCWD, os.fsencode(a), AT_FDCWD, os.fsencode(b), RENAME_EXCHANGE)
        else:
            return False
    except (OSError, AttributeError):  # no such libc call here
        return False
    if done == 0:
        return True
    code = ctypes.get_errno()
    if code in NO_EXCHANGE:
        return False
    raise OSError(code, os.strerror(code), str(b))


def _publish(soul: Path, data: bytes, before: os.stat_result) -> bool:
    """Swaps a prepared copy in for SOUL.md as read under the lock (``before``); another writer's save wins."""
    if not stat.S_IMODE(before.st_mode) & 0o222 or not os.access(soul, os.W_OK):
        logger.warning("subagents: Orchestrator block skipped, %s is read-only", soul)
        return False
    tmp = _prepared(soul, data, before)
    if tmp is None:
        return False
    keep = False
    try:
        if not exchange(tmp, soul):
            logger.warning("subagents: Orchestrator block skipped, %s cannot be swapped atomically here", soul)
            return False
        keep = True  # from here the temp name may hold another writer's file: never delete it unseen
        if _identity(os.lstat(tmp)) == _identity(before):
            keep = False  # the displaced file is the one read under the lock: it is removed below
            return True
        if not exchange(tmp, soul):  # the first exchange worked, so this one fails only on a fault
            raise OSError(errno.EIO, "another writer's save could not be swapped back", str(tmp))
        # Never unlink after a swap-back: an editor writing in place (Hermes' TUI) may have changed the file now
        # at the temp name between the two exchanges, so it stays on disk and its path is reported.
        logger.warning("subagents: Orchestrator block undone, %s was changed by another writer meanwhile; the "
                       "displaced copy is kept at %s", soul, tmp)
        return False
    except OSError as exc:
        logger.warning("subagents: Orchestrator block skipped, %s cannot be swapped: %s%s", soul, exc,
                       "; the displaced copy is kept at %s" % tmp if keep else "")
        return False
    finally:
        if not keep:
            tmp.unlink(missing_ok=True)


def sync(soul: Path, render: Callable[[], str | None], wait: float = LOCK_WAIT) -> bool:
    """Keeps the block current in a hand-written SOUL.md (a None render removes it); True when it was rewritten.
    Never raises for a bad file, lock or render; a busy lock returns False and leaves the file to its holder."""
    first = _read(soul)
    if first is None or composed(first[0]):
        return False
    try:
        block = render()
    except (OSError, ValueError) as exc:
        logger.warning("subagents: Orchestrator block skipped, render failed: %s", exc)
        return False
    if block is None and START not in first[0] and END not in first[0]:
        return False  # nothing to remove: no lock (and no lock file) is taken
    try:
        with _locked(soul, wait):
            current = _read(soul)
            if current is None or current[0] != first[0] or composed(current[0]):
                logger.warning("subagents: Orchestrator block skipped, %s changed meanwhile", soul)
                return False
            updated = with_block(current[0], block)
            if updated is None:
                logger.warning("subagents: Orchestrator block skipped, %s has ambiguous orchestrator markers "
                               "(exactly one ordered start/end pair on their own lines is required)", soul)
                return False
            return updated != current[0] and _publish(soul, updated, current[1])
    except Busy:
        logger.warning("subagents: Orchestrator block skipped, %s is locked by another writer", lock_path(soul))
        return False
    except OSError as exc:
        logger.warning("subagents: Orchestrator block skipped, the lock %s is unusable: %s", lock_path(soul), exc)
        return False
