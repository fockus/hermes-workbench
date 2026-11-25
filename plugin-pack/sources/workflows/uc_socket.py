"""The uc socket (REQ-033, REQ-034): a same-user AF_UNIX transport into the workflow engine of this process.

Named uc_socket, not socket, so the package never shadows the stdlib ``socket``. Transport only: what a
request does is the injected ``handle(request) -> dict`` (``workflows.controls.Controls.handle``); WE-WIRE
starts ``serve`` at session start and closes it on unload.

``default_path(home=None) -> Path``: ``$HERMES_HOME/workflows/uc.sock`` (``store.runs_root().parent``); a path
longer than MAX_PATH bytes raises ValueError (AF_UNIX caps it at 104 bytes on macOS, 108 on Linux).
``serve(path, handle, owner=None) -> Server``: parent folder 0700; a stale socket file (connect refused) is
replaced, a live one raises RuntimeError "already served"; bound under umask 0o177, then chmod 0600; one
daemon thread per connection, run in a copy of the context ``serve`` was called in (the active profile's
HERMES_HOME lives in a ContextVar, which a new thread does not inherit). Each connection is checked first: the peer uid (``peer_uid``) must equal
``owner()`` (default ``os.getuid()``, read at check time); a foreign or unknown uid gets
``{"ok": false, "error": "refused: ..."}`` and is closed before any read. Then one JSON object line (at most
MAX_LINE bytes) -> ``handle`` -> one JSON line reply; a malformed line or a raising ``handle`` answers
``{"ok": false, "error": ...}``. ``Server.close()`` stops accepting, joins briefly and unlinks the socket.
``peer_uid(conn) -> int | None``: Linux SO_PEERCRED, macOS LOCAL_PEERCRED; anything else is None (refused).
``ask(path, request, timeout=20.0) -> dict``: the client (the CLI reaching a run driven by another process).
"""

from __future__ import annotations

import contextvars
import json
import os
import socket
import stat
import struct
import sys
import threading
from pathlib import Path
from typing import Callable

from .store import runs_root

MAX_PATH, MAX_LINE, ERROR_CHARS = 100, 1 << 20, 500
READ_TIMEOUT, ACCEPT_POLL, JOIN_WAIT = 30.0, 0.2, 1.0
SOL_LOCAL, LOCAL_PEERCRED, XUCRED_SIZE = 0, 1, 76  # <sys/un.h>, <sys/ucred.h>: uid_t cr_uid at offset 4


def default_path(home: str | os.PathLike | None = None) -> Path:
    path = (Path(home) / "workflows" if home is not None else runs_root().parent) / "uc.sock"
    _check_length(path)
    return path


def peer_uid(conn: socket.socket) -> int | None:
    try:
        if sys.platform.startswith("linux"):
            _, uid, _ = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            return uid
        if sys.platform == "darwin":
            return struct.unpack_from("I", conn.getsockopt(SOL_LOCAL, LOCAL_PEERCRED, XUCRED_SIZE), 4)[0]
    except (OSError, struct.error, AttributeError):
        return None
    return None


class Server:
    def __init__(self, listener: socket.socket, path: Path, handle: Callable[[dict], dict],
                 owner: Callable[[], int]):
        self.path, self._listener, self._handle, self._owner = path, listener, handle, owner
        self._inode = path.lstat().st_ino
        self._context = contextvars.copy_context()  # the profile (HERMES_HOME) of the session that serves
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._accept, name="workflows-uc-socket", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._closed.set()
        self._thread.join(JOIN_WAIT)
        self._listener.close()
        try:
            if self.path.lstat().st_ino == self._inode:  # never unlink a socket another server bound since
                self.path.unlink()
        except OSError:
            pass

    def _accept(self) -> None:
        while not self._closed.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._context.copy().run, args=(self._serve_one, conn), daemon=True).start()

    def _serve_one(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(READ_TIMEOUT)
            uid = peer_uid(conn)
            try:
                expected = self._owner()
            except Exception:  # noqa: BLE001 - an owner we cannot read refuses everyone
                expected = None
            if uid is None or expected is None or uid != expected:
                _reply(conn, {"ok": False, "error": "refused: the client is not the user who owns this socket"})
                return
            line = _read_line(conn)
            if line is None:
                _reply(conn, {"ok": False, "error": "refused: the request line is longer than 1 MiB"})
                return
            _reply(conn, self._answer(line))

    def _answer(self, line: bytes) -> dict:
        try:
            request = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            return {"ok": False, "error": "malformed request: %s" % _clip(str(exc))}
        if not isinstance(request, dict):
            return {"ok": False, "error": "malformed request: not a JSON object"}
        try:
            reply = self._handle(request)
        except Exception as exc:  # noqa: BLE001 - a failing request fails its reply, not the server
            return {"ok": False, "error": _clip("%s: %s" % (type(exc).__name__, exc))}
        return reply if isinstance(reply, dict) else {"ok": False, "error": "the handler returned no object"}


def serve(path: str | os.PathLike, handle: Callable[[dict], dict], owner: Callable[[], int] | None = None) -> Server:
    path = Path(path)
    _check_length(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if os.path.lexists(path):
        _clear_stale(path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        previous = os.umask(0o177)  # ponytail: process-wide umask for the bind; ceiling: a file another thread
        try:                        # creates in that instant is 0600 too. Upgrade: bind in a 0700 temp dir + rename.
            listener.bind(str(path))
        finally:
            os.umask(previous)
        os.chmod(path, 0o600)
        listener.listen(16)
        listener.settimeout(ACCEPT_POLL)
    except BaseException:
        listener.close()
        raise
    return Server(listener, path, handle, owner or (lambda: os.getuid()))


def ask(path: str | os.PathLike, request: dict, timeout: float = 20.0) -> dict:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(path))
            try:
                client.sendall((json.dumps(request, default=str) + "\n").encode("utf-8"))
            except OSError:
                pass  # a refusing server may close before reading; its reply is still there
            line = _read_line(client)
    except OSError as exc:
        return {"ok": False, "error": "no workflow socket at %s: %s" % (path, exc)}
    try:
        reply = json.loads((line or b"").decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        reply = None
    return reply if isinstance(reply, dict) else {"ok": False, "error": "the socket sent no JSON reply"}


def _check_length(path: Path) -> None:
    if len(os.fsencode(str(path))) > MAX_PATH:
        raise ValueError("socket path %s is longer than %d bytes (the AF_UNIX limit, 104 on macOS); "
                         "use a shorter HERMES_HOME" % (path, MAX_PATH))


def _clear_stale(path: Path) -> None:
    if not stat.S_ISSOCK(path.lstat().st_mode):
        raise RuntimeError("%s exists and is not a socket; not replacing it" % path)
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(2)
    try:
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError):
        path.unlink(missing_ok=True)
        return
    finally:
        probe.close()
    raise RuntimeError("%s is already served by a live process" % path)


def _read_line(conn: socket.socket) -> bytes | None:
    """One line without its newline (EOF ends it too); None when it passes MAX_LINE bytes."""
    data = b""
    while b"\n" not in data:
        if len(data) > MAX_LINE:
            return None
        try:
            chunk = conn.recv(65536)
        except OSError:
            break
        if not chunk:
            break
        data += chunk
    line = data.split(b"\n", 1)[0]
    return None if len(line) > MAX_LINE else line


def _reply(conn: socket.socket, reply: dict) -> None:
    try:
        conn.sendall((json.dumps(reply, default=str) + "\n").encode("utf-8"))
        conn.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def _clip(text: str) -> str:
    return text if len(text) <= ERROR_CHARS else text[:ERROR_CHARS - 1] + "…"
