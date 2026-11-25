"""Bounded capture of a script's print output (used by repl.py; REQ-015, REQ-032).

Everything is kept up to ``cap`` characters, so result shaping redacts the whole output at once and
spills it complete. Past the cap only the head is kept, cut back to its last complete record (a
line, or a whole private-key block), with the count of the characters that were not kept; they are
in no spill either. A cut can then only separate a value from a key that comes before it, never
keep a value without its key (``DB_PASSWORD=`` on the far side of the cut), so redaction never
sees a secret out of context. A callback never retains more than the room left under the cap.
"""

from __future__ import annotations

BEGIN, END = "-----BEGIN", "-----END"  # a multi-line private-key block is one record


def _whole_head(text: str) -> str:
    """``text`` up to its last complete record."""
    text = text[: text.rfind("\n") + 1]
    begin = text.rfind(BEGIN)
    if begin > text.rfind(END):  # ends inside an unfinished block
        text = text[: text.rfind("\n", 0, begin) + 1]
    return text


class Printed:
    """Print output up to ``cap`` characters (see the module docstring); ``text()`` gives it raw,
    for result shaping to redact whole."""

    def __init__(self, cap: int):
        self.cap = cap
        self.head: list[str] = []
        self.head_len = self.dropped = 0

    def __call__(self, _stream: str, text: str) -> None:
        take = text[: self.cap - self.head_len]
        if take:
            self.head.append(take)
            self.head_len += len(take)
        self.dropped += len(text) - len(take)

    def text(self) -> str:
        head = "".join(self.head)
        if not self.dropped:
            return head
        kept = _whole_head(head)
        lost = self.dropped + len(head) - len(kept)
        return (
            "%s\n[codemode: %d characters of print output after this point were not kept; they are not in "
            "the spill either]\n" % (kept, lost)
        )
