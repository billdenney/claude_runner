"""Tests for supervisor.pacing — pure functions, full coverage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from claude_task_runner.supervisor.pacing import (
    SEVEN_DAYS_S,
    adjusted_weekly_band,
    elapsed_fraction,
    target_weekly_pct,
)


class TestElapsedFraction:
    def test_at_start(self) -> None:
        """Now == window start ⇒ elapsed = 0."""
        now = datetime(2026, 5, 13, 0, 0, tzinfo=UTC)
        resets_at = now + timedelta(seconds=SEVEN_DAYS_S)
        assert elapsed_fraction(resets_at=resets_at, now=now) == pytest.approx(0.0)

    def test_at_end(self) -> None:
        """Now == reset ⇒ elapsed = 1."""
        now = datetime(2026, 5, 13, 0, 0, tzinfo=UTC)
        assert elapsed_fraction(resets_at=now, now=now) == pytest.approx(1.0)

    def test_midweek(self) -> None:
        now = datetime(2026, 5, 13, 0, 0, tzinfo=UTC)
        resets_at = now + timedelta(seconds=SEVEN_DAYS_S / 2)
        assert elapsed_fraction(resets_at=resets_at, now=now) == pytest.approx(0.5)

    def test_clamps_past_reset(self) -> None:
        """Now past reset ⇒ clamps to 1.0 (don't return >1)."""
        now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
        resets_at = now - timedelta(hours=2)
        assert elapsed_fraction(resets_at=resets_at, now=now) == 1.0

    def test_clamps_before_window(self) -> None:
        """Reset more than one window in the future ⇒ elapsed clamps to 0."""
        now = datetime(2026, 5, 13, 0, 0, tzinfo=UTC)
        resets_at = now + timedelta(days=10)
        assert elapsed_fraction(resets_at=resets_at, now=now) == 0.0

    def test_zero_window_length_raises(self) -> None:
        with pytest.raises(ValueError):
            elapsed_fraction(
                resets_at=datetime(2026, 5, 13, tzinfo=UTC),
                now=datetime(2026, 5, 13, tzinfo=UTC),
                window_length_s=0,
            )

    def test_negative_window_length_raises(self) -> None:
        with pytest.raises(ValueError):
            elapsed_fraction(
                resets_at=datetime(2026, 5, 13, tzinfo=UTC),
                now=datetime(2026, 5, 13, tzinfo=UTC),
                window_length_s=-1,
            )

    def test_custom_window_length(self) -> None:
        """``window_length_s`` is honored (e.g., for non-7-day windows in tests)."""
        now = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
        resets_at = now + timedelta(hours=2)
        # 2h elapsed of a 4h window = 0.5
        assert elapsed_fraction(
            resets_at=resets_at, now=now, window_length_s=4 * 3600
        ) == pytest.approx(0.5)


class TestTargetWeeklyPct:
    EOW_TARGET = 95.0
    EOW_FRAC = 0.15  # last ~25h of a 7-day window
    PRE_EOW = 80.0

    def _t(self, elapsed: float) -> float:
        return target_weekly_pct(
            elapsed,
            eow_target_pct=self.EOW_TARGET,
            eow_window_fraction=self.EOW_FRAC,
            pre_eow_target_pct=self.PRE_EOW,
        )

    def test_at_start(self) -> None:
        assert self._t(0.0) == pytest.approx(0.0)

    def test_at_end(self) -> None:
        assert self._t(1.0) == pytest.approx(self.EOW_TARGET)

    def test_at_breakpoint(self) -> None:
        """Exactly at 1 - eow_window_fraction the target is pre_eow_target_pct."""
        assert self._t(1.0 - self.EOW_FRAC) == pytest.approx(self.PRE_EOW)

    def test_midway_in_pre_eow_segment(self) -> None:
        """Halfway through the pre-EOW ramp ⇒ half of pre_eow_target_pct."""
        elapsed = (1.0 - self.EOW_FRAC) / 2.0
        assert self._t(elapsed) == pytest.approx(self.PRE_EOW / 2.0)

    def test_midway_in_eow_segment(self) -> None:
        """Halfway between breakpoint and 1.0 ⇒ midpoint of pre_eow and eow targets."""
        breakpoint = 1.0 - self.EOW_FRAC
        elapsed = breakpoint + self.EOW_FRAC / 2.0
        expected = (self.PRE_EOW + self.EOW_TARGET) / 2.0
        assert self._t(elapsed) == pytest.approx(expected)

    def test_elapsed_clamps_below_zero(self) -> None:
        assert self._t(-0.5) == 0.0

    def test_elapsed_clamps_above_one(self) -> None:
        assert self._t(1.5) == pytest.approx(self.EOW_TARGET)

    def test_zero_eow_window(self) -> None:
        """``eow_window_fraction = 0`` ⇒ linear to pre_eow_target_pct across whole week."""
        target = target_weekly_pct(
            0.5,
            eow_target_pct=self.EOW_TARGET,
            eow_window_fraction=0.0,
            pre_eow_target_pct=self.PRE_EOW,
        )
        assert target == pytest.approx(self.PRE_EOW / 2.0)

    def test_full_eow_window(self) -> None:
        """``eow_window_fraction = 1`` ⇒ linear to eow_target_pct across whole week."""
        target = target_weekly_pct(
            0.5,
            eow_target_pct=self.EOW_TARGET,
            eow_window_fraction=1.0,
            pre_eow_target_pct=self.PRE_EOW,
        )
        assert target == pytest.approx(self.EOW_TARGET / 2.0)

    def test_eow_fraction_clamps_negative(self) -> None:
        target = target_weekly_pct(
            0.5,
            eow_target_pct=self.EOW_TARGET,
            eow_window_fraction=-0.1,
            pre_eow_target_pct=self.PRE_EOW,
        )
        # Treated as 0
        assert target == pytest.approx(self.PRE_EOW / 2.0)

    def test_eow_fraction_clamps_above_one(self) -> None:
        target = target_weekly_pct(
            0.5,
            eow_target_pct=self.EOW_TARGET,
            eow_window_fraction=1.5,
            pre_eow_target_pct=self.PRE_EOW,
        )
        # Treated as 1
        assert target == pytest.approx(self.EOW_TARGET / 2.0)

    def test_clamps_to_100(self) -> None:
        """Out-of-range inputs can't produce >100% target."""
        target = target_weekly_pct(
            1.0,
            eow_target_pct=200.0,  # absurd
            eow_window_fraction=0.5,
            pre_eow_target_pct=100.0,
        )
        assert target == 100.0


class TestAdjustedWeeklyBand:
    def test_within_slack_returns_base(self) -> None:
        """Observed within ``slack_pp`` of target ⇒ no shift."""
        result = adjusted_weekly_band(
            observed_pct=55.0,
            target_now=50.0,
            base_pct=70,
            slack_pp=10.0,
        )
        assert result == 70

    def test_within_slack_below_target(self) -> None:
        result = adjusted_weekly_band(
            observed_pct=45.0,
            target_now=50.0,
            base_pct=70,
            slack_pp=10.0,
        )
        assert result == 70

    def test_ahead_of_target_tightens(self) -> None:
        """Observed > target + slack ⇒ band shifts down by (excess - slack)."""
        # observed=75, target=50, slack=10 ⇒ excess=25, shift=15 ⇒ band=55
        result = adjusted_weekly_band(
            observed_pct=75.0,
            target_now=50.0,
            base_pct=70,
            slack_pp=10.0,
        )
        assert result == 55

    def test_behind_target_loosens(self) -> None:
        """Observed < target - slack ⇒ band shifts up by (deficit - slack)."""
        # observed=25, target=50, slack=10 ⇒ deficit=25, shift=15 ⇒ band=85
        result = adjusted_weekly_band(
            observed_pct=25.0,
            target_now=50.0,
            base_pct=70,
            slack_pp=10.0,
        )
        assert result == 85

    def test_clamps_to_min(self) -> None:
        """Extreme overshoot can't drive band below ``min_pct``."""
        result = adjusted_weekly_band(
            observed_pct=99.0,
            target_now=10.0,
            base_pct=70,
            slack_pp=5.0,
            min_pct=20,
        )
        # Raw shift would push to 70 - (99-10-5) = -14; clamped to 20.
        assert result == 20

    def test_clamps_to_max(self) -> None:
        """Extreme undershoot can't drive band above ``max_pct``."""
        result = adjusted_weekly_band(
            observed_pct=1.0,
            target_now=90.0,
            base_pct=70,
            slack_pp=5.0,
            max_pct=80,
        )
        # Raw shift would push to 70 + (89-5) = 154; clamped to 80.
        assert result == 80

    def test_default_clamps_to_zero_and_100(self) -> None:
        """Default min/max are [0, 100]."""
        # 99% over: 70 - (99 - 10) = 70 - 89 = -19 -> 0
        result = adjusted_weekly_band(
            observed_pct=99.0,
            target_now=0.0,
            base_pct=70,
            slack_pp=10.0,
        )
        assert result == 0

    def test_exact_slack_boundary_no_shift(self) -> None:
        """Equal to slack distance still counts as within (use ``<=``)."""
        result = adjusted_weekly_band(
            observed_pct=60.0,
            target_now=50.0,
            base_pct=70,
            slack_pp=10.0,
        )
        # deviation == slack ⇒ no shift.
        assert result == 70

    def test_rounds_half_up(self) -> None:
        """Non-integer shift ⇒ banker-rounded to int (Python ``round``)."""
        # observed=75.7, target=50, slack=10 ⇒ shift=15.7 ⇒ adjusted=54.3 ⇒ round to 54.
        result = adjusted_weekly_band(
            observed_pct=75.7,
            target_now=50.0,
            base_pct=70,
            slack_pp=10.0,
        )
        assert result == 54
