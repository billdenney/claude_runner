"""Every ADR has a row in the decisions index, and every row has an ADR.

``docs/decisions/README.md`` is the table a reader scans to find a
decision, and all that kept it current was one sentence of prose: copy
the template into the next-numbered file "and add a row to the index".
ADR-0033 landed on 2026-09-04 without its row, so the index silently
stopped at 0032 for three weeks. This test is that sentence, enforced.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
DECISIONS_DIR = REPO_ROOT / "docs" / "decisions"
INDEX = DECISIONS_DIR / "README.md"

_ROW_NUMBER = re.compile(r"^\|\s*(\d{4})\s*\|")
"""The ADR number in an index row's first cell: ``| 0033 | <title> | ...``."""


def _index_numbers(text: str) -> list[str]:
    """ADR numbers from the body rows of the ``## Index`` table, in order.

    Fails loud rather than returning less: a missing ``## Index`` section
    raises (from ``list.index``), and so does a body row whose first cell
    is not a four-digit number -- a row the parser cannot read must fail
    the gate, not quietly drop out of it.
    """
    lines = text.splitlines()
    table: list[str] = []
    for line in lines[lines.index("## Index") + 1 :]:
        if line.startswith("## "):
            break
        if line.startswith("|"):
            table.append(line)
    numbers: list[str] = []
    # A GFM table is a header row and a delimiter row, then the body.
    for row in table[2:]:
        match = _ROW_NUMBER.match(row)
        if match is None:
            raise ValueError(f"ADR index row has no four-digit number: {row!r}")
        numbers.append(match.group(1))
    return numbers


def _adr_files() -> list[Path]:
    return sorted(DECISIONS_DIR.glob("[0-9][0-9][0-9][0-9]-*.md"))


def _number(adr: Path) -> str:
    return adr.name[:4]


_SAMPLE_INDEX = """\
# Architecture Decision Records (ADRs)

## Index

| # | Title | Status | Date |
|---|-------|--------|------|
| 0001 | First | accepted | 2026-05-03 |
| 0002 | Second | superseded by ADR-0003 | 2026-05-13 |

## Template

| # | Title |
|---|-------|
| 0099 | a table outside the index |
"""


class TestIndexParser:
    """The row parser itself -- one that finds nothing would pass everything."""

    def test_reads_only_the_body_rows_of_the_index_table(self) -> None:
        # Header and delimiter rows are skipped, and so is the table
        # under the next section.
        assert _index_numbers(_SAMPLE_INDEX) == ["0001", "0002"]

    def test_unreadable_row_fails_loud(self) -> None:
        with pytest.raises(ValueError, match=r"\| 002 \|"):
            _index_numbers(_SAMPLE_INDEX.replace("| 0002 |", "| 002 |"))

    def test_missing_index_section_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="## Index"):
            _index_numbers(_SAMPLE_INDEX.replace("## Index", "## Decisions"))


class TestIndexMatchesDecisions:
    def test_both_sides_find_the_first_adr(self) -> None:
        # Guards the suite: if either side came back empty, every check
        # below would pass on nothing. ADRs are append-only, so ADR-0001
        # is a known answer on both sides.
        assert "0001" in _index_numbers(INDEX.read_text())
        assert DECISIONS_DIR / "0001-full-rewrite-vs-wrap.md" in _adr_files()

    def test_every_adr_has_an_index_row(self) -> None:
        indexed = set(_index_numbers(INDEX.read_text()))
        missing = [adr for adr in _adr_files() if _number(adr) not in indexed]
        assert not missing, (
            "ADR(s) with no row in docs/decisions/README.md. Add one per ADR,\n"
            "taking the title, status and date from the ADR's header:\n"
            + "\n".join(f"  {adr.relative_to(REPO_ROOT)}" for adr in missing)
        )

    def test_every_index_row_has_an_adr(self) -> None:
        on_disk = {_number(adr) for adr in _adr_files()}
        orphans = [n for n in _index_numbers(INDEX.read_text()) if n not in on_disk]
        assert not orphans, (
            "docs/decisions/README.md has row(s) for ADR number(s) with no "
            f"docs/decisions/NNNN-<slug>.md file: {orphans}"
        )

    def test_no_number_is_used_twice(self) -> None:
        rows = Counter(_index_numbers(INDEX.read_text()))
        files = Counter(_number(adr) for adr in _adr_files())
        twice_rows = sorted(n for n, count in rows.items() if count > 1)
        twice_files = sorted(n for n, count in files.items() if count > 1)
        assert (twice_rows, twice_files) == ([], []), (
            f"ADR number(s) used twice -- index rows: {twice_rows}; files: "
            f"{twice_files}. Two branches can each claim the next free "
            "number; renumber one of them."
        )

    def test_every_decision_file_is_numbered(self) -> None:
        # The checks above see only NNNN-<slug>.md files; an ADR named any
        # other way would escape every one of them.
        numbered = set(_adr_files())
        stray = sorted(
            doc.name for doc in DECISIONS_DIR.glob("*.md") if doc != INDEX and doc not in numbered
        )
        assert not stray, (
            f"docs/decisions/ file(s) not named NNNN-<slug>.md, so the index "
            f"checks cannot see them: {stray}"
        )
