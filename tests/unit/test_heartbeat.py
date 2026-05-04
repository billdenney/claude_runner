"""Tests for runner.heartbeat — silence detection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from claude_task_runner.config.schema import TaskCapsSettings
from claude_task_runner.runner.heartbeat import (
    HeartbeatVerdict,
    evaluate,
    silence_window,
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


class TestSilenceWindow:
    def test_kill_zero_returns_none(self) -> None:
        s = _settings(alert=300, kill=0)
        alert, kill = silence_window(s)
        assert alert == 300
        assert kill is None

    def test_kill_set_returns_value(self) -> None:
        s = _settings(alert=300, kill=900)
        alert, kill = silence_window(s)
        assert alert == 300
        assert kill == 900
