"""Tests for runner.heartbeat — silence detection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from claude_task_runner.config.schema import TaskCapsSettings
from claude_task_runner.runner.heartbeat import (
    HeartbeatStatus,
    HeartbeatVerdict,
    evaluate,
)


def _settings(alert: float, kill: float = 0) -> TaskCapsSettings:
    return TaskCapsSettings(
        max_tokens_per_task=0,
        max_duration_s_per_task=0,
        heartbeat_silence_alert_s=alert,
        heartbeat_silence_kill_s=kill,
    )


class TestEvaluate:
    def _now(self) -> datetime:
        return datetime(2026, 5, 3, 18, 0, tzinfo=UTC)

    def test_healthy_within_alert(self) -> None:
        s = _settings(alert=300)
        status = evaluate(
            settings=s,
            last_heartbeat_at=self._now(),
            started_at=self._now(),
            now=self._now() + timedelta(seconds=60),
        )
        assert status.verdict is HeartbeatVerdict.HEALTHY
        assert status.silence_s == 60.0

    def test_no_heartbeat_uses_started_at(self) -> None:
        s = _settings(alert=300)
        status = evaluate(
            settings=s,
            last_heartbeat_at=None,
            started_at=self._now(),
            now=self._now() + timedelta(seconds=120),
        )
        assert status.verdict is HeartbeatVerdict.HEALTHY
        assert status.silence_s == 120.0

    def test_silent_after_alert(self) -> None:
        s = _settings(alert=300)
        status = evaluate(
            settings=s,
            last_heartbeat_at=self._now(),
            started_at=self._now(),
            now=self._now() + timedelta(seconds=400),
        )
        assert status.verdict is HeartbeatVerdict.SILENT

    def test_kill_after_kill_threshold(self) -> None:
        s = _settings(alert=300, kill=900)
        status = evaluate(
            settings=s,
            last_heartbeat_at=self._now(),
            started_at=self._now(),
            now=self._now() + timedelta(seconds=1000),
        )
        assert status.verdict is HeartbeatVerdict.KILL

    def test_kill_zero_disables_kill(self) -> None:
        s = _settings(alert=300, kill=0)
        status = evaluate(
            settings=s,
            last_heartbeat_at=self._now(),
            started_at=self._now(),
            now=self._now() + timedelta(hours=10),
        )
        assert status.verdict is HeartbeatVerdict.SILENT

    def test_now_before_start_rejected(self) -> None:
        s = _settings(alert=300)
        with pytest.raises(ValueError):
            evaluate(
                settings=s,
                last_heartbeat_at=self._now(),
                started_at=self._now(),
                now=self._now() - timedelta(seconds=1),
            )

    def test_future_heartbeat_rejected(self) -> None:
        s = _settings(alert=300)
        with pytest.raises(ValueError):
            evaluate(
                settings=s,
                last_heartbeat_at=self._now() + timedelta(seconds=10),
                started_at=self._now(),
                now=self._now(),
            )

    def test_baseline_picks_max(self) -> None:
        # If last_heartbeat is in the past relative to started_at (a
        # non-real-world quirk), evaluate() uses last_heartbeat as
        # baseline (whichever lets us measure silence from the most
        # recent confirmed liveness).
        s = _settings(alert=300)
        started = self._now()
        last_hb = self._now() + timedelta(seconds=400)
        now = self._now() + timedelta(seconds=600)
        status = evaluate(
            settings=s,
            last_heartbeat_at=last_hb,
            started_at=started,
            now=now,
        )
        # silence = 600 - 400 = 200 -> healthy
        assert status.verdict is HeartbeatVerdict.HEALTHY


_STARTED_AT = datetime(2026, 5, 3, 18, 0, tzinfo=UTC)

# Silence runs from last_heartbeat_at when one is recorded, else from
# started_at. The heartbeat case puts the last event 60 s into the run, so
# measuring from the wrong baseline shows up as a silence_s 60 s off.
_BOTH_BASELINES = pytest.mark.parametrize(
    "last_heartbeat_at",
    [
        pytest.param(_STARTED_AT + timedelta(seconds=60), id="last_heartbeat_at"),
        pytest.param(None, id="started_at"),
    ],
)

# The alert boundary must hold both with the kill threshold off and with it
# set above the alert threshold.
_KILL_OFF_OR_ABOVE_ALERT = pytest.mark.parametrize(
    "kill", [pytest.param(0, id="kill_off"), pytest.param(900, id="kill_900")]
)


def _evaluate_after_silence(
    silence_s: int, *, alert: float, kill: float, last_heartbeat_at: datetime | None
) -> HeartbeatStatus:
    """evaluate() at the instant the run has been silent for ``silence_s``."""
    baseline = _STARTED_AT if last_heartbeat_at is None else last_heartbeat_at
    return evaluate(
        settings=_settings(alert=alert, kill=kill),
        last_heartbeat_at=last_heartbeat_at,
        started_at=_STARTED_AT,
        now=baseline + timedelta(seconds=silence_s),
    )


class TestEvaluateThresholdBoundaries:
    """Both thresholds are strict: silence exactly at a threshold does not
    escalate, and one second over it does."""

    @_BOTH_BASELINES
    def test_silence_at_kill_threshold_is_silent_not_kill(
        self, last_heartbeat_at: datetime | None
    ) -> None:
        status = _evaluate_after_silence(
            900, alert=300, kill=900, last_heartbeat_at=last_heartbeat_at
        )
        assert status == HeartbeatStatus(verdict=HeartbeatVerdict.SILENT, silence_s=900.0)

    @_BOTH_BASELINES
    def test_silence_one_second_over_kill_threshold_is_kill(
        self, last_heartbeat_at: datetime | None
    ) -> None:
        status = _evaluate_after_silence(
            901, alert=300, kill=900, last_heartbeat_at=last_heartbeat_at
        )
        assert status == HeartbeatStatus(verdict=HeartbeatVerdict.KILL, silence_s=901.0)

    @_BOTH_BASELINES
    @_KILL_OFF_OR_ABOVE_ALERT
    def test_silence_at_alert_threshold_is_healthy(
        self, kill: float, last_heartbeat_at: datetime | None
    ) -> None:
        status = _evaluate_after_silence(
            300, alert=300, kill=kill, last_heartbeat_at=last_heartbeat_at
        )
        assert status == HeartbeatStatus(verdict=HeartbeatVerdict.HEALTHY, silence_s=300.0)

    @_BOTH_BASELINES
    @_KILL_OFF_OR_ABOVE_ALERT
    def test_silence_one_second_over_alert_threshold_is_silent(
        self, kill: float, last_heartbeat_at: datetime | None
    ) -> None:
        status = _evaluate_after_silence(
            301, alert=300, kill=kill, last_heartbeat_at=last_heartbeat_at
        )
        assert status == HeartbeatStatus(verdict=HeartbeatVerdict.SILENT, silence_s=301.0)
