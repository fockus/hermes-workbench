"""``hermes soul import`` (Task 8, REQ-013): the main ``~/.hermes/SOUL.md`` split into bricks and a recipe.

The ``<!-- mb-rules:start/end -->`` block becomes ``provider:memory-bank``, the ``<!-- orchestrator:start/end -->``
block ``provider:subagents``, and the rest is cut at level 1 and 2 headings (outside code fences) into own bricks
``main-<n>``, numbered from 0 past every name the store already has. The recipe is assembled in memory with the
provider renders (a scratch cache, nothing written) and compared with the file up to whitespace: a difference prints
a unified diff and writes nothing, as does an unavailable provider. A match writes the bricks, ``recipes/default.yaml``
and team.yaml's ``default`` member, then builds SOUL.md through ``build.build_one`` with ``force``, which keeps the old
file as ``SOUL.md.bak``, and ensures ``<store>/.gitignore``. Every paragraph found twice is reported. A publish that
is refused, or a write that fails partway, rolls the store back (I-044): the bricks this import created are removed,
team.yaml and ``recipes/default.yaml`` get their old bytes back (or are removed), and only the folders this import
created are removed, when empty; no ``.gitignore`` is written then.
"""

from __future__ import annotations

import difflib
import os
import re
import tempfile
from pathlib import Path

import yaml

from . import bricks, cli, team
from . import build as builder
from .bricks import MissingBrick, Recipe, Store, StoreError

PROVIDERS = (("<!-- mb-rules:start -->", "<!-- mb-rules:end -->", "memory-bank"),
             ("<!-- orchestrator:start -->", "<!-- orchestrator:end -->", "subagents"))
HEADING = re.compile(r"#{1,2}\s")
NEW_MEMBER = {"role": "lead", "zone": "main"}  # team.yaml without a default member: the main bot leads


class Refused(Exception):
    """The SOUL.md cannot be imported as it is: nothing is written."""


def pieces(text: str) -> list[tuple[str | None, str]]:
    """(``provider:<name>`` or None for own text, text) of ``text`` in order, empty pieces left out."""
    out: list[tuple[str | None, str]] = []
    while True:
        found = [(text.find(start), start, end, name) for start, end, name in PROVIDERS if start in text]
        if not found:
            out += [(None, part) for part in _sections(text)]
            return out
        at, start, end, name = min(found)
        stop = text.find(end, at)
        if stop < 0:
            raise Refused("SOUL.md has %s without %s" % (start, end))
        stop += len(end)
        out += [(None, part) for part in _sections(text[:at])]
        out.append(("provider:%s" % name, text[at:stop]))
        text = text[stop:]


def _sections(text: str) -> list[str]:
    parts, current, fenced = [], [], False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        if not fenced and HEADING.match(line) and current:
            parts.append("\n".join(current))
            current = []
        current.append(line)
    parts.append("\n".join(current))
    return [part.strip() for part in parts if part.strip()]


def duplicates(text: str) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n[ \t]*\n", text) if p.strip()]
    return [p for p in dict.fromkeys(paragraphs) if paragraphs.count(p) > 1]


def run(root: Path, ctx=None) -> int:
    try:
        return _import(root, ctx)
    except (Refused, StoreError) as exc:
        print("soul-composer: import refused, nothing written: %s" % exc)
        return 1
    except OSError as exc:  # after the checks: a write failed partway
        print("soul-composer: import failed while writing (%s); check %s" % (exc, root / "soul"))
        return 1


def _import(root: Path, ctx=None) -> int:
    path, folder = root / "SOUL.md", root / "soul"
    try:
        original = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise Refused("cannot read %s (%s)" % (path, exc)) from exc
    if builder.split(original)[0] is not None:
        raise Refused("%s is already a soul-composer build" % path)
    store = bricks.load(folder) if folder.exists() else Store(folder, {}, {}, ())
    team.members(store)  # a team that would refuse the build refuses the import first
    others = sorted({r.bot for r in store.recipes} - {"default"}) if not store.team else []
    if others:  # a team.yaml holding only default would stop building these recipes
        raise Refused("there is no team.yaml and every recipe is built; add %s to a team.yaml first" % ", ".join(others))
    names, own, taken, n = [], {}, set(store.bricks) | set(store.roles), 0
    for kind, text in pieces(original):
        if kind is None:
            while "main-%d" % n in taken:
                n += 1
            kind = "main-%d" % n
            own[kind] = text
            taken.add(kind)
        names.append(kind)
    for paragraph in duplicates(original):
        print("soul-composer: duplicated paragraph (kept in each brick): %s" % paragraph.splitlines()[0][:80])
    recipe = Recipe("default", tuple(names))
    with cli.scratch_providers(folder, ctx) as providers:
        for name in (b for b in names if b.startswith("provider:")):
            try:
                text = providers(name, "default")
            except MissingBrick as exc:
                raise Refused("provider %s is unavailable (%s)" % (name[9:], exc)) from exc
            if not text:
                raise Refused("provider %s is unavailable (not loaded, or it rendered nothing)" % name[9:])
        body, _ = builder.assemble(Store(folder, {**store.bricks, **own}, store.roles, (recipe,)), recipe, providers)
        if "".join(body.split()) != "".join(original.split()):
            print("\n".join(difflib.unified_diff(original.splitlines(), body.splitlines(), str(path), "build/default",
                                                 lineterm="")))
            raise Refused("the build of the split pieces does not reproduce %s (diff above)" % path)
        made, saved, created = _before(folder), _saved(folder), []
        if len(saved) < 2:  # a link or a folder in their place would be replaced and never restored
            raise Refused("%s/team.yaml or recipes/default.yaml is not a regular file" % folder)
        try:
            _write(folder, own, names, created)
            store = bricks.load(folder)
            built = next(r for r in store.recipes if r.bot == "default")
            # the memoized scratch renders: the publish writes exactly the checked texts
            result = builder.build_one(store, built, root, team.resolver(store, providers=providers), True)
        except (OSError, StoreError) as exc:  # the store is written partway
            return _rolled_back(made, saved, created, str(exc))
        except KeyboardInterrupt:  # Ctrl-C in the SOUL.md.lock wait or mid-write
            _rolled_back(made, saved, created, "interrupted")
            raise
    try:
        rc = cli.report([result])
    finally:  # decided by the status, so a report that cannot be printed still rolls back
        if result.status not in cli.SHOWN:
            rc = _rolled_back(made, saved, created, result.status.replace("_", " "))
    if rc != 0:
        return rc
    cli.ensure_gitignore(folder)
    print("soul-composer: imported %s into %s; the old file is %s" % (path, ", ".join(names), path.name + ".bak"))
    return rc


def _before(folder: Path) -> list[Path]:
    """The store folders this import would create, in creation order."""
    return [sub for sub in (folder, folder / "bricks", folder / "recipes") if not sub.exists()]


def _saved(folder: Path) -> dict[Path, tuple[bytes, int] | None]:
    """The (bytes, mode) of the files the import overwrites, None when absent; a non-file is left out (import refused)."""
    out: dict[Path, tuple[bytes, int] | None] = {}
    for path in (folder / "team.yaml", folder / "recipes" / "default.yaml"):
        if not path.exists() and not path.is_symlink():
            out[path] = None
        elif path.is_file() and not path.is_symlink():
            out[path] = (path.read_bytes(), path.stat().st_mode & 0o777)
    return out


def _restore(path: Path, old: tuple[bytes, int] | None) -> None:
    if old is None:
        path.unlink(missing_ok=True)
        return
    cli.write_text(path, old[0].decode("utf-8"))
    path.chmod(old[1])


def _rolled_back(made: list[Path], saved: dict, created: list[Path], note: str) -> int:
    """Undoes the import's writes step by step; a failing step is named and the rest still runs. Never raises."""
    steps = [(p, p.unlink) for p in reversed(created)]
    steps += [(p, lambda p=p, old=old: _restore(p, old)) for p, old in saved.items()]
    steps += [(p, lambda p=p: p.exists() and p.rmdir()) for p in reversed(made)]
    for path, step in steps:
        try:
            step()
        except (OSError, ValueError):  # ValueError: undecodable old bytes; the folder is left for the user
            print("soul-composer: rollback incomplete, check %s" % path)
    print("soul-composer: import refused at publish, the store is rolled back: %s" % note)
    return 1


def _write(folder: Path, own: dict[str, str], names: list[str], created: list[Path]) -> None:
    """Writes the import into the store; every brick it creates is appended to ``created`` as it lands."""
    for sub in (folder, folder / "bricks", folder / "recipes"):
        if not sub.exists():
            sub.mkdir(mode=0o755)
            sub.chmod(0o755)
    for name, text in own.items():
        brick = folder / "bricks" / ("%s.md" % name)
        _create(brick, text + "\n")
        created.append(brick)
    cli.write_text(folder / "recipes" / "default.yaml", yaml.safe_dump({"bricks": names}, sort_keys=False))
    path = folder / "team.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
    old = next((m for m in (data or {}).get("members") or [] if m.get("profile") == "default"), None)
    member = {"profile": "default", **NEW_MEMBER, **(old or {})}
    cli.put_member(folder, {**member, "host": team.this_host(), "bricks": names})


def _create(path: Path, text: str) -> None:
    """A new 0644 file at ``path``; FileExistsError when the name was taken meanwhile (never overwritten)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".%s." % path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o644)
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.link(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
