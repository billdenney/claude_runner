"""Tests for cron.backoff — watchdog crash-loop protection."""

from __future__ import annotations

import json
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
from claude_task_runner.queue.schema import CURRENT_SCHEMA_VERSION


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


class TestLocked:
    """Another process holds global.lock, so a restart would exit at once."""

    def test_dead_supervisor_with_the_lock_held_is_locked(self, clock: FakeClock) -> None:
        out = decide(
            state=WatchdogState(),
            supervisor_alive=False,
            lock_held=True,
            lock_holder_pid=4321,
            settings=_settings(),
            clock=clock,
        )
        assert out.verdict is WatchdogVerdict.LOCKED
        assert out.detail == (
            "another supervisor (pid 4321) holds global.lock; starting none until it exits"
        )
        assert out.next_check_at is None
        # No restart is counted, so none can hold back the one after the lock frees.
        assert out.new_state == WatchdogState()

    def test_holder_pid_unknown(self, clock: FakeClock) -> None:
        out = decide(
            state=WatchdogState(),
            supervisor_alive=False,
            lock_held=True,
            settings=_settings(),
            clock=clock,
        )
        assert out.verdict is WatchdogVerdict.LOCKED
        assert out.detail == "another supervisor holds global.lock; starting none until it exits"

    def test_alive_supervisor_is_skipped_whoever_holds_the_lock(self, clock: FakeClock) -> None:
        out = decide(
            state=WatchdogState(),
            supervisor_alive=True,
            lock_held=True,
            lock_holder_pid=4321,
            settings=_settings(),
            clock=clock,
        )
        assert out.verdict is WatchdogVerdict.SKIP

    @pytest.mark.parametrize(
        ("ages_s", "unlocked"),
        [
            pytest.param([10.0], WatchdogVerdict.COOLDOWN, id="would-cool-down"),
            pytest.param(
                [50.0, 40.0, 30.0, 20.0, 10.0], WatchdogVerdict.BACKOFF, id="would-back-off"
            ),
        ],
    )
    def test_lock_comes_before_cooldown_and_backoff(
        self, clock: FakeClock, ages_s: list[float], unlocked: WatchdogVerdict
    ) -> None:
        """The state is kept as it is: no restart, and no crash-loop alert."""
        state = WatchdogState(
            recent_restarts=[clock.now() - timedelta(seconds=age) for age in ages_s]
        )
        free = decide(state=state, supervisor_alive=False, settings=_settings(), clock=clock)
        assert free.verdict is unlocked
        out = decide(
            state=state,
            supervisor_alive=False,
            lock_held=True,
            lock_holder_pid=4321,
            settings=_settings(),
            clock=clock,
        )
        assert out.verdict is WatchdogVerdict.LOCKED
        assert out.new_state == state

    def test_locked_still_ages_out_old_restarts(self, clock: FakeClock) -> None:
        recent = clock.now() - timedelta(seconds=10)
        old = clock.now() - timedelta(seconds=301)
        out = decide(
            state=WatchdogState(recent_restarts=[old, recent]),
            supervisor_alive=False,
            lock_held=True,
            settings=_settings(cooldown=30.0, backoff_max=600.0),
            clock=clock,
        )
        assert out.verdict is WatchdogVerdict.LOCKED
        assert out.new_state.recent_restarts == [recent]

    def test_lock_defaults_to_free(self, clock: FakeClock) -> None:
        out = decide(
            state=WatchdogState(), supervisor_alive=False, settings=_settings(), clock=clock
        )
        assert out.verdict is WatchdogVerdict.RESTART


class TestStateQueue:
    """The restart history names the queue it belongs to."""

    def test_round_trip(self, tmp_path: Path, clock: FakeClock) -> None:
        path = tmp_path / WATCHDOG_STATE_FILENAME
        original = WatchdogState(queue=tmp_path / "q", recent_restarts=[clock.now()])
        write_state_atomic(original, path)
        assert load_state(path) == original
        assert json.loads(path.read_text(encoding="utf-8"))["queue"] == str(tmp_path / "q")

    def test_file_from_before_the_field_loads_as_no_queue(self, tmp_path: Path) -> None:
        path = tmp_path / WATCHDOG_STATE_FILENAME
        path.write_text(
            json.dumps(
                {
                    "schema_version": CURRENT_SCHEMA_VERSION,
                    "recent_restarts": ["2026-09-26T12:00:00Z"],
                }
            ),
            encoding="utf-8",
        )
        loaded = load_state(path)
        assert loaded.queue is None
        assert loaded.recent_restarts == [datetime(2026, 9, 26, 12, 0, tzinfo=UTC)]

    def test_decide_keeps_the_queue(self, tmp_path: Path, clock: FakeClock) -> None:
        state = WatchdogState(queue=tmp_path / "q")
        for alive, lock_held in ((True, False), (False, True), (False, False)):
            out = decide(
                state=state,
                supervisor_alive=alive,
                lock_held=lock_held,
                settings=_settings(),
                clock=clock,
            )
            assert out.new_state.queue == tmp_path / "q"
