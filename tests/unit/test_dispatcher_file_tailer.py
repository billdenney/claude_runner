"""Unit tests for the file tailer that backs adopted/file-backed workers.

``runner.dispatcher.tail_lines`` is the isolated, unit-testable line
source for the ADR-0025 file-backed worker path (the analogue of
``_read_lines`` for the legacy pipe path). It must:

* yield only complete, newline-terminated lines (trailing ``\\n`` kept);
* buffer a partial trailing line until its newline arrives;
* poll (sleep, no busy-spin) while the producer is alive and quiet;
* do exactly one final drain of complete lines once the producer is
  gone, then stop — never hang, never drop the producer's final flush;
* never emit a half-written (un-terminated) final line.

These tests drive the tailer with an injected ``read_fn`` (an in-memory
file model) and an injected ``sleep_fn`` (records waits, advances the
model), so no real files, threads, or wall-clock sleeps are involved and
the assertions are exact.
"""

from __future__ import annotations

from pathlib import Path

from claude_task_runner.runner.dispatcher import tail_lines


class _FakeFile:
    """In-memory append-only file model for the tailer.

    ``content`` is the bytes-as-text written so far; ``alive`` flips to
    False to model the producer exiting. ``read(offset)`` returns the
    slice from ``offset`` to end plus the new offset, mirroring
    ``_read_from_offset``'s contract.
    """

    def __init__(self) -> None:
        self.content = ""
        self.alive = True
        self.reads = 0
        self.sleeps = 0

    def read(self, _path: Path, offset: int) -> tuple[str, int]:
        self.reads += 1
        chunk = self.content[offset:]
        return chunk, len(self.content)

    def append(self, text: str) -> None:
        self.content += text

    def is_alive(self) -> bool:
        return self.alive


def _drain(tailer: object) -> list[str]:
    """Collect a (finite) tailer to a list."""
    return list(tailer)  # type: ignore[call-overload]


def test_yields_only_complete_lines_and_buffers_partial() -> None:
    """A partial trailing line (no newline) is buffered until its newline
    arrives on a later read; complete lines are yielded immediately."""
    fake = _FakeFile()
    # Producer writes one complete line and the start of a second.
    fake.content = 'a\n{"partial":'

    sleeps = {"n": 0}

    def sleep_fn(_s: float) -> None:
        sleeps["n"] += 1
        # On the first quiet poll, finish the partial line and exit.
        if sleeps["n"] == 1:
            fake.append("true}\n")
            fake.alive = False

    lines = _drain(
        tail_lines(
            Path("x"),
            alive=fake.is_alive,
            sleep_fn=sleep_fn,
            read_fn=fake.read,
        )
    )

    assert lines == ["a\n", '{"partial":true}\n']
    # Exactly one quiet sleep happened (then the producer died).
    assert sleeps["n"] == 1


def test_final_drain_after_producer_dies_then_stops() -> None:
    """Once ``alive()`` is False the tailer reads one last time, emits the
    remaining complete lines, and returns (does not hang)."""
    fake = _FakeFile()
    fake.content = "one\ntwo\n"
    # Producer is already gone before the first poll: the very first read
    # is the final drain.
    fake.alive = False

    def sleep_fn(_s: float) -> None:  # pragma: no cover - must never run
        raise AssertionError("tailer must not sleep once producer is dead")

    lines = _drain(tail_lines(Path("x"), alive=fake.is_alive, sleep_fn=sleep_fn, read_fn=fake.read))

    assert lines == ["one\n", "two\n"]
    # One read (the final drain), no sleeps.
    assert fake.reads == 1
    assert fake.sleeps == 0


def test_unterminated_final_line_is_dropped() -> None:
    """A half-written final line (producer died mid-write, no newline) is
    NOT emitted — parsing it could yield a spurious event."""
    fake = _FakeFile()
    fake.content = 'done\n{"half":'  # second line never terminated
    fake.alive = False

    lines = _drain(
        tail_lines(Path("x"), alive=fake.is_alive, sleep_fn=lambda _s: None, read_fn=fake.read)
    )

    assert lines == ["done\n"]


def test_does_not_hang_when_quiet_then_dies() -> None:
    """A producer that stays alive and quiet causes polling sleeps, and
    the loop terminates promptly once it dies — proving no infinite hang
    and no busy-spin (each quiet iteration sleeps exactly once)."""
    fake = _FakeFile()
    fake.content = "first\n"

    sleeps: list[float] = []

    def sleep_fn(s: float) -> None:
        sleeps.append(s)
        # Stay quiet for three polls, then write a final line and die.
        if len(sleeps) == 3:
            fake.append("last\n")
            fake.alive = False

    lines = _drain(
        tail_lines(
            Path("x"),
            alive=fake.is_alive,
            poll_interval_s=0.25,
            sleep_fn=sleep_fn,
            read_fn=fake.read,
        )
    )

    assert lines == ["first\n", "last\n"]
    # Three quiet polls, each a single sleep of the configured interval.
    assert sleeps == [0.25, 0.25, 0.25]


def test_multiple_lines_in_one_read() -> None:
    """Several newline-terminated lines arriving in a single read are all
    emitted in order."""
    fake = _FakeFile()
    fake.content = "a\nb\nc\n"
    fake.alive = False

    lines = _drain(
        tail_lines(Path("x"), alive=fake.is_alive, sleep_fn=lambda _s: None, read_fn=fake.read)
    )
    assert lines == ["a\n", "b\n", "c\n"]


def test_incremental_writes_across_polls_read_each_byte_once() -> None:
    """Bytes appended between polls are read exactly once (offset tracked),
    so no line is duplicated across iterations."""
    fake = _FakeFile()
    fake.content = "l1\n"

    state = {"step": 0}

    def sleep_fn(_s: float) -> None:
        state["step"] += 1
        if state["step"] == 1:
            fake.append("l2\n")
        elif state["step"] == 2:
            fake.append("l3\n")
            fake.alive = False

    lines = _drain(tail_lines(Path("x"), alive=fake.is_alive, sleep_fn=sleep_fn, read_fn=fake.read))
    assert lines == ["l1\n", "l2\n", "l3\n"]


def test_real_file_round_trip(tmp_path: Path) -> None:
    """The default ``read_fn`` (real file, byte offset) round-trips a file
    that is fully written before the tailer starts."""
    log = tmp_path / "attempt-1.stream.jsonl"
    log.write_text("x\ny\n")

    # alive() False from the start ⇒ single drain of the existing file.
    lines = list(tail_lines(log, alive=lambda: False, sleep_fn=lambda _s: None))
    assert lines == ["x\n", "y\n"]


def test_missing_file_polled_until_producer_dies(tmp_path: Path) -> None:
    """A not-yet-created log reads as empty and is polled for; once the
    producer dies with the file still absent, the tailer stops with no
    lines (the default read_fn maps FileNotFoundError → empty)."""
    log = tmp_path / "never-created.stream.jsonl"
    alive = {"v": True}
    sleeps = {"n": 0}

    def sleep_fn(_s: float) -> None:
        sleeps["n"] += 1
        if sleeps["n"] == 2:
            alive["v"] = False

    lines = list(tail_lines(log, alive=lambda: alive["v"], sleep_fn=sleep_fn))
    assert lines == []
    assert sleeps["n"] == 2
