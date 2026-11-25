"""Saved workflows (REQ-006, REQ-016): ``<name>.js`` files looked up in six places, first match wins.

Public names for WE-WIRE: ``dirs(cwd, hermes_home=None, home=None)`` (the lookup order), ``find(name, cwd)``
(the first regular file, or None for an unsafe or unknown name), ``names(cwd)`` (one ``/<name>`` command per
entry) and ``source(name, cwd)`` (the runner's ``load`` reply, ``{ok, script}`` or ``{ok: False, message}``).
"""

from __future__ import annotations

from pathlib import Path

from .store import runs_root

TEMPLATES = Path(__file__).resolve().parent / "templates"
SUFFIX = ".js"


def dirs(cwd, hermes_home=None, home=None) -> list[Path]:
    """The lookup order: project .hermes, .agents, .claude, then $HERMES_HOME, ~/.claude, the plugin templates."""
    project = Path(cwd)
    hermes = Path(hermes_home) / "workflows" if hermes_home else runs_root().parent
    user = Path(home) if home else Path.home()
    return [project / ".hermes" / "workflows", project / ".agents" / "workflows", project / ".claude" / "workflows",
            hermes, user / ".claude" / "workflows", TEMPLATES]


def _safe(name: str) -> bool:
    return bool(name) and not name.startswith(".") and not any(c in name for c in "/\\\0")


def find(name: str, cwd) -> Path | None:
    if not isinstance(name, str) or not _safe(name):
        return None
    for folder in dirs(cwd):
        path = folder / (name + SUFFIX)
        if path.is_file():
            return path
    return None


def names(cwd) -> list[str]:
    found: set[str] = set()
    for folder in dirs(cwd):
        if folder.is_dir():
            found.update(p.stem for p in folder.glob("*" + SUFFIX) if p.is_file() and _safe(p.stem))
    return sorted(found)


def source(name: str, cwd) -> dict:
    path = find(name, cwd)
    if path is None:
        return {"ok": False, "message": "no saved workflow named %s" % name}
    try:
        return {"ok": True, "script": path.read_text(encoding="utf-8")}
    except UnicodeDecodeError:
        return {"ok": False, "message": "saved workflow %s is not UTF-8 text" % name}
    except OSError as exc:
        return {"ok": False, "message": "cannot read saved workflow %s: %s" % (name, exc.strerror or exc)}
