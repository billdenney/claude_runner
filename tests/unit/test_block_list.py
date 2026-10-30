"""``runner.block_list.blocked_task_ids``: the task ids the dispatch
block-list blocks (ADR-0029), read the same way by the selector and the
terminal-close gate (ADR-0033)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_task_runner.runner import terminal_gate
from claude_task_runner.runner.block_list import blocked_task_ids

BLOCK_FILE = "needs_acquisition.jsonl"
LOGGER = "claude_task_runner.runner.block_list"


def _block(task: str, **extra: object) -> bytes:
    return json.dumps({"task": task, "block_dispatch": True, **extra}).encode()


def _write(queue_dir: Path, *rows: bytes) -> Path:
    path = queue_dir / BLOCK_FILE
    path.write_bytes(b"\n".join(rows) + b"\n")
    return path


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


def test_off_when_unset(tmp_path: Path) -> None:
    """No configured block file: the feature is off, whatever is on disk."""
    _write(tmp_path, _block("t1"))
    assert blocked_task_ids(tmp_path, None) == set()
    assert blocked_task_ids(tmp_path, "") == set()


def test_a_missing_file_blocks_nothing(tmp_path: Path) -> None:
    assert blocked_task_ids(tmp_path, BLOCK_FILE) == set()


def test_a_path_no_file_can_have_blocks_nothing(tmp_path: Path) -> None:
    """A NUL byte in the configured path reads as an unreadable file."""
    assert blocked_task_ids(tmp_path, "needs\x00acquisition.jsonl") == set()


def test_only_flagged_task_rows_block(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Of a mixed block-list, only object rows with ``block_dispatch`` true
    and a non-empty string ``task`` block. An explicit ``false``, an index
    row, a ``target_path`` row, a flagged row with no ``task`` and a blank
    line are passed over without a word; a line that is not JSON with a
    WARNING."""
    path = _write(
        tmp_path,
        _block("211-goulooze", needed="suppl"),
        _block("400-metrum"),
        json.dumps({"task": "999-cleared", "block_dispatch": False}).encode(),
        json.dumps({"title": "some paper", "status": "INDEX_NEEDS_ACQUISITION"}).encode(),
        json.dumps({"target_path": "papers/PMID_X/PMID_X.pdf"}).encode(),
        json.dumps({"block_dispatch": True}).encode(),
        b"not valid json at all",
        b"",
    )

    with caplog.at_level("WARNING", logger=LOGGER):
        assert blocked_task_ids(tmp_path, BLOCK_FILE) == {"211-goulooze", "400-metrum"}

    assert _warnings(caplog) == [
        f"dispatch block-list {path} line 7 is not JSON "
        "(Expecting value: line 1 column 1 (char 0)); it blocks nothing"
    ]


def test_a_byte_that_is_not_utf8_reads_as_a_replacement_character(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """In a note the row still blocks; in the task id it blocks no real
    task; in the JSON's structure the line is not JSON, and the WARNING
    names its line. The rows around them still count."""
    path = _write(
        tmp_path,
        _block("in-a-note", note="x").replace(b'"x"', b'"a\xffb"'),
        _block("in-the-id").replace(b"in-the-id", b"in-the\xff-id"),
        _block("in-the-structure").replace(b'"block_dispatch":', b'"block_dispatch"\xff:'),
        _block("after"),
    )

    with caplog.at_level("WARNING", logger=LOGGER):
        blocked = blocked_task_ids(tmp_path, BLOCK_FILE)

    assert blocked == {"in-a-note", "in-the�-id", "after"}
    [warning] = _warnings(caplog)
    assert warning.startswith(f"dispatch block-list {path} line 3 is not JSON (")


def test_a_line_is_warned_about_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The selector reads the file on every tick; a damaged line is named
    once. Changed, it is a new line, named again."""
    path = tmp_path / BLOCK_FILE
    path.write_text("{not json\n", encoding="utf-8")

    with caplog.at_level("WARNING", logger=LOGGER):
        blocked_task_ids(tmp_path, BLOCK_FILE)
        blocked_task_ids(tmp_path, BLOCK_FILE)
        path.write_text("{still not json\n", encoding="utf-8")
        blocked_task_ids(tmp_path, BLOCK_FILE)

    assert [w.split(" (")[0] for w in _warnings(caplog)] == [
        f"dispatch block-list {path} line 1 is not JSON",
        f"dispatch block-list {path} line 1 is not JSON",
    ]


def test_a_byte_order_mark_does_not_drop_the_first_row(tmp_path: Path) -> None:
    (tmp_path / BLOCK_FILE).write_bytes(b"\xef\xbb\xbf" + _block("first") + b"\n")
    assert blocked_task_ids(tmp_path, BLOCK_FILE) == {"first"}


def test_the_terminal_gate_reads_what_the_selector_reads(tmp_path: Path) -> None:
    """already_gated agrees with blocked_task_ids for every id, damaged rows
    included."""
    _write(
        tmp_path,
        _block("blocked"),
        json.dumps({"task": "cleared", "block_dispatch": False}).encode(),
        b"\xff{}",
        _block("damaged").replace(b"damaged", b"dam\xffaged"),
    )
    blocked = blocked_task_ids(tmp_path, BLOCK_FILE)

    for task_id in ("blocked", "cleared", "damaged", "dam�aged", "absent"):
        assert terminal_gate.already_gated(tmp_path, BLOCK_FILE, task_id) is (task_id in blocked)
