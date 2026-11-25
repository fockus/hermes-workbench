"""``hermes soul doctor`` (Task 7, REQ-012): read-only; every problem once, on a line that starts with the profile
or host it concerns, then a count. Exit 1 only when a build would be refused (StoreError, TeamError).

Checks: profiles outside team.yaml; members of this machine without a profile; SOUL.md files edited by hand (header
digest ≠ body digest), not built, or over ``build.LIMIT``; hosts of remote members missing from config.yaml
``bot_peers``; plugins of the main profile (``plugins.enabled`` and ``plugins/`` names) missing in a bot (SPIKE.md
§ 1); hand-written profile.yaml descriptions (``team.mismatches``). Tools against a role is not checked yet.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from hermes_constants import PROFILE_ID_RE, named_profile_has_identity

from . import bricks, team
from . import build as builder
from .bricks import StoreError


def run(root: Path) -> int:
    try:
        store = bricks.load(root / "soul")
        everyone = team.members(store)
        local = team.local_members(store)
    except StoreError as exc:
        print("soul-composer doctor: a build would be refused: %s" % exc)
        return 1
    problems: list[str] = []
    if store.team:
        names = {m.profile for m in everyone}
        problems += ["%s: profile is not in team.yaml" % p for p in _profiles(root) if p not in names]
        problems += ["%s: team member has no profile here; create it with `hermes soul new %s --role %s`"
                     % (m.profile, m.profile, m.role) for m in local if not _is_profile(root, m.profile)]
        bots = [m.profile for m in local]
    else:
        problems.append("soul-composer doctor: no team.yaml; every recipe is built and team checks are skipped")
        bots = [r.bot for r in store.recipes]
    present = [b for b in bots if _is_profile(root, b)]
    for bot in present:
        problems += _soul(bot, builder.home_of(root, bot) / "SOUL.md")
    config = _config(root, problems)
    peers = config.get("bot_peers") if isinstance(config.get("bot_peers"), dict) else {}
    here = team.this_host()
    for host in sorted({m.host for m in everyone if m.host != here} - set(peers)):
        problems.append("%s: host of %s is missing from bot_peers in config.yaml"
                        % (host, ", ".join(m.profile for m in everyone if m.host == host)))
    main_enabled, main_installed = _plugins(root, config)
    for bot in present:
        if bot != "default":
            enabled, installed = _plugins(builder.home_of(root, bot), _config(builder.home_of(root, bot), problems))
            missing = sorted((main_enabled - enabled) | (main_installed - installed))
            if missing:
                problems.append("%s: plugins of the main profile missing here: %s" % (bot, ", ".join(missing)))
    if store.team:
        problems += ["%s: profile.yaml description %r differs from its role %r (written by hand; kept)"
                     % (m.profile, m.description, m.role) for m in team.mismatches(store, root, local)]
    for line in dict.fromkeys(problems):
        print(line)
    print("soul-composer doctor: %d problem(s) found" % len(dict.fromkeys(problems)))
    return 0


def _is_profile(root: Path, bot: str) -> bool:
    home = builder.home_of(root, bot)
    return home.is_dir() and named_profile_has_identity(home)


def _profiles(root: Path) -> list[str]:
    folder = root / "profiles"
    named = sorted(p.name for p in folder.iterdir() if PROFILE_ID_RE.match(p.name)) if folder.is_dir() else []
    return [p for p in ["default", *named] if _is_profile(root, p)]


def _soul(bot: str, path: Path) -> list[str]:
    try:
        data = path.read_bytes().decode("utf-8")  # bytes, as the builder reads: a CRLF re-save is a hand edit
    except FileNotFoundError:
        return ["%s: SOUL.md is not built yet (hermes soul build)" % bot]
    except (OSError, UnicodeDecodeError) as exc:
        return ["%s: SOUL.md is unreadable (%s)" % (bot, exc)]
    out = []
    stored, body = builder.split(data)
    if stored is None:
        out.append("%s: SOUL.md is not a soul-composer build (hermes soul build --force replaces it)" % bot)
    elif stored != builder.digest(body):
        out.append("%s: SOUL.md was edited by hand since its build (hermes soul absorb %s shows it)" % (bot, bot))
    if len(data) > builder.LIMIT:
        out.append("%s: SOUL.md is %d characters, over %d; it may be truncated" % (bot, len(data), builder.LIMIT))
    return out


def _config(home: Path, problems: list[str]) -> dict:
    try:
        data = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        problems.append("%s: config.yaml is unreadable (%s)" % (home, exc))
        return {}
    return data if isinstance(data, dict) else {}


def _plugins(home: Path, config: dict) -> tuple[set[str], set[str]]:
    """(``plugins.enabled`` names, ``plugins/`` folder names): a bot needs both, so they are compared apart."""
    section = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
    enabled = section.get("enabled") if isinstance(section.get("enabled"), list) else []
    folder = home / "plugins"
    installed = {p.name for p in folder.iterdir() if not p.name.startswith(".")} if folder.is_dir() else set()
    return {str(name) for name in enabled}, installed
