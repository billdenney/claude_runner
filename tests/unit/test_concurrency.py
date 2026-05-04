"""Tests for runner.concurrency — three-band throttle + EMA-driven target."""

from __future__ import annotations

import pytest

from claude_task_runner.config.schema import (
    ConcurrencySettings,
    EMAPrior,
    EMASettings,
    ThrottleBandSettings,
    ThrottleSettings,
    ThrottleWeeklySettings,
)
from claude_task_runner.queue.schema import Task
from claude_task_runner.runner.concurrency import (
    DispatchBand,
    compute_target_concurrency,
    in_flight_estimate_tokens,
    predict_post_dispatch_pct,
    should_dispatch,
)
from claude_task_runner.runner.ema import EMAFile


def _band_5h(full: int = 70, slow: int = 90, budget: int = 100_000_000) -> ThrottleBandSettings:
    return ThrottleBandSettings(
        budget_tokens=budget,
        band_full_dispatch_max_pct=full,
        band_slowdown_max_pct=slow,
    )


def _band_weekly(
    full: int = 70, slow: int = 90, budget: int = 800_000_000
) -> ThrottleWeeklySettings:
    return ThrottleWeeklySettings(
        budget_tokens=budget,
        band_full_dispatch_max_pct=full,
        band_slowdown_max_pct=slow,
        pause_at_pct=90,
        eow_push_enter_at_pct=90,
        eow_target_pct=98,
        eow_window_s=43200,
        eow_runtime_safety_factor=0.5,
    )


def _concurrency(max_c: int = 4, init: int = 1) -> ConcurrencySettings:
    return ConcurrencySettings(max_concurrency=max_c, initial_concurrency=init)


def _ema_settings() -> EMASettings:
    return EMASettings(
        alpha=0.3,
        prior_warmup_samples=3,
        runtime_p90_multiplier=1.5,
        priors={
            "claude-opus-4-7": {
                "medium": EMAPrior(tokens=2_000_000, duration_s=1200),
            },
        },
    )


class TestPredictPct:
    def test_basic(self) -> None:
        pct = predict_post_dispatch_pct(
            used_tokens=10_000_000,
            in_flight_estimate_tokens=5_000_000,
            new_task_estimate_tokens=2_000_000,
            budget_tokens=100_000_000,
        )
        assert pct == pytest.approx(0.17)

    def test_zero_budget_rejected(self) -> None:
        with pytest.raises(ValueError):
            predict_post_dispatch_pct(
                used_tokens=0,
                in_flight_estimate_tokens=0,
                new_task_estimate_tokens=0,
                budget_tokens=0,
            )


class TestComputeTarget:
    def test_full_band_full_concurrency(self) -> None:
        decision = compute_target_concurrency(
            used_5h_tokens=10_000_000,
            used_weekly_tokens=10_000_000,
            in_flight_estimate_tokens=0,
            new_task_estimate_tokens=2_000_000,
            five_hour=_band_5h(budget=100_000_000),
            weekly=_band_weekly(budget=800_000_000),
            concurrency=_concurrency(max_c=4),
            have_ema_warmup=True,
        )
        assert decision.band is DispatchBand.FULL
        assert decision.target_concurrency == 4

    def test_slowdown_band_reduces(self) -> None:
        # Pred 5h pct ~= 0.80 — middle of slowdown band [0.70, 0.90]
        decision = compute_target_concurrency(
            used_5h_tokens=78_000_000,
            used_weekly_tokens=10_000_000,
            in_flight_estimate_tokens=0,
            new_task_estimate_tokens=2_000_000,
            five_hour=_band_5h(budget=100_000_000),
            weekly=_band_weekly(budget=800_000_000),
            concurrency=_concurrency(max_c=4),
            have_ema_warmup=True,
        )
        assert decision.five_hour_band is DispatchBand.SLOW
        # progress = (0.80 - 0.70) / 0.20 = 0.5; target = ceil(4 * 0.5) = 2
        assert decision.target_concurrency == 2

    def test_slowdown_near_top_target_one(self) -> None:
        # Pred 5h pct just below 90 -> target close to 0 (ceil keeps at 1).
        decision = compute_target_concurrency(
            used_5h_tokens=88_000_000,
            used_weekly_tokens=10_000_000,
            in_flight_estimate_tokens=0,
            new_task_estimate_tokens=1_000_000,
            five_hour=_band_5h(budget=100_000_000),
            weekly=_band_weekly(budget=800_000_000),
            concurrency=_concurrency(max_c=4),
            have_ema_warmup=True,
        )
        assert decision.five_hour_band is DispatchBand.SLOW
        assert decision.target_concurrency == 1

    def test_stopped_band_zero_concurrency(self) -> None:
        decision = compute_target_concurrency(
            used_5h_tokens=95_000_000,
            used_weekly_tokens=10_000_000,
            in_flight_estimate_tokens=0,
            new_task_estimate_tokens=1_000_000,
            five_hour=_band_5h(budget=100_000_000),
            weekly=_band_weekly(budget=800_000_000),
            concurrency=_concurrency(max_c=4),
            have_ema_warmup=True,
        )
        assert decision.five_hour_band is DispatchBand.STOPPED
        assert decision.target_concurrency == 0

    def test_weekly_more_restrictive_wins(self) -> None:
        # 5h is in FULL but weekly is in STOPPED.
        decision = compute_target_concurrency(
            used_5h_tokens=10_000_000,
            used_weekly_tokens=720_000_000,  # 90% of 800M
            in_flight_estimate_tokens=0,
            new_task_estimate_tokens=10_000_000,
            five_hour=_band_5h(budget=100_000_000),
            weekly=_band_weekly(budget=800_000_000),
            concurrency=_concurrency(max_c=4),
            have_ema_warmup=True,
        )
        assert decision.five_hour_band is DispatchBand.FULL
        assert decision.weekly_band is DispatchBand.STOPPED
        assert decision.target_concurrency == 0
        assert decision.band is DispatchBand.STOPPED

    def test_initial_concurrency_before_warmup(self) -> None:
        decision = compute_target_concurrency(
            used_5h_tokens=10_000_000,
            used_weekly_tokens=10_000_000,
            in_flight_estimate_tokens=0,
            new_task_estimate_tokens=1_000_000,
            five_hour=_band_5h(),
            weekly=_band_weekly(),
            concurrency=_concurrency(max_c=4, init=1),
            have_ema_warmup=False,
        )
        assert decision.target_concurrency == 1


class TestInFlightEstimate:
    def test_sums_predictions(self) -> None:
        tasks = [
            Task(id="a", title="a", prompt="p", model="claude-opus-4-7", effort="medium"),
            Task(id="b", title="b", prompt="p", model="claude-opus-4-7", effort="medium"),
        ]
        ema = EMAFile()
        # Cold start: prior is 2M tokens each, sum = 4M
        total = in_flight_estimate_tokens(tasks, ema, ema_settings=_ema_settings())
        assert total == 4_000_000


class TestShouldDispatch:
    def test_dispatches_when_target_above_inflight(self) -> None:
        candidate = Task(
            id="x",
            title="x",
            prompt="p",
            model="claude-opus-4-7",
            effort="medium",
        )
        ok, decision = should_dispatch(
            candidate=candidate,
            in_flight_tasks=[],
            used_5h_tokens=10_000_000,
            used_weekly_tokens=10_000_000,
            ema=EMAFile(),
            settings_throttle=ThrottleSettings(five_hour=_band_5h(), weekly=_band_weekly()),
            settings_concurrency=_concurrency(max_c=4),
            settings_ema=_ema_settings(),
            have_ema_warmup=True,
        )
        assert ok is True
        assert decision.target_concurrency == 4

    def test_refuses_when_at_target(self) -> None:
        candidate = Task(
            id="x",
            title="x",
            prompt="p",
            model="claude-opus-4-7",
            effort="medium",
        )
        in_flight = [
            Task(id=f"i{i}", title="x", prompt="p", model="claude-opus-4-7", effort="medium")
            for i in range(4)
        ]
        ok, decision = should_dispatch(
            candidate=candidate,
            in_flight_tasks=in_flight,
            used_5h_tokens=10_000_000,
            used_weekly_tokens=10_000_000,
            ema=EMAFile(),
            settings_throttle=ThrottleSettings(five_hour=_band_5h(), weekly=_band_weekly()),
            settings_concurrency=_concurrency(max_c=4),
            settings_ema=_ema_settings(),
            have_ema_warmup=True,
        )
        assert ok is False
        assert decision.target_concurrency == 4
        assert len(in_flight) == decision.target_concurrency

    def test_refuses_in_stopped_band(self) -> None:
        candidate = Task(
            id="x",
            title="x",
            prompt="p",
            model="claude-opus-4-7",
            effort="medium",
        )
        ok, decision = should_dispatch(
            candidate=candidate,
            in_flight_tasks=[],
            used_5h_tokens=95_000_000,  # well into STOPPED band
            used_weekly_tokens=10_000_000,
            ema=EMAFile(),
            settings_throttle=ThrottleSettings(five_hour=_band_5h(), weekly=_band_weekly()),
            settings_concurrency=_concurrency(max_c=4),
            settings_ema=_ema_settings(),
            have_ema_warmup=True,
        )
        assert ok is False
        assert decision.band is DispatchBand.STOPPED
