"""Tests for runner.caps — per-task token + duration ceilings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from claude_task_runner.config.schema import TaskCapsSettings
from claude_task_runner.queue.schema import Task
from claude_task_runner.runner.caps import (
    effective_duration_cap_s,
    effective_token_cap,
    evaluate_caps,
)


def _settings(
    *, max_tokens: int = 0, max_duration: float = 0, alert: float = 300, kill: float = 0
) -> TaskCapsSettings:
    return TaskCapsSettings(
        max_tokens_per_task=max_tokens,
        max_duration_s_per_task=max_duration,
        heartbeat_silence_alert_s=alert,
        heartbeat_silence_kill_s=kill,
    )


def _task(*, max_tokens: int | None = None, max_duration: float | None = None) -> Task:
    return Task(
        id="t",
        title="t",
        prompt="p",
        max_tokens_override=max_tokens,
        max_duration_s_override=max_duration,
    )


class TestEffective:
    def test_token_cap_default(self) -> None:
        s = _settings(max_tokens=10_000_000)
        assert effective_token_cap(s, _task()) == 10_000_000

    def test_token_cap_override_wins(self) -> None:
        s = _settings(max_tokens=10_000_000)
        assert effective_token_cap(s, _task(max_tokens=2_000_000)) == 2_000_000

    def test_duration_cap_default(self) -> None:
        s = _settings(max_duration=14400)
        assert effective_duration_cap_s(s, _task()) == 14400

    def test_duration_cap_override_wins(self) -> None:
        s = _settings(max_duration=14400)
        assert effective_duration_cap_s(s, _task(max_duration=600)) == 600


class TestEvaluate:
    def _now(self) -> datetime:
        return datetime(2026, 5, 3, 18, 0, tzinfo=UTC)

    def test_no_caps_no_violation(self) -> None:
        s = _settings(max_tokens=0, max_duration=0)
        violation = evaluate_caps(
            settings=s,
            task=_task(),
            cumulative_tokens=999_999_999,
            started_at=self._now(),
            now=self._now() + timedelta(hours=99),
        )
        assert violation is None

    def test_token_cap_breach(self) -> None:
        s = _settings(max_tokens=1_000_000)
        v = evaluate_caps(
            settings=s,
            task=_task(),
            cumulative_tokens=1_500_000,
            started_at=self._now(),
            now=self._now() + timedelta(seconds=10),
        )
        assert v is not None
        assert v.which == "tokens"
        assert v.observed == 1_500_000
        assert v.cap == 1_000_000

    def test_token_cap_at_exactly_cap_no_violation(self) -> None:
        s = _settings(max_tokens=1_000_000)
        v = evaluate_caps(
            settings=s,
            task=_task(),
            cumulative_tokens=1_000_000,
            started_at=self._now(),
            now=self._now() + timedelta(seconds=10),
        )
        assert v is None

    def test_duration_cap_breach(self) -> None:
        s = _settings(max_duration=600)
        v = evaluate_caps(
            settings=s,
            task=_task(),
            cumulative_tokens=0,
            started_at=self._now(),
            now=self._now() + timedelta(seconds=900),
        )
        assert v is not None
        assert v.which == "duration"
        assert v.observed == 900.0
        assert v.cap == 600.0

    def test_token_cap_takes_precedence_over_duration(self) -> None:
        s = _settings(max_tokens=1_000_000, max_duration=600)
        v = evaluate_caps(
            settings=s,
            task=_task(),
            cumulative_tokens=2_000_000,
            started_at=self._now(),
            now=self._now() + timedelta(seconds=900),
        )
        assert v is not None
        assert v.which == "tokens"

    def test_override_disables_cap(self) -> None:
        # Settings cap is 1M, but task overrides to 5M. Cumulative 2M is OK.
        s = _settings(max_tokens=1_000_000)
        v = evaluate_caps(
            settings=s,
            task=_task(max_tokens=5_000_000),
            cumulative_tokens=2_000_000,
            started_at=self._now(),
            now=self._now() + timedelta(seconds=10),
        )
        assert v is None

    def test_negative_tokens_rejected(self) -> None:
        s = _settings(max_tokens=1_000_000)
        with pytest.raises(ValueError):
            evaluate_caps(
                settings=s,
                task=_task(),
                cumulative_tokens=-1,
                started_at=self._now(),
                now=self._now(),
            )

    def test_now_before_start_rejected(self) -> None:
        s = _settings()
        with pytest.raises(ValueError):
            evaluate_caps(
                settings=s,
                task=_task(),
                cumulative_tokens=0,
                started_at=self._now(),
                now=self._now() - timedelta(seconds=1),
            )
