"""Result schemas of the stock tools scripts use most, so their stubs return typed dicts (REQ-010).

Shapes follow Hermes's tools/file_operations_common.py (read_file, search_files, patch) and
terminal_tool.py (terminal). Keys are optional: a tool fills only the ones that apply to the call.
"""

from __future__ import annotations

from . import sdk

_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}
_STRS = {"type": "array", "items": _STR}


def _described(schema: dict, text: str) -> dict:
    return {**schema, "description": text}


READ_FILE = {
    "type": "object",
    "properties": {
        "content": _described(_STR, "each line is prefixed '<n>|'"),
        "total_lines": _INT,
        "file_size": _INT,
        "truncated": _BOOL,
        "hint": _STR,
        "error": _STR,
        "not_found": _BOOL,
        "similar_files": _STRS,
    },
}

SEARCH_FILES = {
    "type": "object",
    "properties": {
        "total_count": _INT,
        "matches": {
            "type": "array",
            "items": {"type": "object", "properties": {"path": _STR, "line": _INT, "content": _STR}},
        },
        "matches_text": _STR,
        "matches_format": _STR,
        "files": _STRS,
        "counts": {"type": "object"},
        "truncated": _BOOL,
        "warning": _STR,
        "error": _STR,
    },
}

TERMINAL = {
    "type": "object",
    "properties": {
        "output": _STR,
        "exit_code": {"type": ["integer", "null"]},  # None while a command runs on in the background
        "error": {"type": ["string", "null"]},
        "status": _STR,
        "session_id": _STR,  # a command handed to the background: the prelude's sh() reads it
        "pid": _INT,
        "promoted_from_foreground": _STR,
        "notify_on_complete": _BOOL,
    },
}

PATCH = {
    "type": "object",
    "properties": {
        "success": _BOOL,
        "diff": _STR,
        "files_modified": _STRS,
        "files_created": _STRS,
        "files_deleted": _STRS,
        "error": _STR,
        "no_change": _BOOL,
        "note": _STR,
    },
}

BUILTIN = {"read_file": READ_FILE, "search_files": SEARCH_FILES, "terminal": TERMINAL, "patch": PATCH}


def register_builtin() -> None:
    """Declare the result schemas above; a tool another plugin already declared keeps its schema."""
    for tool, schema in BUILTIN.items():
        if tool not in sdk.RESULT_SCHEMAS:
            sdk.register_result_schema(tool, schema)
