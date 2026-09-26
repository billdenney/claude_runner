"""Tests for cron.backoff — watchdog crash-loop protection."""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.schema import WatchdogSettings
from claude_task_runner.cron.backoff import (
    STALE_RESTART_AGE,
    WATCHDOG_STATE_FILENAME,
    WatchdogDecision,
    WatchdogState,
    WatchdogStateError,
    WatchdogVerdict,
    decide,
    load_state,
    stayed_up_s,
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

    def test_alive_before_it_has_stayed_up_keeps_the_history(self, clock: FakeClock) -> None:
        """Up for less than min(10 x cooldown, max) since the last restart."""
        old = clock.now() - timedelta(seconds=10_000)
        recent = clock.now() - timedelta(seconds=10)
        state = WatchdogState(recent_restarts=[old, recent])
        out = decide(state=state, supervisor_alive=True, settings=_settings(), clock=clock)
        assert out.verdict is WatchdogVerdict.SKIP
        assert out.new_state == state

    @pytest.mark.parametrize(("up_s", "cleared"), [(299.0, False), (300.0, True)])
    def test_staying_up_clears_the_history(
        self, clock: FakeClock, up_s: float, cleared: bool
    ) -> None:
        """Up 300 s (min(10 x 30, 600)) after the last restart ends the crash loop."""
        last = clock.now() - timedelta(seconds=up_s)
        state = WatchdogState(
            recent_restarts=[last - timedelta(seconds=60 * i) for i in (3, 2, 1, 0)],
            last_backoff_alerted_at=last - timedelta(seconds=30),
        )
        out = decide(state=state, supervisor_alive=True, settings=_settings(), clock=clock)
        assert out.verdict is WatchdogVerdict.SKIP
        if cleared:
            assert out.new_state == WatchdogState()
        else:
            assert out.new_state == state

    def test_stayed_up_period_follows_the_settings(self, clock: FakeClock) -> None:
        """Ten cooldowns, capped at restart_backoff_max_s."""
        assert stayed_up_s(_settings(cooldown=30.0, backoff_max=600.0)) == 300.0
        assert stayed_up_s(_settings(cooldown=100.0, backoff_max=600.0)) == 600.0
        last = clock.now() - timedelta(seconds=599)
        out = decide(
            state=WatchdogState(recent_restarts=[last]),
            supervisor_alive=True,
            settings=_settings(cooldown=100.0, backoff_max=600.0),
            clock=clock,
        )
        assert out.new_state.recent_restarts == [last]

    @pytest.mark.parametrize("alive", [True, False])
    def test_restarts_older_than_a_day_are_forgotten(self, clock: FakeClock, alive: bool) -> None:
        stale = clock.now() - STALE_RESTART_AGE
        kept = clock.now() - STALE_RESTART_AGE + timedelta(seconds=1)
        recent = clock.now() - timedelta(seconds=10)
        out = decide(
            state=WatchdogState(recent_restarts=[stale, kept, recent]),
            supervisor_alive=alive,
            settings=_settings(),
            clock=clock,
        )
        assert out.new_state.recent_restarts[:2] == [kept, recent]
        assert stale not in out.new_state.recent_restarts

    @pytest.mark.parametrize(
        ("restarts", "wait_s"),
        [(5, 60.0), (6, 120.0), (7, 240.0), (8, 480.0), (9, 600.0), (12, 600.0)],
    )
    def test_the_wait_doubles_up_to_restart_backoff_max_s(
        self, clock: FakeClock, restarts: int, wait_s: float
    ) -> None:
        """cooldown x 2 ** excess from the last restart, excess 1 at the threshold."""
        last = clock.now() - timedelta(seconds=1)
        state = WatchdogState(
            recent_restarts=[last - timedelta(seconds=60 * i) for i in reversed(range(restarts))]
        )
        out = decide(state=state, supervisor_alive=False, settings=_settings(), clock=clock)
        assert out.verdict is WatchdogVerdict.BACKOFF
        assert out.next_check_at == last + timedelta(seconds=wait_s)
        assert out.new_state.recent_restarts == state.recent_restarts
        assert out.detail == (
            f"crash loop: {restarts} restarts without the supervisor staying up 300s; "
            f"backing off until {(last + timedelta(seconds=wait_s)).isoformat()}"
        )

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


_T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
_CRON_TICK_S = 60.0
"""The crontab line a cron ``install`` adds runs the tick every minute."""


def _cron_ticks(
    settings: WatchdogSettings, *, ticks: int, survive_s: float = 0.0
) -> list[WatchdogDecision]:
    """Drive :func:`decide` once per cron tick, carrying its state along.

    The supervisor dies ``survive_s`` after each start the watchdog
    approves (``0``: it never comes up), as a crash on startup does."""
    state = WatchdogState()
    started: datetime | None = None
    decisions = []
    for k in range(ticks):
        now = _T0 + timedelta(seconds=k * _CRON_TICK_S)
        alive = started is not None and (now - started).total_seconds() < survive_s
        decision = decide(
            state=state, supervisor_alive=alive, settings=settings, clock=FakeClock(now)
        )
        if decision.verdict is WatchdogVerdict.RESTART:
            started = now
        state = decision.new_state
        decisions.append(decision)
    return decisions


def _restart_ticks(decisions: list[WatchdogDecision]) -> list[int]:
    return [k for k, d in enumerate(decisions) if d.verdict is WatchdogVerdict.RESTART]


class TestCronCadence:
    """:func:`decide` at the cadence the crontab line runs the tick."""

    def test_a_supervisor_that_never_comes_up_backs_off_to_the_max(self) -> None:
        """Five restarts a minute apart, then waits of 2, 4, 8 and 10 ticks.

        Before, restarts were counted over a 300 s window, and a timestamp
        exactly 300 s old was pruned, so at one tick a minute the count
        never reached the threshold of 5: all 120 ticks restarted.

        The sixth restart comes a tick after the fifth because the first
        wait, 2 x cooldown = 60 s, ends at the next tick. The waits then
        run 120, 240 and 480 s, and 600 s (restart_backoff_max_s) from
        there on: six restarts an hour."""
        decisions = _cron_ticks(_settings(), ticks=120)
        restarts = _restart_ticks(decisions)
        assert restarts == [0, 1, 2, 3, 4, 5, 7, 11, 19, 29, 39, 49, 59, 69, 79, 89, 99, 109, 119]
        gaps = [b - a for a, b in itertools.pairwise(restarts)]
        assert gaps == [1, 1, 1, 1, 1, 2, 4, 8] + [10] * 10
        verdicts = {d.verdict for d in decisions}
        assert verdicts == {WatchdogVerdict.RESTART, WatchdogVerdict.BACKOFF}

    def test_a_supervisor_that_dies_90s_after_each_start_backs_off(self) -> None:
        """A tick finds it up in between, but never for 300 s, so the count grows."""
        decisions = _cron_ticks(_settings(), ticks=60, survive_s=90.0)
        assert _restart_ticks(decisions) == [0, 2, 4, 6, 8, 10, 12, 16, 24, 34, 44, 54]

    def test_a_supervisor_that_stays_up_10_min_is_never_held_back(self) -> None:
        """Each run outlasts 300 s, so each crash starts a fresh count."""
        decisions = _cron_ticks(_settings(), ticks=120, survive_s=600.0)
        assert _restart_ticks(decisions) == list(range(0, 120, 10))
        assert WatchdogVerdict.BACKOFF not in {d.verdict for d in decisions}

    def test_after_staying_up_the_next_crash_restarts_at_once(self) -> None:
        """A crash loop that ended leaves no count behind."""
        loop = _cron_ticks(_settings(), ticks=30)
        state = loop[-1].new_state
        assert len(state.recent_restarts) >= 5
        last = state.recent_restarts[-1]
        up = decide(
            state=state,
            supervisor_alive=True,
            settings=_settings(),
            clock=FakeClock(last + timedelta(seconds=300)),
        )
        crashed = decide(
            state=up.new_state,
            supervisor_alive=False,
            settings=_settings(),
            clock=FakeClock(last + timedelta(seconds=360)),
        )
        assert crashed.verdict is WatchdogVerdict.RESTART
        assert crashed.detail == "restart approved (recent count: 1 of threshold 5)"


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

    def test_locked_keeps_the_history_but_forgets_stale_restarts(self, clock: FakeClock) -> None:
        """The supervisor is down, so nothing shows the crash loop is over."""
        recent = clock.now() - timedelta(seconds=10)
        old = clock.now() - timedelta(seconds=301)
        stale = clock.now() - STALE_RESTART_AGE
        out = decide(
            state=WatchdogState(recent_restarts=[stale, old, recent]),
            supervisor_alive=False,
            lock_held=True,
            settings=_settings(cooldown=30.0, backoff_max=600.0),
            clock=clock,
        )
        assert out.verdict is WatchdogVerdict.LOCKED
        assert out.new_state.recent_restarts == [old, recent]

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
