"""Help appended to a script error so the model can fix the call without a second round trip (CM-BULK).

Two shapes are recognised: an error that names a tool (``tools.<name> failed:`` from nested.py, or the
argument TypeErrors of sdk.py ``SdkTool.arguments``) gets that tool's stub; a refused import (at run time, or the type check's unresolved-import) gets the
importable modules. Anything else comes back unchanged.
"""

from __future__ import annotations

import re

from .sdk import Sdk

MAX_EXTRA = 600
# the modules a script can import (codemode/SPIKE.md § Sandbox surface); modes.py keeps its own copy
IMPORTABLE = (
    "os sys json re math asyncio datetime collections itertools functools typing dataclasses pathlib "
    "random time base64 hashlib csv tomllib decimal copy bisect fnmatch binascii"
).split()
_NAMED = re.compile(
    r"((?:tools|commands)\.\w+|mcp\.\w+\.\w+)(?= failed:| takes at most \d+ positional| got multiple values)"
)
_REFUSED_IMPORT = re.compile(  # the exception type opens its line; unresolved-import is Monty's type-check code
    r"^(?:ModuleNotFoundError|ImportError)\b|error\[unresolved-import\]", re.MULTILINE
)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _stub_of(error: str, sdk: Sdk) -> str:
    found = _NAMED.search(error)
    tool = sdk.resolve(found.group(1)) if found else None
    if tool is None:
        return ""
    text = tool.stub.strip()
    if len(text) + 2 > MAX_EXTRA:  # keep the signature, which is what the model needs
        text = text[text.index("async def"):]
    return _cut(text, MAX_EXTRA - 2)


def explain(error: str | None, sdk: Sdk) -> str | None:
    """``error`` plus at most 600 characters of help, or ``error`` itself when there is none to give."""
    if not error:
        return error
    extra = _stub_of(error, sdk)
    if not extra and _REFUSED_IMPORT.search(error):
        extra = _cut("Importable modules: " + ", ".join(IMPORTABLE), MAX_EXTRA - 2)
    return error + "\n\n" + extra if extra else error
