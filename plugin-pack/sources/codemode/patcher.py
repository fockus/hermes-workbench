"""The core tool-list hook (spec Task 12; REQ-024, REQ-036): ``apply`` puts ``patch/tool-list-hook.patch``
into a Hermes checkout, ``hook_present`` is the start check and ``register_tool_filter`` hands a plugin's
per-session filter to the patched ``model_tools``.

The patch is applied by exact text: every hunk's old lines must occur exactly once in its file, so a
Hermes version the patch was not made for is refused before any file is written. Only the files the patch
names are read. The original bytes of a patched file stay next to it with ``BACKUP_SUFFIX``.
"""

from __future__ import annotations

import functools
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import seams

PATCH = Path(__file__).with_name("patch") / "tool-list-hook.patch"
BACKUP_SUFFIX = ".codemode-orig"
_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


class PatchRefused(RuntimeError):
    """The files under the root are not the Hermes version the patch was made for; nothing was written."""


@dataclass(frozen=True)
class Hunk:
    old: str
    new: str


def parse(text: str) -> dict[str, list[Hunk]]:
    """{relative path: its hunks} of a unified diff whose new-file headers are ``+++ b/<path>``."""
    files: dict[str, list[Hunk]] = {}
    lines = text.splitlines(keepends=True)
    i, current = 0, None
    while i < len(lines):
        line = lines[i]
        i += 1
        if line.startswith("+++ b/"):
            current = files.setdefault(line[6:].strip(), [])
            continue
        found = _HUNK.match(line)
        if not found or current is None:
            continue
        old_left, new_left = int(found.group(1) or 1), int(found.group(2) or 1)
        old, new = [], []
        while (old_left or new_left) and i < len(lines):
            body, i = lines[i], i + 1
            tag, rest = body[:1], body[1:]
            if tag in (" ", "-"):
                old.append(rest)
                old_left -= 1
            if tag in (" ", "+"):
                new.append(rest)
                new_left -= 1
        current.append(Hunk("".join(old), "".join(new)))
    return files


def _patched(rel: str, text: str, hunks: list[Hunk]) -> str | None:
    """The patched text, or None when the file already carries the patch."""
    if all(text.count(h.new) == 1 for h in hunks):
        return None
    for number, hunk in enumerate(hunks, 1):
        if text.count(hunk.old) != 1:
            raise PatchRefused(
                "refused: %s is not the Hermes version the tool-list hook patch was made for "
                "(hunk %d does not match); no file was changed" % (rel, number)
            )
        text = text.replace(hunk.old, hunk.new, 1)
    return text


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + BACKUP_SUFFIX)


def _write(path: Path, data: bytes) -> None:
    """Atomic replace that keeps the file's mode."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.chmod(tmp, path.stat().st_mode & 0o7777 if path.exists() else 0o644)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


_CALL_IDS = re.compile(r"^class _CallIds\b[^\n]*\n((?:[ \t]+[^\n]*\n|\n)*)", re.M)


def _check_call_ids(rel: str, text: str) -> None:
    """The bridge hunk reads ``ids.session_id``, a field no hunk's text covers: a Hermes whose
    ``_CallIds`` lost it would patch cleanly and hand the bridge an empty catalog in every session."""
    body = _CALL_IDS.search(text)
    if body is None or not re.search(r"^[ \t]+session_id\s*:", body.group(1), re.M):
        raise PatchRefused(
            "refused: %s is not the Hermes version the tool-list hook patch was made for "
            "(class _CallIds declares no session_id field); no file was changed" % rel
        )


def apply(root: Path | str, patch: Path = PATCH) -> str:
    """Patch the Hermes files under ``root``: idempotent, with a backup, refused on a version mismatch."""
    root = Path(root)
    plan: list[tuple[Path, bytes, bytes]] = []
    for rel, hunks in parse(patch.read_text(encoding="utf-8")).items():
        path = root / rel
        original = path.read_bytes()
        if rel == "model_tools.py":
            _check_call_ids(rel, original.decode("utf-8"))
        new = _patched(rel, original.decode("utf-8"), hunks)
        if new is not None:
            plan.append((path, original, new.encode("utf-8")))
    if not plan:
        return "the tool-list hook is already applied under %s; nothing changed" % root
    for path, original, patched in plan:
        _write(backup_path(path), original)
        _write(path, patched)
    return "applied the tool-list hook to %s (originals kept as *%s)" % (
        ", ".join(str(path.relative_to(root)) for path, _, _ in plan),
        BACKUP_SUFFIX,
    )


def checkout() -> Path:
    """The running Hermes checkout (the folder of ``model_tools``)."""
    import model_tools

    return Path(model_tools.__file__).resolve().parent


def hook_present() -> bool:
    """The start check: does the running Hermes carry the tool-list hook? Never imports model_tools: a
    plugin's register() runs inside its import, and importing it there waits on its import lock past
    the load deadline; a module not imported yet or half-imported is read from its source (seams)."""
    return seams._parameters("model_tools", "register_tool_filter") is not None and (
        "session_id" in (seams._parameters("model_tools", "get_tool_definitions") or [])
    )


def _hook(name: str) -> Callable | None:
    # the patch defines the registry before plugin discovery, so it is there in a half-imported module
    found = getattr(sys.modules.get("model_tools"), name, None)
    return found if callable(found) else None


def register_tool_filter(fn: Callable[[str, list], list], owner: Any = None) -> bool:
    """Hand ``fn(session_id, tools) -> tools`` to the hook; False when the running Hermes has no hook.
    ``owner.on_unload`` (a PluginContext), when given, removes the filter again."""
    if not callable(fn):
        raise TypeError("a tool filter must be callable as fn(session_id, tools)")
    register = _hook("register_tool_filter") if hook_present() else None
    if register is None:
        return False
    if owner is not None:
        owner.on_unload(functools.partial(unregister_tool_filter, fn))
    register(fn)
    return True


def unregister_tool_filter(fn: Callable[[str, list], list]) -> None:
    """Remove ``fn`` from the hook, with every session's recorded answers; nothing without a hook."""
    unregister = _hook("unregister_tool_filter")
    if unregister is not None:
        unregister(fn)
