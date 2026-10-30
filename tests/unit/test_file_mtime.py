"""``file_mtime``: a file's mtime as an aware UTC datetime. One no datetime
can hold is ``None``, and is reported once per path, mtime and consequence:
the silent-orphan reaper reads the same files on every pass."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_task_runner.file_mtime import file_mtime_utc

LOGGER = "claude_task_runner.file_mtime"


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]


def test_an_mtime_is_an_aware_utc_datetime(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_text("x")
    os.utime(path, (1_790_000_000, 1_790_000_000))

    got = file_mtime_utc(path, consequence="unused")

    assert got == datetime(2026, 9, 21, 14, 13, 20, tzinfo=UTC)


def test_a_file_that_cannot_be_stated_is_none_without_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Missing, or a NUL byte in the path, which no file has."""
    with caplog.at_level("WARNING", logger=LOGGER):
        assert file_mtime_utc(tmp_path / "missing", consequence="unused") is None
        assert file_mtime_utc(f"{tmp_path}/nul\x00byte", consequence="unused") is None

    assert _warnings(caplog) == []


def test_an_mtime_no_datetime_can_hold_is_reported_once(
    far_future_mtime_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Read again, the same mtime is not reported again; a new mtime past
    year 9999 on the same file is."""
    path = far_future_mtime_dir / "attempt-1.stream.jsonl"
    path.write_text("{}\n")
    os.utime(path, (253402300800, 253402300800))

    with caplog.at_level("WARNING", logger=LOGGER):
        first = file_mtime_utc(path, consequence="dating the run at now")
        again = file_mtime_utc(path, consequence="dating the run at now")
        os.utime(path, (253402300801, 253402300801))
        moved = file_mtime_utc(path, consequence="dating the run at now")

    assert (first, again, moved) == (None, None, None)
    assert _warnings(caplog) == [
        f"{path} has an mtime no datetime can hold (timestamp 253402300800.0 is out of range: "
        "year 10000 is out of range); dating the run at now",
        f"{path} has an mtime no datetime can hold (timestamp 253402300801.0 is out of range: "
        "year 10000 is out of range); dating the run at now",
    ]


def test_an_mtime_past_time_t_is_none_with_a_warning(
    tmp_path: Path,
    stub_mtime: Callable[[Path, float], None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``OverflowError`` from the conversion is reported like the year
    past 9999, without needing tmpfs."""
    path = tmp_path / "attempt-1.stream.jsonl"
    stub_mtime(path, 1e20)

    with caplog.at_level("WARNING", logger=LOGGER):
        assert file_mtime_utc(path, consequence="skipping it") is None

    assert _warnings(caplog) == [
        f"{path} has an mtime no datetime can hold (timestamp 1e+20 is out of range: "
        "timestamp out of range for platform time_t); skipping it"
    ]


def test_each_consequence_is_reported_once(
    tmp_path: Path,
    stub_mtime: Callable[[Path, float], None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two callers that do different things with the same file each say so,
    once: the reaper's warning does not silence the dispatcher's."""
    path = tmp_path / "attempt-1.stream.jsonl"
    stub_mtime(path, 1e20)

    with caplog.at_level("WARNING", logger=LOGGER):
        for consequence in ("not counting it", "dating the run at now", "not counting it"):
            assert file_mtime_utc(path, consequence=consequence) is None

    out_of_range = (
        f"{path} has an mtime no datetime can hold (timestamp 1e+20 is out of range: "
        "timestamp out of range for platform time_t)"
    )
    assert _warnings(caplog) == [
        f"{out_of_range}; not counting it",
        f"{out_of_range}; dating the run at now",
    ]
