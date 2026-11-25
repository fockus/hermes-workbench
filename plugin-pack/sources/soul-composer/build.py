"""Build: each recipe assembled in order into its bot's SOUL.md under a body-hash header (Task 4).

A built file is ``<!-- soul-composer: build <sha256 of the body> -->`` then the body; the memory-bank and
subagents plugins skip their own SOUL sync when they see that first line. The file is rewritten only when the
body changes (NFR-001); a file whose body no longer matches its stored hash, or that has no header, was written
by hand and is left alone with a warning unless ``force``, which saves a backup first (REQ-003). Writes take
``fcntl.flock`` on ``<SOUL.md>.lock`` (the providers' writer contract, bounded wait), decide on the bytes read
under it, keep one backup ``SOUL.md.bak`` of the last hand-edited file that ``force`` replaced and publish a 0600
temp file (NFR-003) by the providers' atomic name exchange (macOS ``renamex_np(RENAME_SWAP)``, Linux
``renameat2(RENAME_EXCHANGE)``), never ``os.replace``: Hermes' SOUL editor saves without the lock, and a save that
lands between our read and our publish is swapped straight back (the displaced copy stays at the temp name, with a
warning). A new SOUL.md is published by a hard link, which never replaces a file created meanwhile; where no
exchange exists the bot is skipped. A missing brick skips the bot and keeps its file (REQ-014); a file (header included)
over 16000 characters warns with every brick's size (REQ-016).

``provider:<plugin>`` and ``team`` bricks come from the caller's ``resolve(name, bot)``: text, ``None``/empty to
omit the brick, or ``MissingBrick`` to skip the bot. Providers and the Team block are Tasks 5/6.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import bricks
from .bricks import MissingBrick, Recipe, Store

LIMIT = 16000  # characters: 80 % of Hermes' 20 000 SOUL truncation floor (SPIKE.md § 4)
LOCK_WAIT, LOCK_STEP = 10.0, 0.05  # seconds
AT_FDCWD, RENAME_EXCHANGE, RENAME_SWAP = -100, 2, 2  # linux/fcntl.h, linux/fs.h; macOS sys/stdio.h
NO_EXCHANGE = {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}
HEADER = re.compile(r"(?:﻿)?[ \t]*<!-- soul-composer: build ([0-9a-f]+) -->[ \t]*")

Resolver = Callable[[str, str], str | None]


@dataclass(frozen=True)
class BuildResult:
    bot: str
    status: Literal["written", "unchanged", "hand_edited", "skipped", "refused"]
    size: int
    warnings: tuple[str, ...]


def unresolved(name: str, bot: str) -> str | None:
    """The resolver of a build without providers or a Team block: every such brick is missing."""
    raise MissingBrick(name)


def digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def split(data: str) -> tuple[str | None, str]:
    """(stored hash, body) of a SOUL.md; no hash when the first line is not the build header."""
    first, _, rest = data.partition("\n")
    match = HEADER.fullmatch(first.rstrip("\r"))
    return (match.group(1), rest) if match else (None, data)


def home_of(hermes_root: Path, bot: str) -> Path:
    return hermes_root if bot == "default" else hermes_root / "profiles" / bot


def assemble(store: Store, recipe: Recipe, resolve: Resolver) -> tuple[str, list[tuple[str, int]]]:
    """The body and (brick, size) of every brick it holds; raises MissingBrick."""
    parts, sizes = [], []
    for name in recipe.bricks:
        if name == "team" or name.startswith("provider:"):
            text = (resolve(name, recipe.bot) or "").strip()
        else:
            text = store.own(name)
        if text:
            parts.append(text)
            sizes.append((name, len(text)))
    return "\n\n".join(parts) + "\n", sizes


def build(hermes_root: Path, resolve: Resolver = unresolved, *, force: bool = False) -> list[BuildResult]:
    """Builds every recipe of ``<hermes_root>/soul``; raises bricks.StoreError (nothing written) on a bad store."""
    store = bricks.load(hermes_root / "soul")
    return [build_one(store, recipe, home_of(hermes_root, recipe.bot), resolve, force) for recipe in store.recipes]


def build_one(store: Store, recipe: Recipe, home: Path, resolve: Resolver, force: bool) -> BuildResult:
    bot = recipe.bot
    if not _is_profile(home):
        return BuildResult(bot, "skipped", 0, ("soul-composer: %s has no profile at %s; skipped" % (bot, home),))
    try:
        body, sizes = assemble(store, recipe, resolve)
    except MissingBrick as exc:
        return BuildResult(bot, "skipped", 0, ("soul-composer: %s: missing brick %s; its SOUL.md is kept" % (bot, exc),))
    size = len(body) + len(_header(body))  # REQ-016 measures the file as written, header included
    warnings = _size_warnings(bot, size, sizes)
    try:
        status, note = _publish(home / "SOUL.md", body, force, bot)
    except (OSError, UnicodeDecodeError) as exc:
        status, note = "skipped", "soul-composer: %s: SOUL.md not written (%s)" % (bot, exc)
    return BuildResult(bot, status, size, tuple(warnings + ([note] if note else [])))


def _is_profile(home: Path) -> bool:
    from hermes_constants import named_profile_has_identity

    return home.is_dir() and named_profile_has_identity(home)


def _header(body: str) -> str:
    return "<!-- soul-composer: build %s -->\n" % digest(body)


def _size_warnings(bot: str, size: int, sizes: list[tuple[str, int]]) -> list[str]:
    if size <= LIMIT:
        return []
    return ["soul-composer: %s SOUL.md is %d characters, over %d; it may be truncated" % (bot, size, LIMIT)] + [
        "soul-composer: %s: brick %s is %d characters" % (bot, name, size) for name, size in sizes]


def _publish(path: Path, body: str, force: bool, bot: str) -> tuple[str, str | None]:
    """(status, warning) of writing ``body`` to ``path`` under its lock."""
    if path.is_symlink():
        return "refused", "soul-composer: %s is a symbolic link; never written" % path
    with _locked(path):
        read = _read(path)
        old, before = read if read is not None else (None, None)
        if old is not None:
            stored, old_body = split(old)
            if stored is None or stored != digest(old_body):
                if not force:
                    return "hand_edited", ("soul-composer: %s: SOUL.md was edited by hand since its build; left as is "
                                           "(build --force overwrites it after a backup)" % bot)
                # only hand edits are backed up: a clean rebuild must not replace them in .bak with our own build
                _backup(path.with_name(path.name + ".bak"), old)
            elif stored == digest(body):
                return "unchanged", None
        _atomic_write(path, _header(body) + body, before)
    return "written", None


def _read(path: Path) -> tuple[str, os.stat_result] | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())  # before the read: a save landing after it then fails the identity check
        return fh.read().decode("utf-8"), st


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """``flock(LOCK_EX)`` on ``<SOUL.md>.lock`` (0600, never deleted), taken within LOCK_WAIT or OSError."""
    lock = path.with_name(path.name + ".lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError(errno.EWOULDBLOCK, "%s is held by another writer" % lock) from None
                time.sleep(LOCK_STEP)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


def _identity(st: os.stat_result) -> tuple[int, ...]:
    return st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size, st.st_mode, st.st_uid, st.st_gid


def _prepared(path: Path, data: str) -> Path:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".%s." % path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return Path(tmp)


def _backup(path: Path, data: str) -> None:
    """The one backup ``SOUL.md.bak``: nobody else writes it, so a plain rename publishes it."""
    tmp = _prepared(path, data)
    try:
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _atomic_write(path: Path, data: str, before: os.stat_result | None) -> None:
    """Publishes ``data`` at ``path`` in place of the file read under the lock (``before``; None: there was none).
    Raises OSError, leaving the other writer's file in place, when ``path`` changed meanwhile."""
    tmp = _prepared(path, data)
    keep = False
    try:
        if before is None:
            os.link(tmp, path)  # FileExistsError when a file appeared meanwhile: that one is kept
            return
        if not exchange(tmp, path):
            raise OSError(errno.ENOTSUP, "no atomic name exchange on this platform or file system", str(path))
        keep = True  # from here the temp name may hold another writer's file: never delete it unseen
        if _identity(os.lstat(tmp)) == _identity(before):
            keep = False
            return
        if not exchange(tmp, path):
            raise OSError(errno.EIO, "another writer's save could not be swapped back", str(tmp))
        raise OSError(errno.EAGAIN, "changed by another writer meanwhile; that save is kept, the build is at %s"
                      % tmp, str(path))
    finally:
        if not keep:
            tmp.unlink(missing_ok=True)


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
    except (OSError, AttributeError):
        return False
    if done == 0:
        return True
    code = ctypes.get_errno()
    if code in NO_EXCHANGE:
        return False
    raise OSError(code, os.strerror(code), str(b))
