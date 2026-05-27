"""Tests for the variant-C trace-following curve and its inverse (ADR-0022)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from claude_task_runner.throttle.curve import (
    SEVEN_DAYS_S,
    elapsed_for_target_pct,
    elapsed_fraction,
    target_pct,
)


class TestElapsedFraction:
    def test_at_start(self) -> None:
        now = datetime(2026, 5, 27, 0, 0, tzinfo=UTC)
        resets = now + timedelta(seconds=SEVEN_DAYS_S)
        assert elapsed_fraction(now, resets) == 0.0

    def test_at_end(self) -> None:
        now = datetime(2026, 5, 27, 0, 0, tzinfo=UTC)
        resets = now
        assert elapsed_fraction(now, resets) == 1.0

    def test_half(self) -> None:
        now = datetime(2026, 5, 27, 0, 0, tzinfo=UTC)
        resets = now + timedelta(seconds=SEVEN_DAYS_S / 2)
        assert elapsed_fraction(now, resets) == pytest.approx(0.5)

    def test_clamps_negative_past_reset(self) -> None:
        now = datetime(2026, 5, 27, 0, 0, tzinfo=UTC)
        resets = now - timedelta(hours=1)
        assert elapsed_fraction(now, resets) == 1.0

    def test_clamps_before_window_opens(self) -> None:
        now = datetime(2026, 5, 27, 0, 0, tzinfo=UTC)
        resets = now + timedelta(seconds=SEVEN_DAYS_S * 2)
        assert elapsed_fraction(now, resets) == 0.0

    def test_zero_window_raises(self) -> None:
        now = datetime(2026, 5, 27, 0, 0, tzinfo=UTC)
        with pytest.raises(ValueError, match="window_s must be positive"):
            elapsed_fraction(now, now, window_s=0)


class TestTargetPct:
    """Spec tests for the ADR-0022 curve."""

    EWS = 40 * 3600 / SEVEN_DAYS_S  # ~0.2381

    def test_zero_at_start(self) -> None:
        assert target_pct(0.0, early_pct=60, eow_pct=95, eow_window_fraction=self.EWS) == 0.0

    def test_eow_pct_at_end(self) -> None:
        assert target_pct(
            1.0, early_pct=60, eow_pct=95, eow_window_fraction=self.EWS
        ) == pytest.approx(95.0)

    def test_early_pct_at_breakpoint(self) -> None:
        b = 1.0 - self.EWS
        assert target_pct(
            b, early_pct=60, eow_pct=95, eow_window_fraction=self.EWS
        ) == pytest.approx(60.0)

    def test_monotone_increasing(self) -> None:
        previous = -1.0
        for i in range(101):
            t = i / 100.0
            v = target_pct(t, early_pct=60, eow_pct=95, eow_window_fraction=self.EWS)
            assert v >= previous - 1e-9, f"non-monotone at t={t}: {v} < {previous}"
            previous = v

    def test_clamps_elapsed_above_one(self) -> None:
        v_one = target_pct(1.0, early_pct=60, eow_pct=95, eow_window_fraction=self.EWS)
        v_two = target_pct(2.0, early_pct=60, eow_pct=95, eow_window_fraction=self.EWS)
        assert v_one == v_two == pytest.approx(95.0)

    def test_clamps_elapsed_below_zero(self) -> None:
        v = target_pct(-0.5, early_pct=60, eow_pct=95, eow_window_fraction=self.EWS)
        assert v == 0.0

    def test_degenerate_eow_window_fraction_zero(self) -> None:
        # No EOW segment: curve ramps 0 → early_pct linearly across the week.
        assert target_pct(0.5, early_pct=60, eow_pct=95, eow_window_fraction=0.0) == pytest.approx(
            30.0
        )
        assert target_pct(1.0, early_pct=60, eow_pct=95, eow_window_fraction=0.0) == pytest.approx(
            60.0
        )

    def test_degenerate_eow_window_fraction_one(self) -> None:
        # All EOW: curve ramps 0 → eow_pct linearly across the week.
        assert target_pct(0.5, early_pct=60, eow_pct=95, eow_window_fraction=1.0) == pytest.approx(
            47.5
        )

    def test_output_clamped_above_100(self) -> None:
        # Pathological early_pct > 100 should still clamp.
        v = target_pct(1.0, early_pct=150, eow_pct=200, eow_window_fraction=0.5)
        assert v == 100.0


class TestElapsedForTargetPct:
    """Inverse of :func:`target_pct`."""

    EWS = 40 * 3600 / SEVEN_DAYS_S

    def test_zero_observed(self) -> None:
        assert (
            elapsed_for_target_pct(0.0, early_pct=60, eow_pct=95, eow_window_fraction=self.EWS)
            == 0.0
        )

    def test_observed_at_or_above_eow_returns_one(self) -> None:
        for o in (95.0, 96.0, 100.0):
            t = elapsed_for_target_pct(o, early_pct=60, eow_pct=95, eow_window_fraction=self.EWS)
            assert t == 1.0

    def test_observed_below_zero_returns_zero(self) -> None:
        assert (
            elapsed_for_target_pct(-5.0, early_pct=60, eow_pct=95, eow_window_fraction=self.EWS)
            == 0.0
        )

    def test_round_trip_segment_a(self) -> None:
        for observed in (1.0, 15.0, 30.0, 45.0, 59.99):
            t = elapsed_for_target_pct(
                observed, early_pct=60, eow_pct=95, eow_window_fraction=self.EWS
            )
            back = target_pct(t, early_pct=60, eow_pct=95, eow_window_fraction=self.EWS)
            assert back == pytest.approx(observed, abs=1e-6)

    def test_round_trip_segment_b(self) -> None:
        for observed in (60.001, 70.0, 80.0, 90.0, 94.999):
            t = elapsed_for_target_pct(
                observed, early_pct=60, eow_pct=95, eow_window_fraction=self.EWS
            )
            back = target_pct(t, early_pct=60, eow_pct=95, eow_window_fraction=self.EWS)
            assert back == pytest.approx(observed, abs=1e-6)

    def test_observed_equals_early_at_breakpoint(self) -> None:
        b = 1.0 - self.EWS
        t = elapsed_for_target_pct(60.0, early_pct=60, eow_pct=95, eow_window_fraction=self.EWS)
        assert t == pytest.approx(b, abs=1e-9)

    def test_degenerate_eow_window_fraction_zero(self) -> None:
        # All segment A → invertible across [0, early_pct].
        t = elapsed_for_target_pct(30.0, early_pct=60, eow_pct=95, eow_window_fraction=0.0)
        assert t == pytest.approx(0.5)

    def test_degenerate_eow_window_fraction_one(self) -> None:
        # All segment B → invertible across [0, eow_pct].
        t = elapsed_for_target_pct(47.5, early_pct=60, eow_pct=95, eow_window_fraction=1.0)
        assert t == pytest.approx(0.5)

    def test_inversion_robust_across_grid(self) -> None:
        """Round-trip every reachable (early, eow, ews, observed)."""
        for early in (10, 30, 60, 80):
            for eow in (early + 5, early + 10, 95):
                for ews in (0.05, 0.2, 0.4, 0.8):
                    for o_pct in (0.0, early - 5, early, (early + eow) / 2, eow - 1):
                        if o_pct < 0:
                            continue
                        t = elapsed_for_target_pct(
                            o_pct,
                            early_pct=float(early),
                            eow_pct=float(eow),
                            eow_window_fraction=ews,
                        )
                        back = target_pct(
                            t,
                            early_pct=float(early),
                            eow_pct=float(eow),
                            eow_window_fraction=ews,
                        )
                        assert back == pytest.approx(o_pct, abs=1e-6), (
                            f"round-trip failed: early={early}, eow={eow}, ews={ews}, "
                            f"o={o_pct} → t={t} → target={back}"
                        )
