"""When a register line counts as kept: one rule for the revert check and the line check.

verify_no_base_reverts.py asks whether a line of the base survives in the merge
result; verify_added_lines.py asks the same of a line a branch added. Both
compare a line with the lines of the matching ``###`` block of the result (each
check finds that block its own way) and both answer with :class:`Block`:

* a field line (``- **Notes:** ...``, at any indent) is kept when the block
  has it, or when one line of the same field there holds each of its
  sentences, whole: a hand union that joins two branches' sentences into one
  Notes line keeps both, and a sentence that is only a prefix of another
  (``Fixed.`` in ``Fixed-effect on clearance.``) is not one of them;
* every other line, a heading included, is kept only when the block has it
  word for word.

Within a line, a run of whitespace counts as one space; trailing whitespace
is ignored, and the indent counts, so a sub-bullet is not its parent bullet.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable

# A field line, ``- **Label:** text``, at any indent.
FIELD_LINE = re.compile(r"^\s*[-*]\s+\*\*([^*]+?):\*\*(.*)$")
# Where a sentence ends and the next starts.
SENTENCE_BREAK = re.compile(r"(?<=[.;!?])\s+")


def _norm(text: str) -> str:
    return " ".join(text.split())


def _line(line: str) -> str:
    """``line`` with its indent and with every other whitespace run as one space."""
    body = line.lstrip()
    return line[: len(line) - len(body)] + _norm(body)


def field_of(line: str) -> tuple[str, str] | None:
    """``(label, text)`` of a field line, whitespace normalised; None otherwise."""
    match = FIELD_LINE.match(line)
    if match is None:
        return None
    return match.group(1).strip(), _norm(match.group(2))


def sentences(text: str) -> frozenset[str]:
    """The sentences of ``text``, whitespace normalised."""
    return frozenset(piece for piece in SENTENCE_BREAK.split(_norm(text)) if piece)


class Block:
    """The lines of one block of a file, to ask whether a line is kept there."""

    def __init__(self, lines: Iterable[str] = ()) -> None:
        self.exact: set[str] = set()
        self.fields: defaultdict[str, list[frozenset[str]]] = defaultdict(list)
        self.add(lines)

    def add(self, lines: Iterable[str]) -> None:
        for line in lines:
            if not line.strip():
                continue
            self.exact.add(_line(line))
            found = field_of(line)
            if found is not None:
                label, text = found
                self.fields[label].append(sentences(text))

    def holds(self, line: str) -> bool:
        """Whether ``line`` is kept in this block (see the module docstring)."""
        if _line(line) in self.exact:
            return True
        found = field_of(line)
        if found is None:
            return False
        label, text = found
        wanted = sentences(text)
        return bool(wanted) and any(wanted <= held for held in self.fields.get(label, ()))
