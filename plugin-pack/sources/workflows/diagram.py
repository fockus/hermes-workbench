"""The Mermaid diagram of a workflow run (REQ-037), built from its progress entries.

``mermaid(entries, mask=None) -> str``: ``flowchart TD``; agents without a phase in a leading group ``g0``,
then one ``subgraph p<n>`` per phase in order; one node ``a<index>`` per agent with its label, marked
``null`` when its last state is error or skipped; an edge from each group to the next. Labels and phase
titles are masked (default: the engine's stock redactor) and escaped as Mermaid entity codes.

Spec deviation (orchestrator ruling pending): REQ-037 says "from the journal", but the vendored journal
entry ``{index, key, ok, text?, resumed?}`` has no label and no phase; the progress entries carry both.
WE-WIRE stores the text through ``engine.finish(..., extra={"diagram": ...})``.
"""

from __future__ import annotations

from typing import Callable

from .progress import LABEL_CHARS, NULL_STATES, _clip, fold, state, uncut

ESCAPES = {"#": "#35;", '"': "#quot;", "[": "#91;", "]": "#93;", "(": "#40;", ")": "#41;", "{": "#123;",
           "}": "#125;", "|": "#124;", "<": "#lt;", ">": "#gt;"}


def _escape(text: str, mask: Callable[[str], str]) -> str:
    return "".join(ESCAPES.get(c, c) for c in _clip(mask(uncut(str(text))), LABEL_CHARS))


def mermaid(entries: list, mask: Callable[[str], str] | None = None) -> str:
    if mask is None:
        from .engine import _masked as mask
    phases, agents, _ = fold(entries)
    groups: list[tuple[str, str | None]] = [("g0", None)] if any(a["phase"] is None for a in agents.values()) else []
    groups += [("p%d" % n, title) for n, title in enumerate(phases)]
    lines = ["flowchart TD"]
    for ident, title in groups:
        lines.append('  subgraph %s ["%s"]' % (ident, _escape(title or "no phase", mask)))
        for index, agent in agents.items():
            if agent["phase"] == title:
                label = _escape(agent.get("label") or agent.get("agentId") or "agent", mask)
                null = " · null" if state(agent) in NULL_STATES else ""
                lines.append('    a%d["%s%s"]' % (index, label, null))
        lines.append("  end")
    lines += ["  %s --> %s" % (a[0], b[0]) for a, b in zip(groups, groups[1:])]
    return "\n".join(lines)
