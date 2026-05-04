"""Tests for runner.runtime_stats — predictions over EMA."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.schema import EMAPrior, EMASettings
from claude_task_runner.queue.schema import Task
from claude_task_runner.runner.ema import (
    EMAFile,
    task_type_key,
    update_bucket,
)
from claude_task_runner.runner.runtime_stats import (
    fits_in_window,
    has_warm_samples,
    p90_duration_s,
    p90_tokens,
)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 5, 3, 18, 0, tzinfo=UTC))


def _settings(warmup: int = 3, multiplier: float = 1.5) -> EMASettings:
    return EMASettings(
        alpha=0.3,
        prior_warmup_samples=warmup,
        runtime_p90_multiplier=multiplier,
        priors={
            "claude-opus-4-7": {
                "high": EMAPrior(tokens=3_000_000, duration_s=1800),
            },
        },
    )


def _task() -> Task:
    return Task(
        id="t",
        title="t",
        prompt="p",
        model="claude-opus-4-7",
        effort="high",
    )


class TestP90:
    def test_tokens_uses_multiplier(self) -> None:
        ema = EMAFile()
        # Cold start: prior is 3M tokens, multiplier 1.5 -> 4.5M
        assert p90_tokens(ema, _task(), settings=_settings()) == 4_500_000.0

    def test_duration_uses_multiplier(self) -> None:
        ema = EMAFile()
        assert p90_duration_s(ema, _task(), settings=_settings()) == 2700.0

    def test_custom_multiplier(self) -> None:
        ema = EMAFile()
        assert p90_duration_s(ema, _task(), settings=_settings(multiplier=2.0)) == 3600.0


class TestFitsInWindow:
    def test_non_positive_window_rejects(self) -> None:
        ema = EMAFile()
        assert (
            fits_in_window(
                ema,
                _task(),
                settings=_settings(),
                seconds_until_reset=0,
                safety_factor=0.5,
            )
            is False
        )

    def test_short_task_fits(self, clock: FakeClock) -> None:
        ema = EMAFile()
        # Cold-start p90 = 2700s. With reset 30000s away and factor 0.5,
        # threshold = 15000s. 2700 < 15000 -> fits.
        assert (
            fits_in_window(
                ema,
                _task(),
                settings=_settings(),
                seconds_until_reset=30000,
                safety_factor=0.5,
            )
            is True
        )

    def test_long_task_does_not_fit(self) -> None:
        ema = EMAFile()
        # threshold = 1000 * 0.5 = 500s, p90 = 2700s. Doesn't fit.
        assert (
            fits_in_window(
                ema,
                _task(),
                settings=_settings(),
                seconds_until_reset=1000,
                safety_factor=0.5,
            )
            is False
        )


class TestHasWarmSamples:
    def test_no_bucket(self) -> None:
        assert has_warm_samples(EMAFile(), _task(), settings=_settings()) is False

    def test_below_threshold(self, clock: FakeClock) -> None:
        task = _task()
        ema = EMAFile()
        ema = update_bucket(
            ema,
            task_type_key(task),
            observed_tokens=1,
            observed_duration_s=1,
            clock=clock,
            alpha=0.3,
        )
        # 1 sample with warmup=3
        assert has_warm_samples(ema, task, settings=_settings(warmup=3)) is False

    def test_at_threshold(self, clock: FakeClock) -> None:
        task = _task()
        ema = EMAFile()
        for _ in range(3):
            ema = update_bucket(
                ema,
                task_type_key(task),
                observed_tokens=1,
                observed_duration_s=1,
                clock=clock,
                alpha=0.3,
            )
        assert has_warm_samples(ema, task, settings=_settings(warmup=3)) is True
