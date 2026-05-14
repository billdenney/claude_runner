"""Tests for the usage-output parser, including replay of every fixture."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.usage.drift import UsageFormatDrift
from claude_task_runner.usage.parser import parse
from claude_task_runner.usage.render import render


@pytest.fixture
def captured_at() -> datetime:
    return datetime(2026, 5, 3, 18, 0, 0, tzinfo=UTC)


@pytest.fixture
def fake_clock(captured_at: datetime) -> FakeClock:
    return FakeClock(captured_at)


def _fixture_pairs(usage_fixtures_dir: Path) -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    for cap_path in sorted(usage_fixtures_dir.glob("*.cap")):
        expected_path = cap_path.with_suffix(".expected.json")
        if expected_path.exists():
            pairs.append((cap_path, expected_path))
    return pairs


@pytest.mark.parametrize(
    "cap_path",
    [p for p in sorted((Path(__file__).parent.parent / "fixtures" / "usage").glob("*.cap"))],
    ids=lambda p: p.stem,
)
def test_fixture_replay(
    cap_path: Path,
    captured_at: datetime,
    fake_clock: FakeClock,
) -> None:
    """Every .cap fixture must parse to its .expected.json sibling."""
    expected_path = cap_path.with_suffix(".expected.json")
    expected = json.loads(expected_path.read_text())

    raw = cap_path.read_bytes()
    reading = parse(raw, captured_at, fake_clock)

    assert reading.five_hour.utilization_pct == expected["five_hour"]["utilization"]
    assert reading.five_hour.resets_at_raw == expected["five_hour"]["resets_at_raw"]
    assert reading.seven_day.utilization_pct == expected["seven_day"]["utilization"]
    assert reading.seven_day.resets_at_raw == expected["seven_day"]["resets_at_raw"]
    # captured_at is round-tripped
    assert reading.captured_at == captured_at

    # Optional: extra_windows present in expected.json must match.
    if "extra_windows" in expected:
        assert len(reading.extra_windows) == len(expected["extra_windows"])
        for actual, want in zip(reading.extra_windows, expected["extra_windows"], strict=True):
            assert actual.label == want["label"]
            assert actual.utilization_pct == want["utilization_pct"]
            assert actual.resets_at_raw == want["resets_at_raw"]


def _two_block(five_pct: int, week_pct: int) -> bytes:
    """Build minimal raw bytes that pyte renders into two valid blocks.

    Uses ``\\r\\n`` line endings because pyte's VT100 emulation requires
    explicit CR to return the cursor to column 0 on each new line.
    """
    return (
        f"  Current session\r\n"
        f"  {five_pct}% used\r\n"
        f"  Resets 2:10am (UTC)\r\n"
        f"  Current week (all models)\r\n"
        f"  {week_pct}% used\r\n"
        f"  Resets May 4, 3am (UTC)\r\n"
    ).encode()


class TestParseDrift:
    def test_empty_input_raises_drift(self, captured_at: datetime, fake_clock: FakeClock) -> None:
        with pytest.raises(UsageFormatDrift, match="empty"):
            parse(b"", captured_at, fake_clock)

    def test_only_one_block_raises_drift(
        self, captured_at: datetime, fake_clock: FakeClock
    ) -> None:
        raw = b"  Current session\r\n  38% used\r\n  Resets 2:10am (UTC)\r\n"
        with pytest.raises(UsageFormatDrift, match="weekly"):
            parse(raw, captured_at, fake_clock)

    def test_three_blocks_returns_first_two_when_unheaded(
        self, captured_at: datetime, fake_clock: FakeClock
    ) -> None:
        # Without identifying headers, the parser falls back to positional
        # blocks: first becomes 5h, second becomes weekly. Subsequent
        # blocks are ignored unless they have weekly-other headers.
        raw = (
            b"  38% used\r\n  Resets 2:10am (UTC)\r\n"
            b"  20% used\r\n  Resets May 4, 3am (UTC)\r\n"
            b"  99% used\r\n  Resets Dec 31, 11pm (UTC)\r\n"
        )
        reading = parse(raw, captured_at, fake_clock)
        assert reading.five_hour.utilization_pct == 38
        assert reading.seven_day.utilization_pct == 20
        assert reading.extra_windows == []

    def test_pct_without_resets_drops_block(
        self, captured_at: datetime, fake_clock: FakeClock
    ) -> None:
        raw = (
            b"  38% used\r\n"
            b"  20% used\r\n  Resets May 4, 3am (UTC)\r\n"
            b"  99% used\r\n  Resets Dec 31, 11pm (UTC)\r\n"
        )
        # First "% used" is dropped because it's followed by another "% used"
        # before any "Resets". Resulting positional blocks: 20% + 99%.
        reading = parse(raw, captured_at, fake_clock)
        assert reading.five_hour.utilization_pct == 20
        assert reading.seven_day.utilization_pct == 99

    def test_drift_attaches_raw(self, captured_at: datetime, fake_clock: FakeClock) -> None:
        raw = b"only one resets line\r\nResets 2:10am (UTC)\r\n"
        with pytest.raises(UsageFormatDrift) as exc_info:
            parse(raw, captured_at, fake_clock)
        assert exc_info.value.raw == raw

    def test_unparseable_reset_time_is_NOT_drift(
        self, captured_at: datetime, fake_clock: FakeClock
    ) -> None:
        """A malformed Resets string yields resets_at=None, NOT drift."""
        raw = (
            b"  Current session\r\n  38% used\r\n  Resets gibberish-time\r\n"
            b"  Current week (all models)\r\n  20% used\r\n  Resets May 4, 3am (UTC)\r\n"
        )
        reading = parse(raw, captured_at, fake_clock)
        assert reading.five_hour.utilization_pct == 38
        assert reading.five_hour.resets_at_raw == "gibberish-time"
        assert reading.five_hour.resets_at is None
        # Second block's reset still parses
        assert reading.seven_day.resets_at is not None


class TestRender:
    def test_render_strips_color_ansi(self) -> None:
        raw = b"\x1b[32mhello\x1b[0m world"
        out = render(raw)
        assert "hello world" in out
        assert "\x1b" not in out

    def test_render_handles_empty(self) -> None:
        assert render(b"") == ""

    def test_render_resolves_cursor_overwrite(self) -> None:
        # Write "100% used", then move cursor back to col 0 and write "  4% used"
        raw = b"100% used\r  4% used\r\n"
        out = render(raw)
        # The cursor return overwrites: final visible content is "  4% used"
        assert "4% used" in out
        # After the overwrite, the original "100% used" should not survive intact.
        # pyte preserves trailing whitespace so the literal string '100' may not
        # appear contiguously; the overwritten area is the relevant signal.
        assert "100% used" not in out


class TestPctEdgeCases:
    def test_zero_percent(self, captured_at: datetime, fake_clock: FakeClock) -> None:
        reading = parse(_two_block(0, 0), captured_at, fake_clock)
        assert reading.five_hour.utilization_pct == 0
        assert reading.seven_day.utilization_pct == 0

    def test_hundred_percent(self, captured_at: datetime, fake_clock: FakeClock) -> None:
        reading = parse(_two_block(100, 100), captured_at, fake_clock)
        assert reading.five_hour.utilization_pct == 100

    def test_over_hundred_raises_drift(self, captured_at: datetime, fake_clock: FakeClock) -> None:
        raw = (
            b"  Current session\r\n  150% used\r\n  Resets 2:10am (UTC)\r\n"
            b"  Current week (all models)\r\n  20% used\r\n  Resets May 4, 3am (UTC)\r\n"
        )
        with pytest.raises(UsageFormatDrift, match="outside"):
            parse(raw, captured_at, fake_clock)

    def test_extra_spacing_tolerated(self, captured_at: datetime, fake_clock: FakeClock) -> None:
        raw = (
            b"  Current session\r\n     38   %   used\r\n  Resets 2:10am (UTC)\r\n"
            b"  Current week (all models)\r\n  20% used\r\n  Resets May 4, 3am (UTC)\r\n"
        )
        reading = parse(raw, captured_at, fake_clock)
        assert reading.five_hour.utilization_pct == 38


class TestExtraWindows:
    def test_sonnet_only_section_captured(
        self, captured_at: datetime, fake_clock: FakeClock
    ) -> None:
        raw = (
            b"  Current session\r\n  3% used\r\n  Resets 11pm (UTC)\r\n"
            b"  Current week (all models)\r\n  100% used\r\n  Resets 3am (UTC)\r\n"
            b"  Current week (Sonnet only)\r\n  10% used\r\n  Resets 2:59am (UTC)\r\n"
        )
        reading = parse(raw, captured_at, fake_clock)
        assert reading.seven_day.utilization_pct == 100
        assert len(reading.extra_windows) == 1
        assert reading.extra_windows[0].label == "Sonnet only"
        assert reading.extra_windows[0].utilization_pct == 10

    def test_no_extras_yields_empty_list(
        self, captured_at: datetime, fake_clock: FakeClock
    ) -> None:
        reading = parse(_two_block(38, 20), captured_at, fake_clock)
        assert reading.extra_windows == []


class TestPyteSquashedHeaders:
    """Claude >= 2.1.141 sometimes renders section headers without
    internal whitespace because pyte (the virtual terminal we feed
    raw PTY bytes through) collapses adjacent ANSI-cursor-positioned
    tokens. The parser's header regexes use ``\\s*`` so both shapes
    classify as the same window type.
    """

    def test_squashed_current_session(
        self, captured_at: datetime, fake_clock: FakeClock
    ) -> None:
        raw = (
            b"  Currentsession\r\n  13% used\r\n  Resets 10:10pm (UTC)\r\n"
            b"  Current week (all models)\r\n  15% used\r\n  Resets May 20, 11am (UTC)\r\n"
        )
        reading = parse(raw, captured_at, fake_clock)
        assert reading.five_hour.utilization_pct == 13
        assert reading.seven_day.utilization_pct == 15

    def test_squashed_current_week_all_models(
        self, captured_at: datetime, fake_clock: FakeClock
    ) -> None:
        raw = (
            b"  Current session\r\n  13% used\r\n  Resets 10:10pm (UTC)\r\n"
            b"  Currentweek(allmodels)\r\n  15% used\r\n  Resets May 20, 11am (UTC)\r\n"
        )
        reading = parse(raw, captured_at, fake_clock)
        assert reading.five_hour.utilization_pct == 13
        assert reading.seven_day.utilization_pct == 15

    def test_both_headers_squashed(
        self, captured_at: datetime, fake_clock: FakeClock
    ) -> None:
        raw = (
            b"  Currentsession\r\n  13% used\r\n  Resets10:10pm (UTC)\r\n"
            b"  Currentweek(allmodels)\r\n  15% used\r\n  ResetsMay 20, 11am (UTC)\r\n"
        )
        reading = parse(raw, captured_at, fake_clock)
        assert reading.five_hour.utilization_pct == 13
        assert reading.seven_day.utilization_pct == 15

    def test_squashed_sonnet_only_still_classified_as_extra(
        self, captured_at: datetime, fake_clock: FakeClock
    ) -> None:
        raw = (
            b"  Current session\r\n  3% used\r\n  Resets 11pm (UTC)\r\n"
            b"  Current week (all models)\r\n  100% used\r\n  Resets 3am (UTC)\r\n"
            b"  Currentweek(Sonnetonly)\r\n  10% used\r\n  Resets 2:59am (UTC)\r\n"
        )
        reading = parse(raw, captured_at, fake_clock)
        assert reading.seven_day.utilization_pct == 100
        assert len(reading.extra_windows) == 1
        assert reading.extra_windows[0].label == "Sonnetonly"
        assert reading.extra_windows[0].utilization_pct == 10
