"""soul-composer: builds each bot's SOUL.md from bricks in ``~/.hermes/soul`` (spec soul-composer).

``register(ctx)`` mounts ``hermes soul build|new|import|absorb|doctor`` (``soulcomposer.cli``, imported lazily) with
the plugin context in the parser defaults (``soul_ctx``), so provider bricks resolve through this home's plugins, and
registers ``on_session_start``: in the main profile (``default``) a session start builds every local team member
through ``team.build`` without ``--force``; any other profile, or no ``<root>/soul`` store, does nothing. The hook
never raises or prints: a refused build is one warning on the ``soulcomposer`` logger.

Degradation (Task 10): when a Hermes seam the plugin relies on is missing, ``hermes soul`` is still mounted but every
subcommand prints one line naming the missing seams and exits 1, no hook is registered and nothing is written. When
the commands module is absent nothing is registered at all.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import logging

HELP = "soul-composer: build|new|import|absorb|doctor (bots' SOUL.md from ~/.hermes/soul)"
SEAMS = (("hermes_constants", "get_default_hermes_root"), ("hermes_constants", "named_profile_has_identity"),
         ("hermes_constants", "PROFILE_ID_RE"), ("hermes_cli.profiles", "get_active_profile_name"),
         ("hermes_cli.profiles", "read_profile_meta"), ("hermes_cli.profiles", "write_profile_meta"))

logger = logging.getLogger("soulcomposer")


def _cli():
    return importlib.import_module(__name__ + ".cli")


def _handle(args) -> int:
    return _cli().handle(args)


def _missing_seams(ctx) -> list[str]:
    """The Hermes names this plugin needs that the running Hermes lacks (import probe and ``hasattr``)."""
    missing = []
    for module, name in SEAMS:
        try:
            found = hasattr(importlib.import_module(module), name)
        except ImportError:
            found = False
        if not found:
            missing.append("%s.%s" % (module, name))
    if not callable(getattr(ctx, "register_hook", None)):
        missing.append("ctx.register_hook")
    return missing


def _refusing(missing: list[str]):
    """``hermes soul`` setup and handler that refuse every subcommand with one line."""
    line = "soul-composer: Hermes seams missing (%s); nothing written" % ", ".join(missing)

    def setup(parser) -> None:
        parser.add_argument("soul_args", nargs=argparse.REMAINDER)

    def handle(_args) -> int:
        print(line)
        return 1

    return setup, handle


def _session_start(ctx):
    def on_session_start(**_kw) -> None:
        try:
            from hermes_cli.profiles import get_active_profile_name

            if get_active_profile_name() != "default":
                return
            root = _cli().hermes_root()
            if not (root / "soul").is_dir():
                return
            from . import team

            for result in team.build(root, ctx=ctx, force=False).results:
                for note in result.warnings:
                    logger.warning(note)
        except Exception as exc:  # noqa: BLE001 - a session must start whatever the build does
            logger.warning("soul-composer: session-start build skipped: %s", exc)

    return on_session_start


def register(ctx) -> None:
    """Mounts ``hermes soul`` and the main profile's session-start build (SPIKE.md § 7)."""
    if importlib.util.find_spec(__name__ + ".cli") is None:
        return
    missing = _missing_seams(ctx)
    if missing:
        logger.warning("soul-composer: Hermes seams missing (%s); commands refuse, no session-start build",
                       ", ".join(missing))
        ctx.register_cli_command("soul", HELP, *_refusing(missing))
        return

    def setup(parser) -> None:
        _cli().setup(parser)
        parser.set_defaults(soul_ctx=ctx)

    ctx.register_cli_command("soul", HELP, setup, _handle)
    ctx.register_hook("on_session_start", _session_start(ctx))
