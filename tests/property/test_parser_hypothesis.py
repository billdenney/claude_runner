"""Property-based tests for the usage parser.

The risk we're guarding against: small perturbations in spacing or ANSI
that the parser should tolerate, vs. structural perturbations (missing
"Resets" lines, percentages out of range) that MUST raise drift.

We don't fuzz the entire byte space — that just generates noise. Instead
we mutate canonical inputs in known ways and assert outcomes.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from claude_task_runner.clock import FakeClock
from claude_task_runner.usage.drift import UsageFormatDrift
from claude_task_runner.usage.parser import parse


def _canonical(five_pct: int, week_pct: int) -> bytes:
    """Build a minimal pyte-renderable two-block usage capture."""
    return (
        f"  Current session\r\n  {five_pct}% used\r\n  Resets 2:10am (UTC)\r\n"
        f"  Current week (all models)\r\n  {week_pct}% used\r\n  Resets May 4, 3am (UTC)\r\n"
    ).encode()


CAPTURED_AT = datetime(2026, 5, 3, 18, 0, 0, tzinfo=UTC)


@given(
    five=st.integers(min_value=0, max_value=100),
    week=st.integers(min_value=0, max_value=100),
)
def test_any_valid_percentages_round_trip(five: int, week: int) -> None:
    clock = FakeClock(CAPTURED_AT)
    raw = _canonical(five, week)
    reading = parse(raw, CAPTURED_AT, clock)
    assert reading.five_hour.utilization_pct == five
    assert reading.seven_day.utilization_pct == week


@given(
    five=st.integers().filter(lambda x: x < 0 or x > 100),
)
@settings(suppress_health_check=[HealthCheck.filter_too_much])
def test_out_of_range_percentage_raises_drift(five: int) -> None:
    clock = FakeClock(CAPTURED_AT)
    # Three-digit cap so we can construct the bytes; pick a representative
    # invalid value within string-formattable range.
    if not (-999 <= five <= 999):
        return
    raw = (
        f"  Current session\r\n  {five}% used\r\n  Resets 2:10am (UTC)\r\n"
        f"  Current week (all models)\r\n  20% used\r\n  Resets May 4, 3am (UTC)\r\n"
    ).encode()
    # Two paths: (a) regex fails to match negative numbers cleanly so the
    # parser sees "weekly not found" drift; (b) it matches but is rejected
    # as out of range. Either is acceptable drift.
    with pytest.raises(UsageFormatDrift):
        parse(raw, CAPTURED_AT, clock)


@given(extra_blanks=st.integers(min_value=0, max_value=20))
def test_extra_blank_lines_tolerated(extra_blanks: int) -> None:
    """Inserting blank lines between blocks should not break parsing."""
    clock = FakeClock(CAPTURED_AT)
    blanks = b"\r\n" * extra_blanks
    raw = (
        b"  Current session\r\n  38% used\r\n  Resets 2:10am (UTC)\r\n"
        + blanks
        + b"  Current week (all models)\r\n  20% used\r\n  Resets May 4, 3am (UTC)\r\n"
    )
    reading = parse(raw, CAPTURED_AT, clock)
    assert reading.five_hour.utilization_pct == 38
    assert reading.seven_day.utilization_pct == 20


@given(
    leading_garbage=st.text(
        alphabet=st.characters(blacklist_categories=("Cs",), min_codepoint=32, max_codepoint=126),
        min_size=0,
        max_size=200,
    )
)
def test_arbitrary_leading_text_does_not_create_phantom_blocks(
    leading_garbage: str,
) -> None:
    """Random ASCII garbage before the real blocks must not parse as a block."""
    clock = FakeClock(CAPTURED_AT)
    # Garbage that contains no "% used" or "Resets " lines must not produce
    # extra blocks. We strip such substrings from the random text to enforce
    # the precondition cleanly.
    safe = leading_garbage.replace("% used", "").replace("Resets", "")
    raw = (
        safe.encode("utf-8", errors="ignore")
        + b"\r\n  Current session\r\n  38% used\r\n  Resets 2:10am (UTC)\r\n"
        + b"  Current week (all models)\r\n  20% used\r\n  Resets May 4, 3am (UTC)\r\n"
    )
    reading = parse(raw, CAPTURED_AT, clock)
    assert reading.five_hour.utilization_pct == 38
    assert reading.seven_day.utilization_pct == 20
