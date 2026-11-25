"""``hermes soul``: build, new, absorb, doctor and import (Tasks 7 and 8; SPIKE.md § 1, § 7).

``setup(parser)`` mounts the subcommands and ``handle(args) -> int`` runs one; ``soulcomposer.register`` mounts both
lazily. Every command works on the main profile ``~/.hermes`` (``hermes_constants.get_default_hermes_root``), never
on the profile's own HERMES_HOME. The plugin context reaches the commands as ``args.soul_ctx`` (parser defaults set
by ``soulcomposer.register``; None when run without it), so provider bricks resolve through that home's plugins.

* ``build [--force]``: ``team.build``, one ``<bot>: written|unchanged|skipped — <why>`` line per bot; a refused
  store or team prints ``build refused, nothing written`` and exits 1, as does any skipped bot.
* ``new <name> --role <role> [--clone-from <profile>]``: ``hermes profile create <name> --clone-from <source>``
  (``hermes`` found on PATH, the only launch form); the source is the MAIN profile ("default") unless named, because
  stock ``--clone`` copies the ACTIVE profile (AGR-080). Then the member is added to team.yaml with
  ``recipes/<name>.yaml`` and only that bot is built.
* ``absorb <bot>``: the unified diff from the built body to the bot's SOUL.md body; writes nothing.
* ``doctor`` (cli_doctor.py) and ``import`` (importer.py).

``new`` and ``import`` keep ``<store>/.gitignore`` covering ``.cache/`` (composer-private records, never pushed).
Provider renders of ``absorb`` and ``import`` go to a scratch copy of ``.cache``, so neither writes the store.
"""

from __future__ import annotations

import contextlib
import difflib
import os
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import yaml
from hermes_constants import PROFILE_ID_RE

from . import bricks, team
from . import build as builder
from .bricks import MissingBrick, StoreError

CREATE_WAIT = 300  # seconds for `hermes profile create`
IGNORED = ".cache/"
SHOWN = {"written": "written", "unchanged": "unchanged"}  # every other status is a skip
WHY = {"written": "SOUL.md written", "unchanged": "SOUL.md up to date"}


def setup(parser) -> None:
    sub = parser.add_subparsers(dest="soul_command", metavar="build|new|absorb|doctor|import")
    sub.add_parser("build", help="build this machine's SOUL.md files").add_argument(
        "--force", action="store_true", help="overwrite hand-edited SOUL.md files after a backup")
    new = sub.add_parser("new", help="create a team bot: profile cloned from the main one, team member, SOUL.md")
    new.add_argument("name")
    new.add_argument("--role", required=True)
    new.add_argument("--clone-from", default="default", metavar="PROFILE",
                     help="profile to clone (default: the main profile, whichever profile is active)")
    sub.add_parser("absorb", help="show a bot's hand edits as a diff to its build").add_argument("bot")
    sub.add_parser("doctor", help="report team, profile and SOUL.md problems; changes nothing")
    sub.add_parser("import", help="split ~/.hermes/SOUL.md into bricks and a recipe")


def handle(args) -> int:
    command = getattr(args, "soul_command", None)
    if command == "doctor":
        from .cli_doctor import run

        return run(hermes_root())
    if command == "import":
        from .importer import run

        return run(hermes_root(), _ctx(args))
    run = {"build": _build, "new": _new, "absorb": _absorb}.get(command)
    if run is None:
        print("usage: hermes soul build|new|absorb|doctor|import")
        return 2
    return run(args)


def _ctx(args):
    return getattr(args, "soul_ctx", None)


def hermes_root() -> Path:
    """The main profile ``~/.hermes``, whichever profile runs the command."""
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root()


def report(results: list[builder.BuildResult]) -> int:
    """One summary line per bot, then its other warnings; 1 when any bot was skipped."""
    if not results:
        print("soul-composer: nothing built")
    for r in results:
        notes = list(r.warnings)
        why = WHY[r.status] if r.status in SHOWN else (notes.pop() if notes else r.status.replace("_", " "))
        print("%s: %s — %s" % (r.bot, SHOWN.get(r.status, "skipped"), why))
        for note in notes:
            print(note)
    return 0 if all(r.status in SHOWN for r in results) else 1


def _build(args) -> int:
    try:
        out = team.build(hermes_root(), ctx=_ctx(args), force=args.force)
    except StoreError as exc:  # TeamError and UntrustedFolder included
        print("soul-composer: build refused, nothing written: %s" % exc)
        return 1
    return report(out.results)


def _new(args) -> int:
    name, role, root = args.name, args.role.strip(), hermes_root()
    if not PROFILE_ID_RE.match(name) or name == "default" or not role:
        print("soul-composer: new needs a profile name (a-z, 0-9, -, _) other than default and a role")
        return 1
    try:
        store = bricks.load(root / "soul")
        everyone = team.members(store)
    except StoreError as exc:
        print("soul-composer: new refused, nothing written: %s" % exc)
        return 1
    known, here = {m.profile for m in everyone}, team.this_host()
    away = next((m.host for m in everyone if m.profile == name and m.host != here), None)
    if away is not None:
        print("soul-composer: %s is a team member of host %s; nothing written" % (name, away))
        return 1
    others = sorted({r.bot for r in store.recipes} - {name}) if not store.team else []
    if others:  # a team.yaml holding only the new member would stop building these recipes
        print("soul-composer: new refused, nothing written: there is no team.yaml and every recipe is built; "
              "add %s to a team.yaml first" % ", ".join(others))
        return 1
    if name in known and builder.home_of(root, name).exists():
        print("soul-composer: %s is already a team member with a profile; nothing written" % name)
        return 1
    source = args.clone_from
    if source != "default" and not PROFILE_ID_RE.match(source) or not builder.home_of(root, source).is_dir():
        print("soul-composer: new refused, nothing written: there is no profile %r to clone" % source)
        return 1
    cmd = "hermes profile create %s --clone-from %s" % (name, source)
    try:
        done = subprocess.run(["hermes", "profile", "create", name, "--clone-from", source], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=CREATE_WAIT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print("soul-composer: `%s` failed (%s); nothing written" % (cmd, exc))
        return 1
    if done.returncode != 0:
        print("soul-composer: `%s` exited %d; nothing written: %s"
              % (cmd, done.returncode, (done.stderr or done.stdout).strip()[-500:]))
        return 1
    main = next((r.bricks for r in store.recipes if r.bot == "default"), ("team",))
    if name not in known:
        put_member(store.root, {"profile": name, "role": role, "zone": role, "host": here, "bricks": list(main)})
        # an existing member keeps its team.yaml bricks: a recipe file would override them
        recipe = store.root / "recipes" / ("%s.yaml" % name)
        if not recipe.exists():
            write_text(recipe, yaml.safe_dump({"bricks": list(main)}, sort_keys=False))
    store = bricks.load(store.root)
    built = next(r for r in store.recipes if r.bot == name)
    # the clone holds the main bot's SOUL.md: --force rebuilds it even when that one was edited by hand
    rc = report([builder.build_one(store, built, builder.home_of(root, name),
                                   team.resolver(store, ctx=_ctx(args)), True)])
    if rc == 0:
        ensure_gitignore(store.root)
    return rc


def _absorb(args) -> int:
    root, bot = hermes_root(), args.bot
    try:
        store = bricks.load(root / "soul")
        local = {m.profile for m in team.local_members(store)} if store.team else {r.bot for r in store.recipes}
    except StoreError as exc:
        print("soul-composer: absorb refused: %s" % exc)
        return 1
    recipe = next((r for r in store.recipes if r.bot == bot and bot in local), None)
    path = builder.home_of(root, bot) / "SOUL.md"
    if recipe is None or not path.is_file():
        print("soul-composer: %s is not a built team member of this machine; nothing to absorb" % bot)
        return 1
    try:
        with scratch_providers(store.root, _ctx(args)) as providers:
            built, _ = builder.assemble(store, recipe, team.resolver(store, providers=providers))
    except MissingBrick as exc:
        print("soul-composer: %s cannot be built (missing brick %s); nothing to absorb" % (bot, exc))
        return 1
    _, body = builder.split(path.read_text(encoding="utf-8"))
    diff = list(difflib.unified_diff(built.splitlines(), body.splitlines(), "build/%s" % bot, str(path),
                                     lineterm=""))
    print("\n".join(diff) if diff else "soul-composer: %s SOUL.md has no hand edits" % bot)
    return 0


@contextlib.contextmanager
def scratch_providers(store: Path, ctx=None) -> Iterator[builder.Resolver]:
    """A provider resolver over a scratch copy of ``<store>/.cache``: falls back like a build, writes no store file."""
    with tempfile.TemporaryDirectory(prefix="soul-composer-") as tmp:
        root = Path(tmp)
        cache = store / bricks.CACHE
        for path in sorted(cache.glob("*.md")) if cache.is_dir() and not cache.is_symlink() else []:
            try:
                text = bricks.read_private(path)
            except (OSError, UnicodeDecodeError, StoreError):
                continue  # an unusable cache is no fallback: the render decides alone
            if text is not None:
                bricks.write_private(bricks.cache_dir(root) / path.name, text)
        yield bricks.provider_resolver(root, ctx)


def write_text(path: Path, text: str) -> None:
    """``text`` at ``path`` (0644) through a fsynced temp file in the same folder and one rename."""
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".%s." % path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o644)
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def put_member(store: Path, member: dict) -> None:
    """``member`` in place of team.yaml's member of the same profile, else appended (team.yaml created when absent);
    the other members and keys keep their values."""
    path = store / "team.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
    data = data if isinstance(data, dict) else {}
    old = list(data.get("members") or [])
    same = [k for k, m in enumerate(old) if isinstance(m, dict) and m.get("profile") == member["profile"]]
    if same:
        old[same[0]] = member
    else:
        old.append(member)
    data["members"] = old
    write_text(path, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def ensure_gitignore(store: Path) -> None:
    """``<store>/.gitignore`` covers ``.cache/``: created 0644, or the line appended once to the user's file."""
    path = store / ".gitignore"
    if path.is_symlink():
        print("soul-composer: %s is a symbolic link; .cache/ not added" % path)
        return
    text = path.read_text(encoding="utf-8") if path.is_file() else None
    if text is None:
        write_text(path, IGNORED + "\n")
    elif IGNORED not in (line.strip() for line in text.splitlines()):
        mode = path.stat().st_mode & 0o777
        write_text(path, text + ("\n" if text and not text.endswith("\n") else "") + IGNORED + "\n")
        path.chmod(mode)

