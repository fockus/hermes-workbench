"""The team of ``~/.hermes/soul/team.yaml``: members, the Team block per bot and description sync (Task 6).

Entry points for the commands (SC-CLI):

* ``build(hermes_root, *, host=None, ctx=None, force=False) -> TeamBuild``: validates the team (raising
  ``TeamError`` before anything is written), builds the recipes of this machine's members through
  ``build.build_one`` with ``resolver(...)``, then syncs their profile.yaml descriptions; ``TeamBuild.mismatches``
  are the hand-written descriptions. Without team.yaml every recipe file is built; a team.yaml that lists no
  members builds nothing (with a warning).
* ``resolver(store, host=None, ctx=None)``: the build.py resolver seam ``resolve(name, bot)``: ``team`` is the bot's
  Team block, ``provider:<name>`` the Task 5 provider resolver (bricks.provider_resolver, given ``ctx``).
* ``members(store)`` and ``local_members(store, host=None)``: the validated team, and the members built here.
* ``mismatches(store, hermes_root, local)``: the hand-written descriptions, read only (doctor).

A member is ``{profile, role, zone, reports_to?, host, bricks}`` (SPIKE.md § 7); a member without ``host`` is
refused. This machine's host is ``socket.gethostname()`` up to the first dot, lower-cased. A member of
another machine is addressed as ``@<host>/<profile>`` with ``hermes peer dm <host>/<profile>`` (SPIKE.md § 3) and
is never built here. The description is set to the role when it is empty, already the role, auto-generated
(``description_auto: true``) or the one the composer last wrote (recorded in ``<store>/.cache/descriptions.yaml``,
which a push must leave out); any other text is hand-written and kept (SPIKE.md § 2).
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from pathlib import Path

import yaml
from hermes_constants import PROFILE_ID_RE, named_profile_has_identity

from . import bricks
from . import build as builder
from .bricks import MissingBrick, Store, StoreError

logger = logging.getLogger("soulcomposer")
RECORD = "descriptions.yaml"


class TeamError(StoreError):
    """team.yaml has a reports_to cycle, an unknown lead or a malformed member: nothing is built."""


@dataclass(frozen=True)
class Member:
    profile: str
    role: str
    zone: str
    reports_to: str | None  # None = peer
    host: str


@dataclass(frozen=True)
class Mismatch:
    """A hand-written profile.yaml description that differs from the member's role (doctor, REQ-009)."""

    profile: str
    description: str
    role: str


@dataclass(frozen=True)
class TeamBuild:
    results: list[builder.BuildResult]
    mismatches: list[Mismatch]


def this_host() -> str:
    return socket.gethostname().split(".", 1)[0].lower()


def _text(raw: dict, key: str, name) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TeamError("soul-composer: team member %s has no %s" % (name, key))
    return value.strip()


def _member(raw: dict) -> Member:
    name = raw.get("profile")
    if not isinstance(name, str) or not PROFILE_ID_RE.match(name):
        raise TeamError("soul-composer: team.yaml names an invalid profile %r" % (name,))
    where, lead = raw.get("host"), raw.get("reports_to")
    if where is None or isinstance(where, str) and not where.strip():
        raise TeamError("soul-composer: team member %s has no host (team.yaml needs host: <machine>)" % name)
    if not isinstance(where, str) or not PROFILE_ID_RE.match(where):
        raise TeamError("soul-composer: team member %s has an invalid host %r" % (name, where))
    if lead is not None and not isinstance(lead, str):
        raise TeamError("soul-composer: team member %s has an invalid reports_to %r" % (name, lead))
    return Member(name, _text(raw, "role", name), _text(raw, "zone", name), lead or None, where)


def members(store: Store, host: str | None = None) -> tuple[Member, ...]:
    """The team in team.yaml order; raises TeamError naming the members of a cycle or an unknown lead."""
    team = tuple(_member(raw) for raw in store.members)
    names = [m.profile for m in team]
    twice = sorted({n for n in names if names.count(n) > 1})
    if twice:
        raise TeamError("soul-composer: team.yaml lists %s more than once" % ", ".join(twice))
    lead = {m.profile: m.reports_to for m in team}
    for m in team:
        if m.reports_to and m.reports_to not in lead:
            raise TeamError("soul-composer: %s reports to %s, who is not a team member" % (m.profile, m.reports_to))
    for m in team:
        chain = [m.profile]
        while lead[chain[-1]]:
            chain.append(lead[chain[-1]])
            if chain[-1] in chain[:-1]:
                loop = chain[chain.index(chain[-1]):]
                raise TeamError("soul-composer: reports_to cycle %s" % " -> ".join(loop))
    return team


def local_members(store: Store, host: str | None = None) -> tuple[Member, ...]:
    """The members whose host is this machine (``host``): the only ones built here (REQ-019)."""
    here = host or this_host()
    return tuple(m for m in members(store, here) if m.host == here)


def _handle(m: Member, host: str) -> str:
    return m.profile if m.host == host else "@%s/%s" % (m.host, m.profile)


def _hand_off(m: Member, host: str) -> str:
    return "message_agent to %s" % m.profile if m.host == host else "hermes peer dm %s/%s" % (m.host, m.profile)


def block(me: Member, team: tuple[Member, ...], host: str) -> str:
    """The Team block of ``me``: role, lead, reports, peers, every zone and the hand-off rule (REQ-007)."""
    by = {m.profile: m for m in team}
    reports = [_handle(m, host) for m in team if m.reports_to == me.profile]
    peers = [_handle(m, host) for m in team if m.profile not in (me.profile, me.reports_to)
             and (m.reports_to is None or m.reports_to == me.reports_to)]
    lines = ["## Team", "You are %s, role: %s, zone: %s." % (me.profile, me.role, me.zone),
             "Lead: %s" % (_handle(by[me.reports_to], host) if me.reports_to else "none"),
             "Reports: %s" % (", ".join(reports) or "none"), "Peers: %s" % (", ".join(peers) or "none"), "Zones:"]
    for m in team:
        if m is me:
            lines.append("- %s (you) · role: %s · zone: %s" % (_handle(m, host), m.role, m.zone))
        else:
            lines.append("- %s · role: %s · zone: %s · hand off: %s" % (_handle(m, host), m.role, m.zone,
                                                                         _hand_off(m, host)))
    lines.append("Hand a task to the member whose zone owns it: message_agent for a member on this machine, "
                 "`hermes peer dm <host>/<profile>` (handle @<host>/<profile>) for one on another machine.")
    return "\n".join(lines)


def resolver(store: Store, host: str | None = None, ctx=None, providers: builder.Resolver | None = None
             ) -> builder.Resolver:
    """The build resolver of ``store``: ``team`` → the bot's Team block, ``provider:<name>`` → its plugin
    (``providers``, by default ``bricks.provider_resolver`` over the store's own cache)."""
    here = host or this_host()
    team = members(store, here)
    by = {m.profile: m for m in team}
    providers = providers or bricks.provider_resolver(store.root, ctx)

    def resolve(name: str, bot: str) -> str | None:
        if name == "team":
            if bot not in by:
                raise MissingBrick("team (%s is not in team.yaml)" % bot)
            return block(by[bot], team, here)
        return providers(name, bot)

    return resolve


def build(hermes_root: Path, *, host: str | None = None, ctx=None, force: bool = False) -> TeamBuild:
    """Builds this machine's team members (every recipe when there is no team) and syncs their descriptions."""
    store = bricks.load(hermes_root / "soul")
    if store.team and not store.members:
        logger.warning("soul-composer: team.yaml lists no members; nothing built")
        return TeamBuild([], [])
    here = host or this_host()
    resolve = resolver(store, here, ctx)  # validates the whole team before anything is written
    local = local_members(store, here)
    names = {m.profile for m in local}
    recipes = [r for r in store.recipes if r.bot in names] if store.team else list(store.recipes)
    results = [builder.build_one(store, r, builder.home_of(hermes_root, r.bot), resolve, force) for r in recipes]
    return TeamBuild(results, sync_descriptions(store, hermes_root, local))


def sync_descriptions(store: Store, hermes_root: Path, local: tuple[Member, ...]) -> list[Mismatch]:
    """Sets each local member's profile.yaml description to its role unless written by hand (REQ-009)."""
    from hermes_cli.profiles import write_profile_meta

    record = _record(store)
    mismatches, written = [], dict(record)
    for m, home, text, auto in _described(hermes_root, local):
        verdict = _verdict(m, text, auto, record)
        if verdict == "mismatch":
            mismatches.append(Mismatch(m.profile, text, m.role))
        if verdict != "write":
            continue
        try:
            write_profile_meta(home, description=m.role, description_auto=False)
        except OSError as exc:
            logger.warning("soul-composer: %s: profile.yaml description not written (%s)", m.profile, exc)
            continue
        written[m.profile] = m.role
    if written != record:
        try:
            bricks.write_private(bricks.cache_dir(store.root) / RECORD, yaml.safe_dump(written, sort_keys=True))
        except (OSError, StoreError) as exc:
            logger.warning("soul-composer: the description record %s was not written (%s)",
                           store.root / bricks.CACHE / RECORD, exc)
    return mismatches


def mismatches(store: Store, hermes_root: Path, local: tuple[Member, ...]) -> list[Mismatch]:
    """The hand-written descriptions ``sync_descriptions`` would keep; writes nothing (doctor)."""
    record = _record(store)
    return [Mismatch(m.profile, text, m.role) for m, _home, text, auto in _described(hermes_root, local)
            if _verdict(m, text, auto, record) == "mismatch"]


def _record(store: Store) -> dict:
    """The descriptions the composer last wrote, from ``<store>/.cache/descriptions.yaml``."""
    record_path = store.root / bricks.CACHE / RECORD
    try:
        record = yaml.safe_load(bricks.read_private(record_path) or "") or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError, StoreError) as exc:
        logger.warning("soul-composer: %s is unusable (%s); every description is taken as hand-written", record_path, exc)
        record = {}
    return record if isinstance(record, dict) else {}


def _described(hermes_root: Path, local: tuple[Member, ...]):
    """(member, home, description, description_auto) of each local member with a readable profile.yaml."""
    from hermes_cli.profiles import read_profile_meta

    for m in local:
        home = builder.home_of(hermes_root, m.profile)
        if not (home.is_dir() and named_profile_has_identity(home)):
            continue  # no profile here: nothing to describe
        if not _is_mapping(home / "profile.yaml"):
            logger.warning("soul-composer: %s: profile.yaml is not a YAML mapping; description not written", m.profile)
            continue
        meta = read_profile_meta(home)
        yield m, home, meta["description"], meta["description_auto"]


def _verdict(m: Member, text: str, auto: bool, record: dict) -> str:
    """``keep`` (already the role), ``mismatch`` (written by hand) or ``write`` (the role replaces it)."""
    if text == m.role:
        return "keep"
    if text and not auto and text != record.get(m.profile):
        return "mismatch"
    return "write"


def _is_mapping(path: Path) -> bool:
    """True when ``path`` is absent or holds a YAML mapping (or nothing): Hermes rewrites only such a file whole."""
    import hermes_yaml  # the loader and encoding Hermes' own profile.yaml reader uses

    try:
        data = hermes_yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return True
    except (OSError, UnicodeDecodeError, ValueError, hermes_yaml.YAMLError):
        return False
    return data is None or isinstance(data, dict)
