"""``dispatcher._PipeLines``: a worker's stdout pipe as lines, with a tick
when none comes, for the dispatch loop's time caps on the pipe path."""

from __future__ import annotations

import os
from typing import IO

from claude_task_runner.runner.dispatcher import _PipeLines


def _pipe() -> tuple[IO[str], int]:
    """A pipe's read end as the text stream ``Popen`` would make, and its
    write end's descriptor."""
    read_fd, write_fd = os.pipe()
    return open(read_fd, encoding="utf-8", errors="replace"), write_fd


def test_lines_a_last_line_without_its_newline_and_bytes_that_are_not_utf8() -> None:
    stream, write_fd = _pipe()
    os.write(write_fd, b'{"a": 1}\n\xff\xfe\nlast')
    os.close(write_fd)

    with stream:
        assert list(_PipeLines(stream, tick_s=None)) == ['{"a": 1}\n', "��\n", "last"]


def test_a_tick_when_no_line_comes_in_tick_s() -> None:
    stream, write_fd = _pipe()
    lines = iter(_PipeLines(stream, tick_s=0.05))
    try:
        assert next(lines) is None
        os.write(write_fd, b"x\n")
        assert next(lines) == "x\n"
    finally:
        os.close(write_fd)
        stream.close()


def test_unread_is_what_it_read_and_did_not_yield() -> None:
    """One read takes all three lines; the loop takes the first. The rest
    is unread, once."""
    stream, write_fd = _pipe()
    os.write(write_fd, b"one\ntwo\nthr")
    reader = _PipeLines(stream, tick_s=None)
    try:
        assert next(iter(reader)) == "one\n"
        assert (reader.unread(), reader.unread()) == ("two\nthr", "")
    finally:
        os.close(write_fd)
        stream.close()
