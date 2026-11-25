"""The brick store ``~/.hermes/soul``: own bricks, role bricks and recipes, read only when trusted (Task 4).

Layout (SPIKE.md § 7): ``bricks/<name>.md``, ``roles/<name>.md``, ``recipes/<profile>.yaml`` (``bricks:``) and
``team.yaml`` (``members:``, each with its ordered ``bricks``). A recipe brick name is ``<name>`` (``bricks/``,
else ``roles/``), ``provider:<plugin>`` or ``team``; the last two are resolved by the build's resolver
(Tasks 5/6), never here. Every folder and file read must belong to this user and be writable by nobody else
(REQ-020); anything else refuses the whole store before a byte of it is read.

Provider bricks (Task 5, SPIKE.md § 6): ``provider_resolver(root, ctx)`` resolves ``provider:<name>`` to the loaded
plugin module and calls its top-level ``render_soul_brick()``. When ctx has a plugin manager (``ctx._manager``), the
manager decides: its entry for ``<name>`` gives the module of that Hermes home, disabled when absent or not enabled.
Otherwise ``sys.modules["hermes_plugins.<slug>"]`` (slug: ``-`` → ``_``), or its ``__home_<digest>`` variant for a
second Hermes home, is used with ``ctx.has_plugin``; a provider is never imported by path and
its text is never copied into a brick file. subagents does not re-export ``render_soul_brick`` from its package yet
(SA2-WIRE follow-up), so ``<pkg>.orchestrator.render_soul_brick`` is used when the package has none. A disabled
provider (module not loaded, ``ctx.has_plugin(name)`` false, or a ``None``/empty render) omits its brick without
reading or touching its cache. A raising render falls back to ``<root>/.cache/<name>.md`` with a warning naming the
provider, and with no cache the bot is skipped (``MissingBrick``). Every successful render is cached atomically,
0600 in a 0700 ``.cache`` folder; each provider renders at most once per resolver.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml
from hermes_constants import PROFILE_ID_RE

FOLDERS = ("bricks", "roles", "recipes")
CACHE = ".cache"  # composer-private records under the store: provider renders, written descriptions

logger = logging.getLogger("soulcomposer")


class StoreError(Exception):
    """The store cannot be used: nothing is built."""


class UntrustedFolder(StoreError):
    """A store folder or file that is not this user's or is writable by others."""


class MissingBrick(Exception):
    """A recipe names a brick that cannot be had: that bot is skipped and keeps its SOUL.md (REQ-014)."""


@dataclass(frozen=True)
class Recipe:
    bot: str  # profile name; "default" is the main profile ~/.hermes
    bricks: tuple[str, ...]


@dataclass(frozen=True)
class Store:
    root: Path
    bricks: dict[str, str]
    roles: dict[str, str]
    recipes: tuple[Recipe, ...]
    members: tuple[dict, ...] = ()  # team.yaml ``members:`` as read; team.py validates and models them
    team: bool = False  # team.yaml exists: only its members are built, none when it lists none

    def own(self, name: str) -> str:
        """The text of own brick ``name`` (``bricks/`` before ``roles/``), stripped."""
        text = self.bricks.get(name, self.roles.get(name))
        if text is None:
            raise MissingBrick(name)
        return text


def _check(path: Path) -> None:
    st = path.stat()
    if st.st_uid != os.getuid() or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise UntrustedFolder("soul-composer: refused to read %s: not yours or writable by others" % path)


def _files(folder: Path, suffix: str) -> dict[str, Path]:
    return {p.stem: p for p in sorted(folder.glob("*" + suffix)) if p.is_file()} if folder.is_dir() else {}


def _trusted(root: Path) -> None:
    """Every folder and file the store is read from, checked before any is read."""
    paths = [root, root / "team.yaml"]
    for kind, suffix in zip(FOLDERS, (".md", ".md", ".yaml")):
        paths += [root / kind, *_files(root / kind, suffix).values()]
    for path in paths:
        if path.exists():
            _check(path)


def _yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise StoreError("soul-composer: cannot read %s: %s" % (path, exc)) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise StoreError("soul-composer: %s is not a mapping" % path)
    return data


def _recipe(bot, bricks, source: Path) -> Recipe:
    if not isinstance(bot, str) or not PROFILE_ID_RE.match(bot):
        raise StoreError("soul-composer: %s names an invalid profile %r" % (source, bot))
    if not isinstance(bricks, list) or not all(isinstance(b, str) and b for b in bricks):
        raise StoreError("soul-composer: %s: the bricks of %s are not a list of names" % (source, bot))
    return Recipe(bot, tuple(bricks))


def _members(root: Path) -> list[dict]:
    team = root / "team.yaml"
    members = _yaml(team).get("members") if team.is_file() else None
    members = [] if members is None else members
    if not isinstance(members, list) or not all(isinstance(m, dict) for m in members):
        raise StoreError("soul-composer: %s: members is not a list of mappings" % team)
    return members


def _recipes(root: Path, members: list[dict], files: dict[str, Path]) -> tuple[Recipe, ...]:
    """Members of team.yaml in their order, then recipe files of no member; a recipe file wins its bricks."""
    team = root / "team.yaml"
    out: dict[str, Recipe] = {}
    for member in members:
        bot = member.get("profile")
        if bot in files:
            out[bot] = _recipe(bot, _yaml(files[bot]).get("bricks"), files[bot])
        else:
            out[bot] = _recipe(bot, member.get("bricks"), team)
    for bot, path in files.items():
        if bot not in out:
            out[bot] = _recipe(bot, _yaml(path).get("bricks"), path)
    return tuple(out.values())


def load(root: Path) -> Store:
    """The store at ``root`` (``~/.hermes/soul``); raises UntrustedFolder or StoreError, reading nothing then."""
    if not root.is_dir():
        raise StoreError("soul-composer: no brick store at %s" % root)
    _trusted(root)
    texts = {}
    for kind in ("bricks", "roles"):
        try:
            texts[kind] = {name: p.read_text(encoding="utf-8").strip() for name, p in _files(root / kind, ".md").items()}
        except (OSError, UnicodeDecodeError) as exc:
            raise StoreError("soul-composer: cannot read %s: %s" % (root / kind, exc)) from exc
    members = _members(root)
    recipes = _recipes(root, members, _files(root / "recipes", ".yaml"))
    return Store(root, texts["bricks"], texts["roles"], recipes, tuple(members), (root / "team.yaml").is_file())


def cache_dir(root: Path) -> Path:
    """``<root>/.cache``, created 0700; raises UntrustedFolder when it is a link or not this user's."""
    path = root / CACHE
    if path.is_symlink():
        raise UntrustedFolder("soul-composer: %s is a symbolic link; refused" % path)
    path.mkdir(mode=0o700, exist_ok=True)
    if path.stat().st_uid != os.getuid():
        raise UntrustedFolder("soul-composer: %s is not yours; refused" % path)
    path.chmod(0o700)
    return path


def write_private(path: Path, text: str) -> None:
    """``text`` at ``path`` through a fsynced 0600 temp file in the same folder and one rename."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".%s." % path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_private(path: Path) -> str | None:
    """The text of a composer record under ``.cache``; None when it is missing. Raises UntrustedFolder when the
    folder or file is not this user's or writable by others, OSError/UnicodeDecodeError when unreadable."""
    try:
        _check(path.parent)
        _check(path)
    except FileNotFoundError:
        return None
    return path.read_text(encoding="utf-8")


def _managed(ctx) -> Mapping | None:
    """``ctx._manager._plugins`` of a real plugin context; None for ``ctx=None`` and managerless test contexts."""
    plugins = getattr(getattr(ctx, "_manager", None), "_plugins", None)
    return plugins if isinstance(plugins, Mapping) else None


def _provider_render(name: str, ctx=None) -> Callable[[], str | None] | None:
    """The loaded plugin's ``render_soul_brick``, or None when that plugin is not loaded or has none."""
    plugins = _managed(ctx)
    if plugins is not None:  # the context's own home decides (I-039): never another home's module
        entry = next((p for key, p in plugins.items()
                      if key == name or getattr(getattr(p, "manifest", None), "name", None) == name), None)
        module = getattr(entry, "module", None) if getattr(entry, "enabled", False) else None
    else:
        bare = "hermes_plugins." + name.replace("-", "_")
        module = sys.modules.get(bare) or next(
            (sys.modules[key] for key in sorted(sys.modules) if key.startswith(bare + "__home_")), None)
    if module is None:
        return None
    render = getattr(module, "render_soul_brick", None)
    if render is None:  # subagents keeps it in its orchestrator module until SA2-WIRE re-exports it
        render = getattr(getattr(module, "orchestrator", None), "render_soul_brick", None)
    return render if callable(render) else None


def provider_resolver(root: Path, ctx=None) -> Callable[[str, str], str | None]:
    """The build resolver for ``provider:<name>`` bricks of the store at ``root``; other names are MissingBrick."""
    rendered: dict[str, str | None | MissingBrick] = {}

    def resolve(brick: str, bot: str) -> str | None:
        kind, _, name = brick.partition(":")
        if kind != "provider" or not PROFILE_ID_RE.match(name):
            raise MissingBrick(brick)
        if name not in rendered:
            try:
                rendered[name] = _provide(root, name, ctx)
            except MissingBrick as exc:  # a failed render with no cache is not retried for the next bot
                rendered[name] = exc
        if isinstance(rendered[name], MissingBrick):
            raise rendered[name]
        return rendered[name]

    return resolve


def _provide(root: Path, name: str, ctx) -> str | None:
    render = _provider_render(name, ctx)
    if render is None or (ctx is not None and _managed(ctx) is None and not ctx.has_plugin(name)):
        return None  # disabled: the brick is left out and the cache is left as it is
    try:
        text = render()
    except Exception as exc:  # noqa: BLE001 - any failing render falls back to the last good one
        return _cached(root, name, exc)
    text = text.strip() if isinstance(text, str) else ""
    if not text:
        return None
    try:
        write_private(cache_dir(root) / ("%s.md" % name), text)
    except (OSError, StoreError) as exc:
        logger.warning("soul-composer: provider %s rendered, but its cache was not written: %s", name, exc)
    return text


def _cached(root: Path, name: str, exc: Exception) -> str:
    path = root / CACHE / ("%s.md" % name)
    try:
        text = (read_private(path) or "").strip()
    except (OSError, UnicodeDecodeError, StoreError) as err:
        logger.warning("soul-composer: provider %s: its cache %s is unusable: %s", name, path, err)
        text = ""
    if not text:
        raise MissingBrick("provider:%s (render failed: %s; no cached render)" % (name, type(exc).__name__)) from exc
    logger.warning("soul-composer: provider %s failed to render (%s: %s); using its cached render %s",
                   name, type(exc).__name__, exc, path)
    return text
