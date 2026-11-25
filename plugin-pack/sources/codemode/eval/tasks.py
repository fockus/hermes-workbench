"""The eval task set (spec Task 11, REQ-029): twelve file tasks with reference checks, each run in
its own fresh work folder. ``short`` tasks need one tool call, ``bulk_read`` tasks read or filter
many files, ``bulk_write`` tasks write in a loop. Fixture data is fixed, so every run of a task
sees the same files and its check has one right outcome.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class EvalTask:
    id: str
    cls: str
    question: str  # ``{wd}`` is the run's work folder
    setup: Callable[[Path], object]  # what it returns is ignored
    check: Callable[[str, Path], bool]  # (final answer, work folder) -> accepted

    def prompt(self, workdir: Path) -> str:
        return self.question.format(wd=workdir)


def _number(answer: str, wd: Path, value: int) -> bool:
    """The answer gives the number and no other one (the work folder's path left out). ``1,429`` and
    ``1429.0`` are the number 1429; ``1429 of 5000`` names a second one."""
    text = re.sub(r"(?<![\w.,])(\d{1,3}(?:,\d{3})+)(?![\w,])", lambda m: m.group(1).replace(",", ""),
                  (answer or "").replace(str(wd), " "))
    found = re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?(?!\w|\.\d)", text)
    return bool(found) and {re.sub(r"\.0+$", "", n) for n in found} == {str(value)}


def _name(answer: str, wd: Path, value: str, files: dict[str, str]) -> bool:
    """The answer names the file ``value`` (whole, or its stem alone) and no other file: every file
    name with a suffix in it must be exactly ``value``, no other stem of the task may appear (the
    work folder's path left out)."""
    text = (answer or "").replace(str(wd), " ")
    named = re.findall(r"(?<![\w.-])[\w-]+(?:\.[A-Za-z0-9]+)+(?![\w-])", text)  # every suffix: beta.py.bak
    stems = {n.rsplit(".", 1)[0] for n in files}
    said = {w for w in re.findall(r"(?<![\w-])[\w-]+(?![\w-])", text) if w in stems}
    return all(n == value for n in named) and said == {value.rsplit(".", 1)[0]}


def _files(folder: Path, names: dict[str, str]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in names.items():
        (folder / name).write_text(text)


def _read(path: Path) -> str | None:
    return path.read_text() if path.is_file() else None


NUMBERS = {"n%02d.txt" % i: str((i * 37 + 11) % 100) for i in range(20)}


def _log(errors: int) -> str:
    return "".join("INFO tick %d\n" % k for k in range(22)) + "".join(
        "ERROR boom %d\n" % k for k in range(errors)
    )


LOGS = {
    "app%02d.log" % i: _log(20 if i == 17 else (i * 5) % 11) for i in range(30)
}  # app17 has the most
USERS = {
    "u%02d.json" % i: json.dumps(
        {"name": "user-%02d" % i, "age": (i * 13 + 20) % 70 + 10}
    )
    for i in range(16)
}
OLD_USERS = sorted(
    json.loads(t)["name"] for t in USERS.values() if json.loads(t)["age"] > 60
)
DRAFTS = {"d%d.txt" % i: "draft %d\nline two of %d\n" % (i, i) for i in range(10)}
CONFIGS = {
    "c%d.ini" % i: "[app]\nname=svc%d\ndebug=true\nlevel=%d\n" % (i, i)
    for i in range(8)
}
DOCS = {
    "doc-%s.md" % w: "# %s\n" % w
    for w in "kilo alpha juliet bravo india charlie hotel delta golf echo foxtrot "
    "lima mike november oscar".split()
}
SOURCES = {
    "%s.py" % n: "def %s():\n    return %r\n" % (n, "quokka" if n == "beta" else n)
    for n in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta")
}


def _rows(workdir: Path) -> None:
    rows = ["id,status"] + [
        "%d,%s" % (i, "failed" if i % 7 == 3 else "ok") for i in range(5000)
    ]
    (workdir / "big.csv").write_text("\n".join(rows) + "\n")


def _backups_ok(wd: Path) -> bool:
    return all(_read(wd / "drafts" / (n + ".bak")) == t for n, t in DRAFTS.items())


def _debug_off(wd: Path) -> bool:
    return all(
        _read(wd / "cfg" / n) == t.replace("debug=true", "debug=false")
        for n, t in CONFIGS.items()
    )


def _index_ok(wd: Path) -> bool:
    text = _read(wd / "index.txt")
    return text is not None and [
        ln.strip() for ln in text.splitlines() if ln.strip()
    ] == sorted(DOCS)


def _old_users_ok(answer: str, _wd: Path) -> bool:
    """Exactly the old users, each once, in sorted order."""
    return re.findall(r"(?<![\w-])user-\d+(?![\w-])", answer or "") == OLD_USERS


TASK_SET: tuple[EvalTask, ...] = (
    EvalTask(
        "s-read-port",
        "short",
        "What is the port in {wd}/config.ini? Answer with the number only.",
        lambda wd: (wd / "config.ini").write_text(
            "[server]\nhost=localhost\nport=8431\n"
        ),
        lambda a, wd: _number(a, wd, 8431),
    ),
    EvalTask(
        "s-find-word",
        "short",
        "Which file in {wd}/src contains the word quokka? Answer with the file name.",
        lambda wd: _files(wd / "src", SOURCES),
        lambda a, wd: _name(a, wd, "beta.py", SOURCES),
    ),
    EvalTask(
        "s-write-hello",
        "short",
        "Create the file {wd}/hello.txt containing exactly: hello eval",
        lambda wd: None,
        lambda a, wd: (_read(wd / "hello.txt") or "").strip() == "hello eval",
    ),
    EvalTask(
        "s-line-count",
        "short",
        "How many lines does {wd}/notes.txt have? Answer with the number only.",
        lambda wd: (wd / "notes.txt").write_text(
            "".join("note %d\n" % i for i in range(37))
        ),
        lambda a, wd: _number(a, wd, 37),
    ),
    EvalTask(
        "b-sum-numbers",
        "bulk_read",
        "Each file in {wd}/data holds one integer. What is their sum? Answer with the number only.",
        lambda wd: _files(wd / "data", NUMBERS),
        lambda a, wd: _number(a, wd, sum(int(v) for v in NUMBERS.values())),
    ),
    EvalTask(
        "b-most-errors",
        "bulk_read",
        "Which log file in {wd}/logs has the most ERROR lines? Answer with its name.",
        lambda wd: _files(wd / "logs", LOGS),
        lambda a, wd: _name(a, wd, "app17.log", LOGS),
    ),
    EvalTask(
        "b-failed-rows",
        "bulk_read",
        "How many rows of {wd}/big.csv have the status failed? Answer with the number only.",
        _rows,
        lambda a, wd: _number(a, wd, sum(1 for i in range(5000) if i % 7 == 3)),
    ),
    EvalTask(
        "b-old-users",
        "bulk_read",
        "Each JSON file in {wd}/users has a name and an age. List the names of the "
        "users older than 60, sorted, comma separated.",
        lambda wd: _files(wd / "users", USERS),
        _old_users_ok,
    ),
    EvalTask(
        "w-backup-drafts",
        "bulk_write",
        "For every file in {wd}/drafts write a copy named <file name>.bak "
        "next to it with the same contents.",
        lambda wd: _files(wd / "drafts", DRAFTS),
        lambda a, wd: _backups_ok(wd),
    ),
    EvalTask(
        "w-squares",
        "bulk_write",
        "For n from 1 to 12 write the file {wd}/out/<n>.txt containing n squared.",
        lambda wd: None,
        lambda a, wd: all(
            (_read(wd / "out" / ("%d.txt" % n)) or "").strip() == str(n * n)
            for n in range(1, 13)
        ),
    ),
    EvalTask(
        "w-debug-off",
        "bulk_write",
        "In every file in {wd}/cfg replace debug=true with debug=false and change "
        "nothing else.",
        lambda wd: _files(wd / "cfg", CONFIGS),
        lambda a, wd: _debug_off(wd),
    ),
    EvalTask(
        "w-index",
        "bulk_write",
        "Write {wd}/index.txt listing the name of every file in {wd}/docs, one per "
        "line, sorted.",
        lambda wd: _files(wd / "docs", DOCS),
        lambda a, wd: _index_ok(wd),
    ),
)


def by_id(task_id: str) -> EvalTask:
    """A task of the twelve-task protocol or of the big class."""
    from .big_tasks import BIG_TASKS  # big_tasks builds on this module

    for task in (*TASK_SET, *BIG_TASKS):
        if task.id == task_id:
            return task
    raise KeyError(task_id)
