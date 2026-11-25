"""JSON schema to Python stub text for the code-mode SDK (spec Task 5, REQ-006, REQ-010).

Pure text generation: the stubs are type-checked inside Monty (ty), never compiled by the host.
"""

from __future__ import annotations

import keyword
import re
from dataclasses import dataclass

SCALARS = {"string": "str", "integer": "int", "number": "float", "boolean": "bool", "null": "None"}
RESERVED = frozenset({"Any", "TypedDict"})  # names the stub header imports
DOC_CHARS = 600


@dataclass(frozen=True)
class Param:
    py: str  # the Python parameter name in the stub
    name: str  # the tool's own argument name
    annotation: str
    required: bool


def identifier(name: str) -> str:
    """A public Python identifier for a tool, server or argument name."""
    out = re.sub(r"\W", "_", name.strip()) or "tool"
    if out[0].isdigit() or out[0] == "_":
        out = "t" + out
    return out + "_" if keyword.iskeyword(out) or out in RESERVED else out


def pascal(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in identifier(name).split("_") if part)


def annotation(schema: object) -> str:
    """The Python type of a JSON schema; anything it cannot express is ``Any``."""
    if not isinstance(schema, dict):
        return "Any"
    for key in ("anyOf", "oneOf"):
        if isinstance(schema.get(key), list):
            return _union([annotation(option) for option in schema[key]])
    kind = schema.get("type")
    if isinstance(kind, list):
        return _union([annotation({**schema, "type": one}) for one in kind])
    if kind == "array":
        return "list[%s]" % annotation(schema.get("items"))
    if kind == "object":
        return "dict[str, Any]"
    return SCALARS.get(kind, "Any") if isinstance(kind, str) else "Any"


def _union(parts: list[str]) -> str:
    unique = list(dict.fromkeys(parts))
    return "Any" if not unique or "Any" in unique else " | ".join(unique)


def params_of(parameters: object) -> tuple[Param, ...]:
    """Stub parameters of a tool's JSON-schema ``parameters``: required ones first, in schema order."""
    if not isinstance(parameters, dict) or not isinstance(parameters.get("properties"), dict):
        return ()
    required = set(parameters.get("required") or [])
    found, used = [], set()
    for name, prop in parameters["properties"].items():
        py, n = identifier(name), 2
        while py in used:  # two arguments that differ only in characters an identifier cannot hold
            py, n = "%s_%d" % (identifier(name), n), n + 1
        used.add(py)
        found.append(Param(py, name, annotation(prop), name in required))
    return tuple(sorted(found, key=lambda p: not p.required))


def function_head(name: str, params: tuple[Param, ...], returns: str) -> str:
    sig = ", ".join("%s: %s%s" % (p.py, p.annotation, "" if p.required else " = ...") for p in params)
    return "async def %s(%s) -> %s:" % (name, sig, returns)


def docstring(text: str) -> str:
    body = " ".join((text or "").split())[:DOC_CHARS].replace('"""', "'''")
    return '\n    """%s"""\n' % body if body else " ...\n"


def _note(prop: object) -> str:
    """A property's ``description`` as a trailing stub comment of at most 80 characters."""
    text = " ".join(str(prop.get("description") or "").split()) if isinstance(prop, dict) else ""
    return "  # " + text[:80] if text else ""


def result_type(class_name: str, schema: object) -> tuple[str, str] | None:
    """The return annotation of a declared result schema and the class text it needs: a scalar or
    array schema is its own annotation; an object schema is a TypedDict whose keys outside
    ``required`` are optional (a ``total=False`` subclass). None when nothing typed can be said
    (no schema, ``Any``, or object keys that are not identifiers)."""
    if not isinstance(schema, dict):
        return None
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        plain = annotation(schema)
        return None if plain in ("Any", "dict[str, Any]") else (plain, "")
    if any(not key.isidentifier() or keyword.iskeyword(key) for key in props):
        return None
    required = set(schema.get("required") or []) & set(props)
    fields: dict[bool, list[str]] = {True: [], False: []}
    for key, value in props.items():
        fields[key in required].append("    %s: %s%s" % (key, annotation(value), _note(value)))
    base = "_%sRequired" % class_name
    lines = ["class %s(TypedDict):" % base] + (fields[True] or ["    pass"])
    lines += ["", "", "class %s(%s, total=False):" % (class_name, base)] + (fields[False] or ["    pass"])
    return class_name, "\n".join(lines) + "\n"
