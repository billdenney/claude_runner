"""Tests for usage/source.py — ClaudeUsageSource (mocked capture) + FakeUsageSource.

``ClaudeUsageSource.read()`` calls capture_mod.capture (pexpect-driven,
spawns claude) and then parser_mod.parse. We mock both so the wrapper
itself is tested end-to-end without an external process.

``FakeUsageSource`` is the scripted source used by integration tests
elsewhere; here we cover its empty / exhausted / re-script branches.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.usage.models import UsageReading, WindowReading
from claude_task_runner.usage.source import ClaudeUsageSource, FakeUsageSource


def _make_reading(*, util_5h: int = 20, util_wk: int = 40) -> UsageReading:
    return UsageReading(
        captured_at=datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC),
        five_hour=WindowReading(
            utilization_pct=util_5h,
            resets_at_raw="some time",
            resets_at=datetime(2026, 5, 16, 17, 0, 0, tzinfo=UTC),
        ),
        seven_day=WindowReading(
            utilization_pct=util_wk,
            resets_at_raw="some time",
            resets_at=datetime(2026, 5, 20, 11, 0, 0, tzinfo=UTC),
        ),
    )


# ---------------------------------------------------------------------------
# ClaudeUsageSource
# ---------------------------------------------------------------------------


def test_claude_usage_source_constructor_stores_args(tmp_path: Path) -> None:
    settings = load_settings(None).usage
    src = ClaudeUsageSource(
        settings,
        FakeClock(datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)),
        captures_dir=tmp_path / "caps",
        claude_executable="/custom/path/claude",
        claude_config_dir="/home/user/.claude_alt",
    )
    # Private attributes are intentionally introspected here to confirm
    # they round-trip into capture_mod.capture in the next test.
    assert src._claude_executable == "/custom/path/claude"
    assert src._claude_config_dir == "/home/user/.claude_alt"


def test_claude_usage_source_read_returns_parsed_reading(tmp_path: Path) -> None:
    settings = load_settings(None).usage
    clock = FakeClock(datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC))
    captures_dir = tmp_path / "caps"
    fake_reading = _make_reading(util_5h=33, util_wk=12)
    fake_path = captures_dir / "fixture.cap"

    with (
        patch(
            "claude_task_runner.usage.source.capture_mod.capture",
            return_value=(b"raw bytes", fake_path),
        ),
        patch(
            "claude_task_runner.usage.source.parser_mod.parse",
            return_value=fake_reading,
        ),
    ):
        src = ClaudeUsageSource(
            settings,
            clock,
            captures_dir=captures_dir,
            claude_executable="claude",
            claude_config_dir="",
        )
        result = src.read()

    # capture_path is set from the capture's returned path.
    assert result.five_hour.utilization_pct == 33
    assert result.seven_day.utilization_pct == 12
    assert result.capture_path == str(fake_path)


def test_claude_usage_source_read_propagates_capture_args(tmp_path: Path) -> None:
    """The settings / captures_dir / executable / config_dir handed in at
    construction time must flow through to capture_mod.capture."""
    settings = load_settings(None).usage
    clock = FakeClock(datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC))
    captures_dir = tmp_path / "caps"

    capture_calls: list[dict] = []

    def _spy_capture(s, c, **kw):
        capture_calls.append(kw)
        return (b"raw", tmp_path / "fixture.cap")

    with (
        patch(
            "claude_task_runner.usage.source.capture_mod.capture",
            side_effect=_spy_capture,
        ),
        patch(
            "claude_task_runner.usage.source.parser_mod.parse",
            return_value=_make_reading(),
        ),
    ):
        src = ClaudeUsageSource(
            settings,
            clock,
            captures_dir=captures_dir,
            claude_executable="/custom/claude",
            claude_config_dir="/home/.cdir",
        )
        src.read()

    assert len(capture_calls) == 1
    assert capture_calls[0]["captures_dir"] == captures_dir
    assert capture_calls[0]["claude_executable"] == "/custom/claude"
    assert capture_calls[0]["claude_config_dir"] == "/home/.cdir"


# ---------------------------------------------------------------------------
# FakeUsageSource
# ---------------------------------------------------------------------------


def test_fake_usage_source_rejects_empty() -> None:
    with pytest.raises(ValueError):
        FakeUsageSource([])


def test_fake_usage_source_single_reading_repeats() -> None:
    """Once the script is exhausted, the last reading is returned forever."""
    r = _make_reading()
    src = FakeUsageSource([r])
    assert src.read() is r  # 1st: from iter
    assert src.read() is r  # 2nd: from _last fallback
    assert src.read() is r  # 3rd: still _last


def test_fake_usage_source_multi_reading_progression() -> None:
    r1 = _make_reading(util_5h=10)
    r2 = _make_reading(util_5h=20)
    r3 = _make_reading(util_5h=30)
    src = FakeUsageSource([r1, r2, r3])
    assert src.read() is r1
    assert src.read() is r2
    assert src.read() is r3
    # Exhausted → last reading repeats.
    assert src.read() is r3
    assert src.read() is r3


def test_fake_usage_source_set_readings_resets() -> None:
    r1 = _make_reading(util_5h=10)
    r2 = _make_reading(util_5h=20)
    src = FakeUsageSource([r1])
    assert src.read() is r1
    # Reset to a new 2-reading script.
    src.set_readings([r2, r1])
    assert src.read() is r2
    assert src.read() is r1


def test_fake_usage_source_set_readings_rejects_empty() -> None:
    src = FakeUsageSource([_make_reading()])
    with pytest.raises(ValueError):
        src.set_readings([])
