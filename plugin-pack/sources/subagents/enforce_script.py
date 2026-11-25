"""The calls a codemode script makes (SA-CMODE, AGR-094).

Code-mode dispatches each nested call as the agent's own tool call: the hook sees the agent's session and a
``tool_call_id`` of ``codemode-<12 hex>-<n>`` (codemode/bridge.py: one run id per codemode call), so a call is a
script's when its id says so. All of one run's calls share ``<12 hex>``; that is the unit the read budget counts."""

from __future__ import annotations

import re

SCRIPT_TOOL = "codemode"
_NESTED_ID = re.compile(r"codemode-([0-9a-f]{12})-\d+")


def script_run(tool_call_id: object) -> str | None:
    """The run id of the codemode call that made this call, or None for a direct call."""
    match = _NESTED_ID.fullmatch(tool_call_id) if isinstance(tool_call_id, str) else None
    return match.group(1) if match else None
