"""``hermes codemode doctor|eval|patch`` (spec Tasks 15 and 12; REQ-002, REQ-022, REQ-024, REQ-029).
``patch`` applies the core tool-list hook to the running Hermes checkout (patcher.py). Doctor reports the pinned dependency, the nested-call seam, the tool-list hook, the mode, the exposure map
and the matching ``tools.tool_search.defer`` line; it never writes the Hermes configuration."""

from __future__ import annotations

import argparse
from typing import Any, Callable

from . import deps, modes, seams

INSTALL_SCRIPT = "bash scripts/codemode-deps.sh hermes"
GetConfig = Callable[[str, Any], Any]


def setup(parser: argparse.ArgumentParser) -> None:
    from .eval import cli as eval_cli

    sub = parser.add_subparsers(dest="codemode_command", required=True)
    sub.add_parser(
        "doctor", help="check the sandbox dependency, the Hermes seams and the settings"
    )
    eval_cli.setup(
        sub.add_parser(
            "eval", help="paired A/B eval of code mode (calls the configured model)"
        )
    )
    sub.add_parser(
        "patch",
        help="apply the core tool-list hook patch to this Hermes checkout (idempotent, with a backup)",
    )


def install_hint(found: deps.DepsStatus) -> str:
    return "install or repair it with `%s` from the hermes-plugins checkout, or run: %s" % (
        INSTALL_SCRIPT,
        found.install_command,
    )


def _current_defer() -> list[str]:
    from tools.tool_search import load_config_readonly

    return sorted(load_config_readonly().effective_defer_tools)


def doctor_lines(get_config: GetConfig, found: deps.DepsStatus) -> list[str]:
    if found.ok:
        lines = ["%s: %s in %s" % (deps.PACKAGE, found.version, found.folder)]
    else:
        lines = [
            "%s: %s; codemode is not registered" % (deps.PACKAGE, found.problem),
            install_hint(found),
        ]
    settings = modes.read_settings(get_config)
    declared = seams.declared() or not found.ok  # an unregistered tool has nothing to defer
    return lines + [_hook_line()] + modes.doctor_lines(
        settings, current_defer=_current_defer(), declared=declared
    )


def _hook_line() -> str:
    from .patcher import hook_present

    if hook_present():
        return "tool-list hook: present (other plugins' per-session tool filters are active)"
    return ("tool-list hook: absent; other plugins' per-session tool filters do nothing until "
            "`%s` installs it" % modes.PATCH_COMMAND)


def _eval_problem() -> str | None:
    """Why the eval cannot measure code mode: Monty missing or not answering (a worker handshake
    before the first agent, hardening G5), or the codemode tool not registered."""
    found = deps.activate(deep=True)
    if not found.ok:
        return "codemode eval needs %s: %s; %s" % (deps.PACKAGE, found.problem, install_hint(found))
    from tools.registry import registry

    if registry.get_entry(seams.TOOL) is None:
        return "codemode eval needs the codemode tool, which is not registered (see `hermes codemode doctor`)"
    return None


def _patch() -> int:
    from . import patcher

    try:
        print(patcher.apply(patcher.checkout()))
    except (patcher.PatchRefused, OSError, UnicodeDecodeError) as exc:
        print("codemode patch: %s" % exc)
        return 1
    print("restart Hermes to load the hook")
    return 0


def run(get_config: GetConfig, args: argparse.Namespace) -> int:
    if args.codemode_command == "patch":
        return _patch()
    if args.codemode_command == "eval":
        problem = _eval_problem()
        if problem:  # both arms would run without code mode: nothing to measure
            print(problem)
            return 1
        from .eval import cli as eval_cli

        return eval_cli.run(args)
    found = deps.activate(deep=True)  # the one result both the report and the exit code come from
    print("\n".join(doctor_lines(get_config, found)))
    return 0 if found.ok else 1
