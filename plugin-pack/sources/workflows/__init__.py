"""workflows: deterministic multi-agent workflow scripts for Hermes (spec workflow-engine).

The engine API lives in ``workflows.engine`` (``WorkflowEngine``, ``RunSummary``), the run store in
``workflows.store`` and the runner bridge in ``workflows.bridge``. ``register(ctx)`` imports its modules inside the
function, so loading the package starts no Node process and touches no file.

``register`` adds the ``workflow`` tool (workflows/tool.py), the ``pre_llm_call`` hook that records, per session,
whether the latest user turn opts in (workflows/optin.py), and the commands and CLI (workflows/commands.py). The plugin is disabled until the user enables it.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("workflows")


def register(ctx, make_host=None) -> None:
    """``make_host(cwd)`` replaces the per-call ``HermesAgentHost`` (the tests' seam); Hermes passes only ``ctx``."""
    from .commands import register_commands
    from .tool import SCHEMA, make_handler
    from .wiring import Wiring

    wiring = Wiring(ctx, make_host)
    ctx.register_tool(name="workflow", toolset="workflows", schema=SCHEMA, handler=make_handler(ctx, wiring),
                      description=SCHEMA["description"])
    ctx.register_hook("pre_llm_call", wiring.pre_llm_call)
    register_commands(ctx, wiring)
    logger.debug("workflows: registered the workflow tool, the opt-in hook and the commands")
