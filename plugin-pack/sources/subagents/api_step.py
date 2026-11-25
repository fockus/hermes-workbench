"""A harness-runner run of the agent API (api.py): its runner class and its StepSpec fields."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from .orchestrator import resolve_bank
from .registry import AgentDef

WRITE_TOOLS = frozenset({"write_file", "patch"})  # Bash without Write/Edit is a read-only checker (AGR-026)


def step_fields(run: dict, agent: AgentDef, harness: str, model: str, schema: dict | None,
                workspace: str) -> dict[str, Any]:
    """A run's StepSpec fields: no write tools runs read-only; a non-skill definition's body leads the prompt."""
    from hermes_constants import get_hermes_home

    cwd, skill = Path(workspace), _skill_agent(agent)
    writes = (WRITE_TOOLS if agent.tools is None else agent.tools & WRITE_TOOLS) - agent.disallowed
    prompt = run["prompt"] if skill or not agent.body.strip() else "%s\n\n%s" % (agent.body.strip("\n"),
                                                                                run["prompt"])
    return {"task_id": run["id"], "role": agent.name, "harness": harness, "model": model.split(":", 1)[1] or None,
            "effort": run["effort"] or agent.effort, "prompt": prompt, "agent_type": agent.name if skill else None,
            "output_schema": schema, "cwd": cwd, "mb_path": resolve_bank(workspace) or cwd / ".memory-bank",
            # harness-runner only echoes this path; the run's own step file is written from its result (_keep)
            "step_file": Path(get_hermes_home()) / "subagents" / "steps" / ("%s.md" % run["id"]),
            "read_only": not writes, "idle_timeout": None, "hard_timeout": None, "session_id": None, "lane": run["root_task"] or "subagents", "risk": "medium"}


def runner_class(module: Any = None) -> type | None:
    """harness-runner's runner: its ``Runner`` facade, else a concrete ``HarnessRunner``; None when not importable."""
    if module is None:
        try:
            module = importlib.import_module("harnessrunner")
        except ImportError:
            return None
    found = getattr(module, "Runner", None) or getattr(module, "HarnessRunner", None)
    return found if isinstance(found, type) and not getattr(found, "_is_protocol", False) else None


def _skill_agent(agent: AgentDef) -> bool:
    """Whether the definition is the Memory Bank skill's own (the only agents harness-runner renders)."""
    try:
        from harnessrunner.mb import skill_source
    except ImportError:
        return False
    return agent.source.resolve().is_relative_to((skill_source() / "agents").resolve())
