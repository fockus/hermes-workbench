"""Sandbox source of the script prelude (sdk.py ``Sdk.prelude``): ``search_tools``, ``describe_tool``,
``retry``, ``settle`` and ``bulk`` (bulk.py) always, ``sh`` over ``tools.terminal`` and ``edit`` over ``tools.patch`` only
when the session's stub of that tool fits the helper."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .bulk import SOURCE as _BULK_SOURCE

if TYPE_CHECKING:
    from .sdk import SdkTool

_HELPERS = '''
import asyncio as _cm_asyncio


def search_tools(query: str, limit: int = 10) -> list[str]:
    """Qualified names of the tools whose name or description holds the most query words."""
    words = query.lower().split()
    scored = []
    for name in ALL_TOOLS:
        text = _CM_STUBS[name].lower()
        score = 0
        for word in words:
            if word in text:
                score += 1
        if score:
            scored.append((-score, name))
    scored.sort()
    return [name for _, name in scored[:limit]]


def describe_tool(name: str) -> str:
    """The typed stub of a qualified tool name such as tools.read_file."""
    if name not in _CM_STUBS:
        raise KeyError("unknown tool " + name + ": see search_tools() or ALL_TOOLS")
    return _CM_STUBS[name]


async def retry(fn, *args, attempts: int = 3, **kwargs):
    """Await fn(*args, **kwargs), trying a failed call up to attempts times in total."""
    for n in range(attempts):
        try:
            return await fn(*args, **kwargs)
        except Exception:
            if n == attempts - 1:
                raise


async def _cm_settled(aw):
    try:
        return {"ok": await aw}
    except Exception as exc:
        return {"error": str(exc)}


async def settle(*aws):
    """Await the calls together: one {"ok": value} or {"error": text} per call, in order."""
    return list(await _cm_asyncio.gather(*[_cm_settled(a) for a in aws]))
'''
_HELPERS += _BULK_SOURCE

_SH_SOURCE = '''

async def sh(command: str, check: bool = True, **kw):
    """Run command through tools.terminal and return its output. A non-zero exit raises with the
    output; check=False returns the output anyway. Other keywords go to tools.terminal."""
    try:
        result = await tools.terminal(command=command, **kw)
    except RuntimeError as exc:
        text = str(exc)
        if check or not text.startswith("tools.terminal failed: exit "):
            raise
        return text[text.find(": ", len("tools.terminal failed: exit ")) + 2:]
    if isinstance(result, dict) and result.get("session_id") and not kw.get("background"):
        raise RuntimeError("tools.terminal moved the command to the background before it finished (session %s): "
                           "wait for it with tools.process_manage" % result["session_id"])
    return result["output"] if isinstance(result, dict) else result
'''

_EDIT_SOURCE = '''

async def edit(path: str, old: str, new: str):
    """Replace old with new in the file at path through tools.patch; raises when old is not found."""
    return await tools.patch(path=path, old_string=old, new_string=new%s)
'''


def _fits(tool: SdkTool, names: set[str], optional: frozenset[str] = frozenset()) -> bool:
    """The stub takes ``names`` and requires nothing beyond them and ``optional``."""
    have = {p.py for p in tool.params}
    return names <= have and {p.py for p in tool.params if p.required} <= names | optional


def _sh(tool: SdkTool) -> str:
    return _SH_SOURCE if _fits(tool, {"command"}) else ""


def _edit(tool: SdkTool) -> str:
    """Stock serves patch with or without ``mode`` (the V4A variant requires it): the helper always
    replaces, so its source must match the stub the prelude is type checked against."""
    if not _fits(tool, {"path", "old_string", "new_string"}, frozenset({"mode"})):
        return ""
    return _EDIT_SOURCE % (', mode="replace"' if any(p.py == "mode" for p in tool.params) else "")


# (qualified SDK name, registry tool, helper): fed only when its tool is in the SDK with a usable stub
_TOOL_HELPERS = (("tools.terminal", "terminal", _sh), ("tools.patch", "patch", _edit))
