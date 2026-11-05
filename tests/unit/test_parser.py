"""Tests for the usage-output parser, including replay of every fixture."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.usage.drift import UsageFormatDrift
from claude_task_runner.usage.models import ExtraWindow, WindowReading
from claude_task_runner.usage.parser import panel_pending, panel_refresh_failure, parse
from claude_task_runner.usage.render import render

USAGE_FIXTURES = Path(__file__).parent.parent / "fixtures" / "usage"
CLEANUP_FIXTURES = Path(__file__).parent.parent / "fixtures" / "usage_cleanup"


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

    if expected["five_hour"] is None:
        assert reading.five_hour is None
    else:
        assert reading.five_hour is not None
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

    def test_squashed_current_session(self, captured_at: datetime, fake_clock: FakeClock) -> None:
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

    def test_both_headers_squashed(self, captured_at: datetime, fake_clock: FakeClock) -> None:
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


# ---------------------------------------------------------------------------
# Panels without a 5-hour section (ADR-0040).
# ---------------------------------------------------------------------------

NOV_5 = datetime(2026, 11, 5, 17, 51, 0, tzinfo=UTC)
"""When the 2026-11-05 panels were captured."""

_FRAME_END = re.compile(rb"\x1b\[\d+C\x1b\[\d+A")
"""The cursor move with which claude 2.1.318 ends each frame it draws,
back to the input position; a frame's bytes arrive together."""


def _real(name: str) -> bytes:
    return (USAGE_FIXTURES / f"real_2026-11-05_{name}.cap").read_bytes()


def _frame_ends(raw: bytes) -> list[int]:
    """Where each frame drawn after ``/usage`` ends in ``raw``, and its end."""
    start = raw.index(b"/usage")
    return sorted({m.end() for m in _FRAME_END.finditer(raw, start)} | {len(raw)})


def _panel(
    *sections: tuple[str, str | None, str | None],
    footer: str = "Esc to cancel",
    tail: tuple[str, ...] = (),
) -> bytes:
    """A 2.1.318-shaped /usage panel: the session-cost block, then each
    ``(heading, gauge, resets)`` section (no gauge or Resets line for
    ``None``), then ``tail`` and the closing line ``footer``."""
    lines = ["  Session", "", "  Total cost:            $0.0000", ""]
    for heading, gauge, resets in sections:
        lines.append(f"  {heading}")
        if gauge is not None:
            lines.append(f"  ████████          {gauge}")
        if resets is not None:
            lines.append(f"  Resets {resets}")
        lines.append("")
    lines += [*tail, f"  {footer}"]
    return ("\r\n".join(lines) + "\r\n").encode()


WEEKLY = ("Current week (all models)", "16% used", "Nov 11, 11am (UTC)")
FABLE = ("Current week (Fable)", "12% used", "Nov 11, 11am (UTC)")
SESSION = ("Current session", "14% used", "8pm (UTC)")


class TestRealPanels:
    """Known answers from the 2026-11-05 captures of claude 2.1.318."""

    def test_weekly_only_panel_has_no_five_hour_window(self) -> None:
        reading = parse(_real("weekly_only"), NOV_5, FakeClock(NOV_5))

        assert reading.five_hour is None
        assert reading.five_hour_util_pct is None
        assert reading.seven_day == WindowReading(
            utilization_pct=16,
            resets_at_raw="Nov 11, 11am (UTC)",
            resets_at=datetime(2026, 11, 11, 11, 0, tzinfo=UTC),
        )
        assert reading.extra_windows == [
            ExtraWindow(
                label="Fable",
                utilization_pct=12,
                resets_at_raw="Nov 11, 11am (UTC)",
                resets_at=datetime(2026, 11, 11, 11, 0, tzinfo=UTC),
            )
        ]

    def test_session_and_weekly_panel_reads_as_before(self) -> None:
        reading = parse(_real("session_and_weekly"), NOV_5, FakeClock(NOV_5))

        assert reading.five_hour == WindowReading(
            utilization_pct=14,
            resets_at_raw="8pm (UTC)",
            resets_at=datetime(2026, 11, 5, 20, 0, tzinfo=UTC),
        )
        assert reading.seven_day == WindowReading(
            utilization_pct=61,
            resets_at_raw="Nov 9, 3am (UTC)",
            resets_at=datetime(2026, 11, 9, 3, 0, tzinfo=UTC),
        )
        assert [(w.label, w.utilization_pct) for w in reading.extra_windows] == [("Fable", 0)]

    @pytest.mark.parametrize(
        ("name", "complete"),
        [
            # Opened with the saved 15% under "Refreshing…"; the fetch drew 16%
            # and the Fable section in the last frame.
            ("weekly_only", [False, False, False, False, False, True]),
            # Opened with the saved session and weekly sections; the fetch
            # added Fable, then the usage breakdown below it.
            ("session_and_weekly", [False, False, False, True, True, True]),
        ],
    )
    def test_only_frames_after_the_fetch_are_complete(
        self, name: str, complete: list[bool]
    ) -> None:
        """At every frame end, a panel that ``panel_pending`` calls complete
        reads exactly as the whole capture does."""
        raw = _real(name)
        expected = parse(raw, NOV_5, FakeClock(NOV_5))
        seen: list[bool] = []
        for end in _frame_ends(raw):
            text = render(raw[:end])
            assert panel_refresh_failure(text) is None
            seen.append(panel_pending(text) is None)
            if seen[-1]:
                assert parse(raw[:end], NOV_5, FakeClock(NOV_5)) == expected
        assert seen == complete

    def test_the_saved_numbers_under_refreshing_are_not_read(self) -> None:
        raw = _real("weekly_only")
        seeded = max(e for e in _frame_ends(raw) if "Refreshing…" in render(raw[:e]))
        assert "15% used" in render(raw[:seeded])

        with pytest.raises(UsageFormatDrift, match=r"it still shows 'Refreshing…'$"):
            parse(raw[:seeded], NOV_5, FakeClock(NOV_5))

    def test_a_dismissed_panel_is_not_read_as_weekly_only(self) -> None:
        """Esc redraws the terminal's rows and leaves the panel's lower rows
        in the render: the weekly sections without the session above them."""
        tail = (CLEANUP_FIXTURES / "real_2026-11-05_session_and_weekly_after_esc.cap").read_bytes()
        raw = _real("session_and_weekly") + tail
        text = render(raw)
        assert "Settings dialog dismissed" in text
        assert "Current week (all models)" in text
        assert "Current session" not in text

        with pytest.raises(
            UsageFormatDrift,
            match=r"applies: the panel was dismissed, so the screen holds only part of it$",
        ):
            parse(raw, NOV_5, FakeClock(NOV_5))


class TestNoFiveHourSection:
    def test_weekly_section_alone(self) -> None:
        reading = parse(_panel(WEEKLY), NOV_5, FakeClock(NOV_5))
        assert reading.five_hour is None
        assert reading.seven_day.utilization_pct == 16
        assert reading.extra_windows == []

    def test_weekly_out_of_range_is_drift(self) -> None:
        over = ("Current week (all models)", "150% used", "Nov 11, 11am (UTC)")
        with pytest.raises(UsageFormatDrift, match=r"^weekly utilization 150% outside \[0, 100\]$"):
            parse(_panel(over), NOV_5, FakeClock(NOV_5))

    def test_a_credit_gauge_is_no_window(self) -> None:
        credits = ("Usage credits", "36% used", None)
        tail = ("  $36.89 / $100.00 spent · Resets Dec 1 (UTC)", "")
        reading = parse(_panel(WEEKLY, FABLE, credits, tail=tail), NOV_5, FakeClock(NOV_5))
        assert reading.five_hour is None
        assert [w.label for w in reading.extra_windows] == ["Fable"]

    def test_a_squashed_weekly_heading_counts(self) -> None:
        squashed = ("Currentweek(allmodels)", "16% used", "Nov 11, 11am (UTC)")
        reading = parse(_panel(squashed), NOV_5, FakeClock(NOV_5))
        assert reading.five_hour is None
        assert reading.seven_day.utilization_pct == 16

    @pytest.mark.parametrize(
        "heading",
        ["Current 5-hour window", "Current session (all models)", "Session limit"],
    )
    def test_a_renamed_session_heading_is_drift(self, heading: str) -> None:
        raw = _panel((heading, "14% used", "8pm (UTC)"), WEEKLY, FABLE)
        with pytest.raises(UsageFormatDrift) as info:
            parse(raw, NOV_5, FakeClock(NOV_5))
        assert str(info.value) == (
            "5-hour usage block not found, and the panel does not show that no 5-hour "
            "window applies: usage gauges under headings that name no known window: "
            f"[{heading!r}]"
        )
        assert info.value.raw == raw

    def test_a_session_section_without_a_reset_is_drift(self) -> None:
        raw = _panel(("Current session", "0% used", None), WEEKLY)
        with pytest.raises(
            UsageFormatDrift,
            match=r"applies: 5-hour heading 'Current session' without a percentage and reset$",
        ):
            parse(raw, NOV_5, FakeClock(NOV_5))

    def test_a_session_heading_does_not_lend_itself_to_the_next_section(self) -> None:
        """A ``Current session`` heading drawn with no gauge of its own is
        not the heading of the section under an unknown heading after it,
        whose numbers would otherwise read as the 5-hour window."""
        raw = _panel(
            ("Current session", None, None),
            ("Current 5h window", "3% used", "8pm (UTC)"),
            WEEKLY,
        )
        with pytest.raises(UsageFormatDrift) as info:
            parse(raw, NOV_5, FakeClock(NOV_5))
        assert str(info.value) == (
            "5-hour usage block not found, and the panel does not show that no 5-hour "
            "window applies: 5-hour heading 'Current session' without a percentage and "
            "reset; usage gauges under headings that name no known window: "
            "['Current 5h window']"
        )

    def test_a_bar_on_its_own_line_keeps_the_heading(self) -> None:
        raw = (
            b"  Current session\r\n  \xe2\x96\x88\xe2\x96\x88\r\n  14% used\r\n"
            b"  Resets 8pm (UTC)\r\n" + _panel(WEEKLY)
        )
        reading = parse(raw, NOV_5, FakeClock(NOV_5))
        assert reading.five_hour is not None
        assert reading.five_hour.utilization_pct == 14

    def test_a_gauge_with_no_line_above_it_is_drift(self) -> None:
        raw = b"  7% used\r\n  Resets 9pm (UTC)\r\n" + _panel(WEEKLY)
        with pytest.raises(UsageFormatDrift, match=r"name no known window: \[None\]$"):
            parse(raw, NOV_5, FakeClock(NOV_5))

    def test_a_gauge_below_the_closing_line_is_drift(self) -> None:
        raw = _panel(WEEKLY) + b"\r\n  7% used\r\n  Resets 9pm (UTC)\r\n"
        with pytest.raises(UsageFormatDrift, match=r"name no known window: \['Esc to cancel'\]$"):
            parse(raw, NOV_5, FakeClock(NOV_5))

    @pytest.mark.parametrize(
        ("raw", "doubt"),
        [
            (_panel(WEEKLY, tail=("  Refreshing…", "")), "it still shows 'Refreshing…'"),
            (_panel(WEEKLY, footer="q to quit"), "its closing line 'Esc to cancel' is not drawn"),
            (
                _panel(WEEKLY, footer="Enter to confirm · Esc to cancel"),
                "its closing line 'Esc to cancel' is not drawn",
            ),
        ],
        ids=["refreshing", "no-footer", "another-dialog"],
    )
    def test_an_unfinished_panel_is_drift(self, raw: bytes, doubt: str) -> None:
        with pytest.raises(UsageFormatDrift, match=f"applies: the panel is not complete: {doubt}$"):
            parse(raw, NOV_5, FakeClock(NOV_5))

    def test_a_failed_refresh_is_drift(self) -> None:
        raw = _panel(
            WEEKLY, tail=("  Could not refresh usage data", ""), footer="r to retry · Esc to cancel"
        )
        with pytest.raises(
            UsageFormatDrift,
            match=r"applies: the panel could not refresh its numbers "
            r"\('Could not refresh usage data'\)$",
        ):
            parse(raw, NOV_5, FakeClock(NOV_5))

    def test_every_doubt_is_listed(self) -> None:
        raw = _panel(
            ("Current 5h window", "3% used", "8pm (UTC)"),
            ("Current session", "0% used", None),
            WEEKLY,
            tail=("  Refreshing…", ""),
        )
        with pytest.raises(UsageFormatDrift) as info:
            parse(raw, NOV_5, FakeClock(NOV_5))
        assert str(info.value).split(": ", 1)[1].split("; ") == [
            "the panel is not complete: it still shows 'Refreshing…'",
            "5-hour heading 'Current session' without a percentage and reset",
            "usage gauges under headings that name no known window: ['Current 5h window']",
        ]


class TestNoWeeklySection:
    """A panel without a ``Current week (all models)`` section is drift,
    whatever else it shows."""

    @pytest.mark.parametrize(
        ("sections", "message"),
        [
            ((FABLE,), "5-hour usage block not found"),
            ((FABLE, ("Current week (Sonnet only)", "3% used", "Nov 11 (UTC)")), "5-hour"),
            ((SESSION,), "weekly (all models) usage block not found"),
            # A per-model section never stands in for the all-models one.
            ((SESSION, FABLE), "weekly (all models) usage block not found"),
        ],
        ids=["per-model-only", "two-per-model", "session-only", "session-and-per-model"],
    )
    def test_missing_weekly_section_is_drift(
        self, sections: tuple[tuple[str, str | None, str | None], ...], message: str
    ) -> None:
        with pytest.raises(UsageFormatDrift, match=f"^{re.escape(message)}"):
            parse(_panel(*sections), NOV_5, FakeClock(NOV_5))

    def test_unheaded_blocks_still_read_by_position(self) -> None:
        """A heading the parser cannot read, with no other heading on the
        panel, still falls back to position."""
        raw = _panel(("Something new", "38% used", "2:10am (UTC)"), ("Else", "20% used", "May 4"))
        reading = parse(raw, NOV_5, FakeClock(NOV_5))
        assert reading.five_hour is not None
        assert reading.five_hour.utilization_pct == 38
        assert reading.seven_day.utilization_pct == 20


class TestPanelState:
    def test_retry_footer_names_the_line_above(self) -> None:
        text = render(
            _panel(
                WEEKLY, tail=("  Error: Failed to load", ""), footer="r to retry · Esc to cancel"
            )
        )
        assert panel_refresh_failure(text) == "Error: Failed to load"

    def test_retry_footer_alone_names_itself(self) -> None:
        assert panel_refresh_failure("  r to retry · Esc to cancel") == "r to retry · Esc to cancel"

    def test_three_dots_are_in_flight_too(self) -> None:
        text = render(_panel(WEEKLY, tail=("  Refreshing...", "")))
        assert panel_pending(text) == "it still shows 'Refreshing...'"

    def test_loading_is_in_flight(self) -> None:
        text = render(b"  Loading usage data\xe2\x80\xa6\r\n\r\n  Esc to cancel\r\n")
        assert panel_pending(text) == "it still shows 'Loading usage data…'"

    def test_a_plain_footer_with_nothing_in_flight_is_complete(self) -> None:
        assert panel_pending(render(_panel(WEEKLY))) is None
        assert panel_refresh_failure(render(_panel(WEEKLY))) is None
