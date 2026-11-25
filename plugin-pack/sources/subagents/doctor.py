"""`hermes subagents doctor|setup` (REQ-017): the plugin never changes delegation settings on its own.

``doctor`` prints the values agent types need (``delegation.backend: subagents``,
``delegation.max_spawn_depth: 5``) and every model alias whose provider is not installed, with the
exact ``delegation.backends.subagents.aliases`` remap when an installed provider of that family
exists. ``setup`` offers the same changes and writes each group only after the user answers yes,
through Hermes' comment-preserving config writer.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

import yaml

from .provider import alias_table

NEEDED = {"backend": "subagents", "max_spawn_depth": 5}
BACKEND = "subagents"


def setup_parser(parser: Any) -> None:
    sub = parser.add_subparsers(dest="subagents_command")
    sub.add_parser("doctor", help="Check the delegation settings and model aliases agent types need")
    sub.add_parser("setup", help="Write the needed delegation settings after asking")


def handle(args: Any) -> None:
    if getattr(args, "subagents_command", None) == "setup":
        setup()
    else:
        doctor()


def _config() -> tuple[Path, Mapping[str, Any]]:
    from hermes_cli.config import get_config_path, read_user_config_raw

    path = get_config_path()
    delegation = read_user_config_raw(path).get("delegation")
    return path, delegation if isinstance(delegation, Mapping) else {}


def _missing_settings(delegation: Mapping[str, Any]) -> list[str]:
    lines = []
    for key, value in NEEDED.items():
        now = delegation.get(key)
        if now != value:
            lines.append("- delegation.%s: %s needed, %s" % (key, value, "not set" if now is None else "now %r" % now))
    return lines


def _provider_installed(name: str) -> bool:
    import providers

    return providers.get_provider_profile(name) is not None


def _installed_like(provider: str) -> str | None:
    """An installed provider of the same family (``<name>-experimental`` and the like)."""
    import providers

    names = sorted(p.name for p in providers.list_providers())
    return next((n for n in names if n.startswith(provider + "-") or provider.startswith(n + "-")), None)


def _alias_remap(delegation: Mapping[str, Any]) -> tuple[list[str], dict[str, dict[str, str]]]:
    backends = delegation.get("backends")
    own = backends.get(BACKEND) if isinstance(backends, Mapping) else None
    lines, remap = [], {}
    for alias, (provider, model) in alias_table(own.get("aliases") if isinstance(own, Mapping) else None).items():
        if _provider_installed(provider):
            continue
        target = _installed_like(provider)
        lines.append("- alias %s: provider %s is not installed%s" % (
            alias, provider, "; %s is" % target if target else "; remap it to a provider you use"))
        if target:
            remap[alias] = {"provider": target, "model": model}
    return lines, remap


def _remap_yaml(remap: Mapping[str, Any]) -> str:
    return yaml.safe_dump({"delegation": {"backends": {BACKEND: {"aliases": dict(remap)}}}},
                          default_flow_style=None, sort_keys=False)


def doctor() -> None:
    path, delegation = _config()
    settings = _missing_settings(delegation)
    aliases, remap = _alias_remap(delegation)
    print("hermes subagents doctor (%s)" % path)
    if not (settings or aliases):
        print("all set: delegation.backend: subagents, delegation.max_spawn_depth: 5, every alias provider installed")
        return
    for line in settings:
        print(line)
    if settings:
        print("  Agent types stay inactive until these are set; `hermes subagents setup` writes them after asking.")
    for line in aliases:
        print(line)
    if remap:
        print("Remap these aliases in %s (or let `hermes subagents setup` write it):" % path)
        print(_remap_yaml(remap), end="")


def _consent(question: str) -> bool:
    print("%s [y/N] " % question, flush=True)
    return sys.stdin.readline().strip().lower() in ("y", "yes")


def setup() -> None:
    from hermes_cli.config_backend import write_config_keys

    path, delegation = _config()
    settings = _missing_settings(delegation)
    _, remap = _alias_remap(delegation)
    if not (settings or remap):
        print("Nothing to change: delegation settings and alias providers are in place.")
        return
    for line in settings:
        print(line)
    if settings and _consent("Write delegation.backend: subagents and delegation.max_spawn_depth: 5 to %s?" % path):
        write_config_keys(path, {"delegation.%s" % key: value for key, value in NEEDED.items()})
        print("Written. Restart Hermes to activate agent types.")
    if remap:
        print(_remap_yaml(remap), end="")
        if _consent("Write this alias remap to %s?" % path):
            write_config_keys(path, {"delegation.backends.%s.aliases.%s" % (BACKEND, alias): target
                                     for alias, target in remap.items()})
            print("Alias remap written.")
