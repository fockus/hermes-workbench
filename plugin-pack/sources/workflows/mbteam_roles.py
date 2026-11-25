"""Running the Memory Bank skill's ``mb-team-track.js`` on Hermes (MBT-HERMESWF).

``resolve(agent_type, api)`` is the type a spawn uses: the script names roles of the skill's pipeline (``mb-backend``,
``mb-judge``, ``plan-verifier``, ``codex-reviewer``, ...). The subagents registry knows them only where the skill is
installed (``skills_root/agents``) or a project defines them; a name it does not know maps to the built-in type with
the tools the role needs (``ROLE_TYPES``; the roles whose skill definition has no Write/Edit get the read-only
``verifier``). A name the registry knows or refuses is never replaced (the exception classes are told apart by name,
because the registry package loads as ``subagents`` or ``hermes_plugins.subagents`` and the classes differ), and an
unmapped name is returned as it came. ``run_refusal(source, args, gate)`` is the reason a run of that script cannot
start, or None: its review step has an agent run ``args.review.command``, so the reviewer program must exist on this
host (checked with ``gate``, the host's ``/bin/sh -c`` gate) before the first agent starts. It looks at no other
script.
"""

from __future__ import annotations

import re
import shlex
from typing import Any, Callable

VERIFIER = "verifier"  # read, search and terminal; no edit tools
IMPLEMENTER = "general-purpose"
ROLE_TYPES = {
    **{name: IMPLEMENTER for name in ("mb-developer", "mb-backend", "mb-frontend", "mb-ios", "mb-android",
                                      "mb-architect", "mb-analyst", "mb-devops", "mb-qa", "mb-debugger")},
    **{name: VERIFIER for name in ("plan-verifier", "mb-judge", "mb-researcher", "codex-reviewer", "mb-reviewer",
                                   "mb-reviewer-logic",
                                   "mb-reviewer-tests", "mb-reviewer-quality", "mb-reviewer-security",
                                   "mb-reviewer-scalability", "mb-reviewer-lead")},
}
REFUSED = "review refused: %s cannot run here: %s"
REVIEW_STARTS = ("execute", "verify", "fix-review")  # the starts of mb-team-track.js that run the review step
SCRIPT = re.compile(r"""\bname\s*:\s*['"]mb-team-track['"]""")
# Programs that run the next word: ``env`` is read through, the others take options we do not parse.
READ_THROUGH = {"command", "exec", "nohup"}
UNPARSED = {"nice", "timeout", "gtimeout", "sudo", "xargs", "stdbuf", "setsid", "time", "caffeinate", "sh", "bash",
            "zsh"}


def resolve(agent_type: str, api: Any) -> str:
    mapped = ROLE_TYPES.get(agent_type)
    find = getattr(getattr(api, "registry", None), "find", None)
    if mapped is None or find is None:
        return agent_type
    try:
        find(agent_type)
    except Exception as exc:  # noqa: BLE001 - told apart by class name, see the module docstring
        names = {cls.__name__ for cls in type(exc).__mro__}
        if "UnknownAgentType" in names and "RefusedAgentType" not in names:
            return mapped  # a refused name keeps its own: a broader type must not stand in for it
    return agent_type


def run_refusal(source: Any, args: Any, gate: Callable[[str], dict]) -> str | None:
    """Why a run of ``source`` cannot start, or None: only the mb-team-track script is looked at."""
    if not (isinstance(source, str) and SCRIPT.search(source)):
        return None
    return review_refusal(args, gate)


def review_refusal(args: Any, gate: Callable[[str], dict]) -> str | None:
    """Why the review step of a team-track run cannot run, or None. The message names the program, never the
    command line (its argument values may be secrets)."""
    if not (isinstance(args, dict) and isinstance(args.get("review"), dict) and isinstance(args.get("tracks"), list)):
        return None
    if not any(isinstance(t, dict) and t.get("review_rounds") and t.get("start", "execute") in REVIEW_STARTS
               for t in args["tracks"]):
        return None  # no round will run
    command = args["review"].get("command")
    cli, why = _cli(command) if isinstance(command, str) else ("", "the review command names no program")
    if not cli:
        return REFUSED % ("the review command", why or "it names no program")
    probe = gate("command -v %s" % shlex.quote(cli))
    return None if probe.get("ok") else REFUSED % (cli, "%s is not installed on this host" % cli)


def _cli(command: str) -> tuple[str, str]:
    """``(program, "")`` the review command runs, after a ``perl -e '<prog>'`` timeout wrapper, ``env`` with
    assignments, and ``command``/``exec``/``nohup``; ``("", reason)`` when it cannot be told."""
    try:
        words = shlex.split(command)
    except ValueError:
        return "", ""
    if words[:2] == ["perl", "-e"]:
        words = words[3:]
    while words:
        word = words[0]
        if re.fullmatch(r"[A-Za-z_]\w*=.*", word) or word in READ_THROUGH:
            words = words[1:]
        elif word == "env":
            words = words[1:]
            if words and words[0].startswith("-"):
                return "", "the program behind env %s cannot be told" % words[0]
        elif word in UNPARSED:
            return "", "the program behind %s cannot be told" % word
        else:
            return word, ""
    return "", ""
