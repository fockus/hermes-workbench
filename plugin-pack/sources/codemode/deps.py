"""Pinned pydantic-monty in ``codemode/.deps/cp<XY>``, one folder per interpreter (D-02, D-19).

The client wheels are CPython-specific, so every interpreter that runs code-mode gets its own
``pip --target`` folder; the Hermes venv is never touched. Installing is an explicit step through
the managed installer (``install_command``: this file run as a script, which ``scripts/codemode-
deps.sh`` also runs); this module checks, activates and, on request, installs.

Ready means more than the files being there: ``status(deep=True)`` imports the client from the
folder in an isolated interpreter and has its worker answer a script (``_handshake``); doctor and
the installer judge by it. The installer stages, checks the staged folder the same way, then
replaces the folder with it and asks for a Hermes restart: the user runs it, and a running Hermes
keeps the client it loaded. ``activate()`` imports the client from the checked folder and keeps it:
``client()`` is that module for the executor and the SDK, whatever the search path says later.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import importlib
import importlib.metadata
import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Iterator

PINNED = "1.6.0"
PACKAGE = "pydantic-monty"
PARTS = ("pydantic-monty-client", "pydantic-monty-runtime")  # what the metapackage pins to PINNED
DEPS_ROOT = Path(__file__).resolve().parent / ".deps"
LOCK = Path(__file__).resolve().parent / "requirements.lock"
HANDSHAKE_SECONDS = 30
_HANDSHAKE = r"""
import sys
sys.path.insert(0, sys.argv[1])
import pydantic_monty as pm
with pm.Monty(binary_path=sys.argv[2], min_processes=1, max_processes=1, checkout_timeout=10) as pool:
    with pool.checkout() as session:
        print(pm.__file__)
        print(session.feed_run("40 + 2"))
"""
_ACTIVE: list[tuple[Path, ModuleType]] = []  # the folder activate() approved last, and its client


@dataclass(frozen=True)
class DepsStatus:
    ok: bool
    folder: Path
    binary: Path
    version: str | None
    problem: str | None
    install_command: str


def folder_for(version_info: tuple = tuple(sys.version_info[:2]), root: Path | None = None) -> Path:
    """The deps folder of an interpreter version, under ``DEPS_ROOT`` read at call time."""
    return (DEPS_ROOT if root is None else root) / ("cp%d%d" % tuple(version_info[:2]))


def _pip_args(folder: Path) -> list[str]:
    return [sys.executable, "-m", "pip", "install", "--require-hashes", "--no-deps", "--only-binary", ":all:",
            "--target", str(folder), "-r", str(LOCK)]


def install_command(_folder: Path | None = None) -> str:
    """The managed installer for the running interpreter (lock, staging, checks), never raw pip."""
    return shlex.join([sys.executable, "-I", str(Path(__file__).resolve()), "install"])


def _dist(folder: Path, name: str) -> importlib.metadata.Distribution | None:
    if not folder.is_dir():
        return None
    for dist in importlib.metadata.distributions(path=[str(folder)]):
        if (dist.metadata["Name"] or "").lower().replace("_", "-") == name:
            return dist
    return None


def _part_problem(folder: Path) -> str | None:
    for name in PARTS:
        dist = _dist(folder, name)
        if dist is None or dist.version != PINNED:
            found = "is missing" if dist is None else "%s is installed" % dist.version
            return "%s %s in %s, code-mode needs %s" % (name, found, folder, PINNED)
    return None


def _artifact_problem(folder: Path, binary: Path) -> str | None:
    """The files Monty runs on: an executable worker and the client package with its native code."""
    if not os.access(binary, os.X_OK):
        return "the monty worker binary is not executable: %s" % binary
    dist = _dist(folder, PARTS[0])
    files = [f for f in (dist.files or []) if not str(f).startswith("..")] if dist else []
    missing = sorted(str(f) for f in files if not (folder / f).is_file())
    if missing:
        return "%s is incomplete in %s: %s is missing" % (PARTS[0], folder, missing[0])
    if not any(f.name.startswith("_monty.") and f.suffix in (".so", ".pyd") for f in files):
        return "%s in %s has no native pydantic_monty/_monty extension" % (PARTS[0], folder)
    return None


def _stopwrap() -> ModuleType:
    """codemode.stopwrap, also when this file runs as a script (``install_command``, no package)."""
    if __package__:
        return importlib.import_module(".stopwrap", __package__)
    spec = importlib.util.spec_from_file_location("codemode_stopwrap", Path(__file__).with_name("stopwrap.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _handshake(folder: Path, binary: Path) -> str | None:
    """Why the client does not load from ``folder`` in an isolated interpreter or its worker does
    not answer a script; None when both work. The worker starts through the same stopwrap
    launcher the executor gives Monty, so a temp folder that refuses it fails here too."""
    try:
        launcher = _stopwrap().launcher(str(binary))
    except OSError as exc:
        return "the code-mode worker launcher cannot be created: %s" % exc
    try:
        done = subprocess.run([sys.executable, "-I", "-c", _HANDSHAKE, str(folder), launcher],
                              capture_output=True, text=True, timeout=HANDSHAKE_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "the Monty handshake did not run: %s" % exc
    finally:
        shutil.rmtree(os.path.dirname(launcher), ignore_errors=True)
    lines = done.stdout.splitlines()  # a folder path may hold spaces
    if done.returncode != 0 or len(lines) != 2 or lines[1] != "42":
        tail = (done.stderr.strip().splitlines() or ["no output"])[-1]
        return "the Monty handshake failed in %s: %s" % (folder, tail[:300])
    if not Path(lines[0]).resolve().is_relative_to(folder.resolve()):
        return "the Monty handshake loaded pydantic_monty from %s, not from %s" % (lines[0], folder)
    return None


def _check(folder: Path, deep: bool = False) -> DepsStatus:
    binary = folder / "bin" / "monty"
    dist = _dist(folder, PACKAGE)
    version = dist.version if dist else None
    problem: str | None
    if version is None:
        problem = "%s %s is missing in %s" % (PACKAGE, PINNED, folder)
    elif version != PINNED:
        problem = "%s %s is installed in %s, code-mode needs %s" % (PACKAGE, version, folder, PINNED)
    elif not binary.is_file():
        problem = "the monty worker binary is missing: %s" % binary
    else:
        problem = _part_problem(folder) or _artifact_problem(folder, binary)
        if problem is None and deep:
            problem = _handshake(folder, binary)
    return DepsStatus(problem is None, folder, binary, version, problem, install_command(folder))


@contextlib.contextmanager
def _installer_lock(target: Path) -> Iterator[None]:
    """One installer at a time; a second one finds the first one's result."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target.with_name(target.name + ".lock"), "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def status(version_info: tuple = tuple(sys.version_info[:2]), deep: bool = False) -> DepsStatus:
    return _check(folder_for(version_info), deep)


def activate(deep: bool = False) -> DepsStatus:
    """Check the running interpreter's folder and, when it is complete, append it to ``sys.path``.

    Appended, not prepended: packages the host already has (typing_extensions) keep their versions.
    The client that ``import pydantic_monty`` actually resolves (already loaded, or found first on
    sys.path) must be the pinned one in that folder; another origin is refused and named."""
    found = status(deep=deep)
    if not found.ok:
        return found
    if str(found.folder) not in sys.path:
        sys.path.append(str(found.folder))
    try:  # imported now, from the folder just checked (or the copy already loaded, checked the same way)
        module = importlib.import_module("pydantic_monty")
    except ImportError as exc:
        return dataclasses.replace(found, ok=False, problem="pydantic_monty does not import: %s" % exc)
    origin = Path(getattr(module, "__file__", None) or "?").resolve()
    if not origin.is_relative_to(found.folder.resolve()):
        problem = "pydantic_monty is imported from %s, not from the pinned installation %s" % (origin, found.folder)
        return dataclasses.replace(found, ok=False, problem=problem)
    _ACTIVE[:] = [(found.folder, module)]
    return found


def client() -> ModuleType:
    """The pydantic_monty module the executor and the SDK use: the one activate() imported from
    the folder it checked; ImportError when no folder is ready."""
    if not _ACTIVE:
        found = activate()
        if not found.ok:
            raise ImportError(found.problem)
    return _ACTIVE[0][1]


def install() -> DepsStatus:
    """Install the locked wheels for the running interpreter, one installer at a time.

    The installer stages into its own folder; the staged installation is checked like an installed
    one (handshake included) before it replaces the folder, which a failed or incomplete install
    never touches."""
    target = folder_for()
    with _installer_lock(target):
        current = status(deep=True)
        if current.ok:
            return current  # ready, or another installer finished first
        staging = Path(tempfile.mkdtemp(prefix=target.name + ".staging-", dir=target.parent))
        try:
            subprocess.run(_pip_args(staging), check=True)
            staged = _check(staging, deep=True)
            if not staged.ok:
                problem = "the new installation was not published (%s); %s is unchanged" % (staged.problem, target)
                return dataclasses.replace(status(), ok=False, problem=problem)
            shutil.rmtree(target, ignore_errors=True)
            staging.rename(target)  # interrupted before this: the next install starts over
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return status(deep=True)


def main(argv: list[str]) -> int:
    try:
        found = install() if argv[:1] == ["install"] else status(deep=True)
    except subprocess.CalledProcessError as exc:
        print("pip failed with exit %d; nothing was installed" % exc.returncode)
        return 1
    print("%s: %s" % (PACKAGE, "%s in %s" % (found.version, found.folder) if found.ok else found.problem))
    if found.ok and argv[:1] == ["install"]:
        print("restart Hermes: a running Hermes keeps the pydantic-monty it loaded")
    if not found.ok:
        print("install: %s" % found.install_command)
    return 0 if found.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
