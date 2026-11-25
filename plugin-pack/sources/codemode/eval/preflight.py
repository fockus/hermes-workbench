"""Preflight before any measured attempt (AGR-095, I-062): one trivial codemode script and one write_file in
a temporary folder, run through the eval's own agent. If the runtime refuses either (an orchestrator mode
that does not honour the eval's exemption, a toolset left out) or the model does not make the call, the
eval stops with the refusal text and measures nothing: an attempt that cannot use the tools it is
comparing measures the refusals."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import evidence, exempt, live, tally

SESSION_PREFIX = "codemode-eval-preflight-"
EXPECTED = "42"
PROMPT = ("Preflight check. Do exactly two things and nothing else: (1) call the codemode tool with a script "
          "that returns 6 * 7; (2) call write_file to create %s with the text ok. Then reply done.")
SHOWN = 400


def _results(messages: Sequence[Any]) -> list[tuple[str, str]]:
    """(tool name, result text) of every tool result, in the order the results came."""
    names = {c.get("id"): (c.get("function") or {}).get("name") for m in messages
             if isinstance(m, Mapping) and m.get("role") == "assistant" for c in m.get("tool_calls") or []
             if isinstance(c, Mapping)}
    return [(names[m.get("tool_call_id")], str(m.get("content") or "")) for m in messages
            if isinstance(m, Mapping) and m.get("role") == "tool" and m.get("tool_call_id") in names]


def _problem(tool: str, text: str | None) -> str:
    if text is None:
        return "preflight: the model did not call %s" % tool
    return "preflight: %s was refused or failed: %s" % (tool, text[:SHOWN])


def _codemode_ok(text: str) -> bool:
    """A ScriptResult with no error and 42 returned or printed (not a 42 inside a duration or a message):
    the preflight proves codemode is not refused, not how the model wrote the script."""
    try:
        data = json.loads(tally._GUARDRAIL.sub("", text))
    except ValueError:
        return False
    if not isinstance(data, dict) or data.get("error"):
        return False
    try:
        if json.loads(data.get("result_json")) == int(EXPECTED):
            return True
    except (TypeError, ValueError):
        pass
    return re.search(r"(?<![\d.])%s(?![\d.])" % EXPECTED, str(data.get("logs") or "")) is not None


def _first_failure(results: Sequence[tuple[str, str]], tool: str, ok: Callable[[str], bool]) -> str | None:
    """None when at least one call of ``tool`` succeeded (a refusal that a retry followed is not a refusal
    of the tool); else the first failure, in call order."""
    texts = [text for name, text in results if name == tool]
    if any(ok(text) for text in texts):
        return None
    return next((_problem(tool, text) for text in texts), None)


def check(agent_factory: Callable[[str, str, str], Any], root: Path) -> str | None:
    """None when both tools work for an eval agent, else why not."""
    try:
        Path(root).mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix="preflight-", dir=str(root)))
    except OSError as exc:
        return "preflight: cannot create the work folder under %s: %s" % (root, exc)
    target = folder / "preflight.txt"
    agent, session = None, SESSION_PREFIX + uuid.uuid4().hex[:12]
    try:
        agent = agent_factory("on", session, str(folder))
        exempt.mark(agent)
        out = agent.run_conversation(PROMPT % target) or {}
        results = _results(out.get("messages") or [])
        failure = (_first_failure(results, "codemode", _codemode_ok)
                   or _first_failure(results, "write_file", lambda text: not tally.failed(text)))
        if failure:
            return failure
        turn = evidence.turn_failure(out)  # the provider failed on a request: the tools' results are not the cause
        if turn:
            return "preflight: %s" % turn
        called = {name for name, _ in results}
        missing = next((tool for tool in ("codemode", "write_file") if tool not in called), None)
        if missing:
            return _problem(missing, None)
        if not target.is_file() or target.read_text().strip() != "ok":
            return _problem("write_file", "the file %s does not hold the expected text" % target.name)
        return None
    except Exception as exc:  # an agent that cannot start or run is a failed preflight, not a crash
        return "preflight: the agent failed: %s" % exc
    finally:
        live._close(agent, [session])
        shutil.rmtree(folder, ignore_errors=True)
