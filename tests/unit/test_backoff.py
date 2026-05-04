"""Tests for cron.backoff — watchdog crash-loop protection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.schema import WatchdogSettings
from claude_task_runner.cron.backoff import (
    WATCHDOG_STATE_FILENAME,
    WatchdogState,
    WatchdogStateError,
    WatchdogVerdict,
    decide,
    load_state,
    write_state_atomic,
)


def _settings(
    *,
    cooldown: float = 30.0,
    backoff_max: float = 600.0,
    threshold: int = 5,
) -> WatchdogSettings:
    return WatchdogSettings(
        restart_cooldown_s=cooldown,
        restart_backoff_max_s=backoff_max,
        crash_loop_threshold=threshold,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 5, 4, 12, 0, tzinfo=UTC))


class TestDecide:
    def test_alive_skips(self, clock: FakeClock) -> None:
        out = decide(
            state=WatchdogState(),
            supervisor_alive=True,
            settings=_settings(),
            clock=clock,
        )
        assert out.verdict is WatchdogVerdict.SKIP

    def test_dead_no_history_restarts(self, clock: FakeClock) -> None:
        out = decide(
            state=WatchdogState(),
            supervisor_alive=False,
            settings=_settings(),
            clock=clock,
        )
        assert out.verdict is WatchdogVerdict.RESTART
        assert out.new_state.recent_restarts == [clock.now()]

    def test_dead_recent_restart_cooldown(self, clock: FakeClock) -> None:
        state = WatchdogState(recent_restarts=[clock.now() - timedelta(seconds=10)])
        out = decide(
            state=state,
            supervisor_alive=False,
            settings=_settings(cooldown=30.0),
            clock=clock,
        )
        assert out.verdict is WatchdogVerdict.COOLDOWN
        # No new restart appended.
        assert out.new_state.recent_restarts == state.recent_restarts
        assert out.next_check_at is not None

    def test_cooldown_elapses_then_restart(self, clock: FakeClock) -> None:
        state = WatchdogState(recent_restarts=[clock.now() - timedelta(seconds=60)])
        out = decide(
            state=state,
            supervisor_alive=False,
            settings=_settings(cooldown=30.0),
            clock=clock,
        )
        assert out.verdict is WatchdogVerdict.RESTART

    def test_crash_loop_backoff(self, clock: FakeClock) -> None:
        # 5 restarts at threshold → backoff
        restarts = [clock.now() - timedelta(seconds=i * 35) for i in range(5)]
        state = WatchdogState(recent_restarts=list(reversed(restarts)))
        out = decide(
            state=state,
            supervisor_alive=False,
            settings=_settings(threshold=5, cooldown=30.0),
            clock=clock,
        )
        assert out.verdict is WatchdogVerdict.BACKOFF
        # First alert should be set in the new state.
        assert out.new_state.last_backoff_alerted_at is not None

    def test_alert_throttled(self, clock: FakeClock) -> None:
        # Already in backoff and alerted recently — don't re-alert.
        restarts = [clock.now() - timedelta(seconds=i * 35) for i in range(5)]
        state = WatchdogState(
            recent_restarts=list(reversed(restarts)),
            last_backoff_alerted_at=clock.now() - timedelta(seconds=60),
        )
        out = decide(
            state=state,
            supervisor_alive=False,
            settings=_settings(threshold=5),
            clock=clock,
        )
        assert out.verdict is WatchdogVerdict.BACKOFF
        # Same alert timestamp preserved (no fresh alert).
        assert out.new_state.last_backoff_alerted_at == state.last_backoff_alerted_at

    def test_backoff_window_pruning(self, clock: FakeClock) -> None:
        # Old restarts (well past 10 cooldowns) get pruned.
        old = clock.now() - timedelta(seconds=10_000)
        recent = clock.now() - timedelta(seconds=10)
        state = WatchdogState(recent_restarts=[old, recent])
        out = decide(
            state=state,
            supervisor_alive=True,
            settings=_settings(),
            clock=clock,
        )
        assert out.new_state.recent_restarts == [recent]

    def test_alert_re_emitted_after_long_silence(self, clock: FakeClock) -> None:
        # Backoff state more than 10 minutes old → re-alert.
        restarts = [clock.now() - timedelta(seconds=i * 35) for i in range(5)]
        state = WatchdogState(
            recent_restarts=list(reversed(restarts)),
            last_backoff_alerted_at=clock.now() - timedelta(seconds=900),
        )
        out = decide(
            state=state,
            supervisor_alive=False,
            settings=_settings(threshold=5),
            clock=clock,
        )
        assert out.verdict is WatchdogVerdict.BACKOFF
        assert out.new_state.last_backoff_alerted_at == clock.now()


class TestPersistence:
    def test_load_missing_returns_empty(self, tmp_path: Path) -> None:
        out = load_state(tmp_path / WATCHDOG_STATE_FILENAME)
        assert out.recent_restarts == []

    def test_round_trip(self, tmp_path: Path, clock: FakeClock) -> None:
        path = tmp_path / WATCHDOG_STATE_FILENAME
        original = WatchdogState(
            recent_restarts=[clock.now()],
            last_backoff_alerted_at=clock.now(),
        )
        write_state_atomic(original, path)
        loaded = load_state(path)
        assert loaded == original

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / WATCHDOG_STATE_FILENAME
        path.write_text("{not json")
        with pytest.raises(WatchdogStateError, match="invalid JSON"):
            load_state(path)

    def test_unknown_schema_version_raises(self, tmp_path: Path) -> None:
        path = tmp_path / WATCHDOG_STATE_FILENAME
        path.write_text('{"schema_version": 99, "recent_restarts": []}')
        with pytest.raises(WatchdogStateError, match="schema_version=99"):
            load_state(path)
