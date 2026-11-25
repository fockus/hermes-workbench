"""SDK imports of a script (used by repl.py and executor.py; REQ-017, REQ-026, REQ-033).

A REPL reloaded for tools added mid-session serves the SDK modules under generation-prefixed
names (``codemode_sdk<N>_tools``), because a restored dump keeps ty's view of the plain names it
imported. Its prelude binds them to ``tools``, ``mcp`` and ``commands``; ``rewrite`` does the same
for a script's own import statements, keeping lines and columns, and ``plain`` takes the prefix off
the leading module of a call name or a diagnostic again, nothing else.

Each SDK import statement takes one Monty suspension (codemode/SPIKE.md § Pool), and the session
REPL reserves its suspensions before a script runs (executor.py ``_renewal``). An import inside a
function, loop or block could run any number of times, so SDK imports must be straight-line
top-level statements: their count is then exact, any other placement is refused with its line.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize

ROOTS = ("tools", "mcp", "commands")
LEADING = re.compile(r"(?<![\w.])codemode_sdk\d+_(?=(?:tools|mcp|commands)\b)")  # a module that leads a name


def prefix(generation: int) -> str:
    return "codemode_sdk%d_" % generation if generation else ""


def plain(text: str) -> str:
    """``text`` with the generation prefix taken off every leading SDK module name only: a tool or
    server whose own name holds such text (``tools.codemode_sdk3_fetch``) keeps it."""
    return LEADING.sub("", text)


def rewrite(code: str, prefix: str) -> str:
    """``code`` with every import of an SDK module renamed to its ``prefix`` name. Only the root
    module token is replaced, found by its source position, so spaces around a dot and line
    continuations inside the import keep working. A source whose tokens and syntax tree disagree
    (an identifier spelled with compatibility characters) is left as it is: Monty then reports the
    import it cannot resolve, with its line."""
    if not prefix:
        return code
    try:
        return _rewrite(code, prefix)
    except (SyntaxError, ValueError, IndexError, StopIteration, tokenize.TokenError):
        return code


def _rewrite(code: str, prefix: str) -> str:
    tree = ast.parse(code)
    names = [(t.start, t.string) for t in tokenize.generate_tokens(io.StringIO(code).readline)
             if t.type == tokenize.NAME]
    lines = code.split("\n")
    edits: list[tuple[int, int, int, str]] = []  # line, start char, end char, text

    def root_at(root: str, line: int, byte_col: int) -> None:
        start = (line, _chars(lines, line, byte_col))
        at = next(pos for pos, text in names if text == root and pos >= start)
        edits.append((at[0], at[1], at[1] + len(root), prefix + root))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root not in ROOTS:
                    continue
                root_at(root, a.lineno, a.col_offset)
                if not a.asname and "." not in a.name:  # bind the usual name; the prelude binds it for `import mcp.x`
                    line = a.end_lineno or a.lineno
                    end = _chars(lines, line, a.end_col_offset or 0)
                    edits.append((line, end, end, " as %s" % root))
        elif isinstance(node, ast.ImportFrom) and not node.level and (node.module or "").split(".")[0] in ROOTS:
            root_at((node.module or "").split(".")[0], node.lineno, node.col_offset)
    for line, start, stop, text in sorted(edits, reverse=True):
        lines[line - 1] = lines[line - 1][:start] + text + lines[line - 1][stop:]
    return "\n".join(lines)


def _chars(lines: list[str], line: int, byte_col: int) -> int:
    """The character column of an ast byte offset."""
    return len(lines[line - 1].encode()[:byte_col].decode("utf-8", "replace"))


def _names_sdk(node: ast.AST) -> int:
    """How many SDK modules an import statement names (0 for any other node)."""
    if isinstance(node, ast.Import):
        return sum(1 for a in node.names if a.name.split(".")[0] in ROOTS)
    if isinstance(node, ast.ImportFrom) and not node.level and (node.module or "").split(".")[0] in ROOTS:
        return 1
    return 0


def sdk_imports(code: str) -> int:
    """Suspensions the script's top-level SDK imports take besides its calls."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return 0  # Monty reports it with its line
    return sum(_names_sdk(node) for node in tree.body)


def nested_import(code: str) -> str | None:
    """Refusal for an SDK import that is not a top-level statement, with its line."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return None
    top = {id(node) for node in tree.body}
    lines = sorted(node.lineno for node in ast.walk(tree)
                   if isinstance(node, (ast.Import, ast.ImportFrom)) and _names_sdk(node) and id(node) not in top)
    if not lines:
        return None
    return ("line %d: import SDK modules (tools, mcp, commands) at the top level of the script, not inside "
            "a function, loop or block" % lines[0])
