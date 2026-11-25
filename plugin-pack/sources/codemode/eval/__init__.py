"""Paired A/B eval of code mode and its rollout gate (spec Task 11; REQ-029, REQ-030).

``run_eval(tasks, runner, repeats=3)`` runs and reports; ``rollout_gate(report)`` judges on mode
against direct. ``cli`` is ``hermes codemode eval``, ``live`` its Hermes-agent runner, ``tasks``
the task set; ``big_tasks``, ``big_gate`` and ``evidence`` are the big class and the per-attempt
record (AGR-103). Importing this package loads no Hermes module.
"""

from __future__ import annotations

from .gate import GateResult, rollout_gate
from .metrics import run_eval

__all__ = ["GateResult", "rollout_gate", "run_eval"]
