"""Integration test: drive the supervisor through a scripted day.

Uses :class:`FakeUsageSource` to script a sequence of readings (5h
climb → throttle → reset → weekly climb → pause → EOW push → drift →
recover) and asserts the supervisor's state machine arrives at each
expected vertex.

No real ``claude`` subprocess; this test runs in milliseconds.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.config.schema import Settings
from claude_task_runner.supervisor import persistence as persist_mod
from claude_task_runner.supervisor.daemon import (
    TickContext,
    execute_actions,
    next_wakeup,
    run_one_tick,
)
from claude_task_runner.supervisor.states import SupervisorSnapshot, SupervisorState
from claude_task_runner.usage.drift import UsageFormatDrift
from claude_task_runner.usage.models import UsageReading, WindowReading


def _r(
    *,
    five: int,
    weekly: int,
    five_resets: datetime | None = None,
    weekly_resets: datetime | None = None,
    captured_at: datetime,
) -> UsageReading:
    return UsageReading(
        captured_at=captured_at,
        five_hour=WindowReading(utilization_pct=five, resets_at_raw="x", resets_at=five_resets),
        seven_day=WindowReading(utilization_pct=weekly, resets_at_raw="x", resets_at=weekly_resets),
    )


@pytest.fixture
def settings() -> Settings:
    return load_settings(None)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 5, 4, 8, 0, tzinfo=UTC))


def _run_until(
    snapshot: SupervisorSnapshot,
    poll_results: list[Any],
    settings: Settings,
    clock: FakeClock,
    *,
    pending: int = 2,
    in_flight: int = 0,
) -> tuple[SupervisorSnapshot, list[SupervisorState]]:
    """Run the state machine through ``poll_results`` and return the trail."""
    visited: list[SupervisorState] = [snapshot.state]
    events_log: list[dict[str, Any]] = []
    for result in poll_results:
        ctx = TickContext(
            settings=settings,
            poll_result=result,
            pending_count=pending,
            in_flight_count=in_flight,
        )
        snapshot, actions = run_one_tick(snapshot, ctx, clock)
        execute_actions(
            actions,
            event_callback=lambda et, payload: events_log.append({"event": et, **payload}),
        )
        visited.append(snapshot.state)
        clock.advance(60)  # 1-minute cadence
    return snapshot, visited


class TestScriptedDay:
    def test_full_lifecycle(self, settings: Settings, clock: FakeClock) -> None:
        five_reset_initial = datetime(2026, 5, 4, 13, 0, tzinfo=UTC)
        five_reset_after = datetime(2026, 5, 4, 18, 0, tzinfo=UTC)
        weekly_reset = datetime(2026, 5, 6, 12, 0, tzinfo=UTC)

        captured = clock.now()
        results = [
            # 1. Cold start: low utilization → DISPATCHING
            _r(
                five=5,
                weekly=5,
                five_resets=five_reset_initial,
                weekly_resets=weekly_reset,
                captured_at=captured,
            ),
            # 2. Climbing: 75% (slowdown band) → SLOWING_DOWN
            _r(
                five=75,
                weekly=10,
                five_resets=five_reset_initial,
                weekly_resets=weekly_reset,
                captured_at=captured,
            ),
            # 3. Hits 92% → THROTTLED_5H
            _r(
                five=92,
                weekly=15,
                five_resets=five_reset_initial,
                weekly_resets=weekly_reset,
                captured_at=captured,
            ),
            # 4. 5h reset crossed; back down to 5% with new reset target
            _r(
                five=5,
                weekly=20,
                five_resets=five_reset_after,
                weekly_resets=weekly_reset,
                captured_at=captured,
            ),
            # 5. Weekly climbs to 92% → PAUSED_WEEKLY
            _r(
                five=10,
                weekly=92,
                five_resets=five_reset_after,
                weekly_resets=weekly_reset,
                captured_at=captured,
            ),
        ]

        snap = SupervisorSnapshot(state=SupervisorState.IDLE, since=clock.now())
        final, visited = _run_until(snap, results, settings, clock, pending=3)

        assert SupervisorState.IDLE in visited
        assert SupervisorState.DISPATCHING in visited
        assert SupervisorState.SLOWING_DOWN in visited
        assert SupervisorState.THROTTLED_5H in visited
        # After 5h reset, we should be back to dispatching territory
        assert SupervisorState.DISPATCHING in visited[3:]
        assert SupervisorState.PAUSED_WEEKLY in visited
        assert final.state is SupervisorState.PAUSED_WEEKLY


class TestEowPushTrajectory:
    def test_paused_to_eow_to_pause_again(self, settings: Settings, clock: FakeClock) -> None:
        # Place clock 6h before weekly reset → inside default 12h EOW window.
        clock.set_to(datetime(2026, 5, 6, 6, 0, tzinfo=UTC))
        weekly_reset = datetime(2026, 5, 6, 12, 0, tzinfo=UTC)
        five_reset = datetime(2026, 5, 6, 11, 0, tzinfo=UTC)
        captured = clock.now()

        snap = SupervisorSnapshot(state=SupervisorState.PAUSED_WEEKLY, since=clock.now())

        # Reading 1: weekly at 92% (below 98 target, EOW window open)
        results = [
            _r(
                five=10,
                weekly=92,
                five_resets=five_reset,
                weekly_resets=weekly_reset,
                captured_at=captured,
            ),
            # Reading 2: weekly hits 98% target → back to PAUSED_WEEKLY
            _r(
                five=10,
                weekly=98,
                five_resets=five_reset,
                weekly_resets=weekly_reset,
                captured_at=captured,
            ),
        ]

        final, visited = _run_until(snap, results, settings, clock, pending=3)
        assert SupervisorState.END_OF_WEEK_PUSH in visited
        # After hitting 98% target, EOW push exits back to paused.
        assert visited[-1] is SupervisorState.PAUSED_WEEKLY
        assert final.state is SupervisorState.PAUSED_WEEKLY


class TestDriftRecovery:
    def test_drift_then_three_clean_polls(self, settings: Settings, clock: FakeClock) -> None:
        five_reset = datetime(2026, 5, 4, 13, 0, tzinfo=UTC)
        weekly_reset = datetime(2026, 5, 6, 12, 0, tzinfo=UTC)
        captured = clock.now()

        snap = SupervisorSnapshot(state=SupervisorState.IDLE, since=clock.now())
        results = [
            # 1. Initial dispatch
            _r(
                five=10,
                weekly=5,
                five_resets=five_reset,
                weekly_resets=weekly_reset,
                captured_at=captured,
            ),
            # 2. Drift detected
            UsageFormatDrift("only one block found"),
            # 3-5. Three clean polls
            _r(
                five=10,
                weekly=5,
                five_resets=five_reset,
                weekly_resets=weekly_reset,
                captured_at=captured,
            ),
            _r(
                five=10,
                weekly=5,
                five_resets=five_reset,
                weekly_resets=weekly_reset,
                captured_at=captured,
            ),
            _r(
                five=10,
                weekly=5,
                five_resets=five_reset,
                weekly_resets=weekly_reset,
                captured_at=captured,
            ),
        ]

        final, visited = _run_until(snap, results, settings, clock, pending=3)
        assert SupervisorState.ERROR_DRIFT in visited
        # After 3 clean polls (default), should recover.
        assert final.state is SupervisorState.DISPATCHING


class TestDaemonPersistence:
    def test_snapshot_persists_per_tick(
        self,
        tmp_path: Path,
        settings: Settings,
        clock: FakeClock,
    ) -> None:
        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()
        path = persist_mod.supervisor_state_path(queue_dir, settings.supervisor.state_file)

        snap = persist_mod.initial_snapshot(since=clock.now())
        five_reset = datetime(2026, 5, 4, 13, 0, tzinfo=UTC)
        weekly_reset = datetime(2026, 5, 6, 12, 0, tzinfo=UTC)
        captured = clock.now()

        ctx = TickContext(
            settings=settings,
            poll_result=_r(
                five=10,
                weekly=5,
                five_resets=five_reset,
                weekly_resets=weekly_reset,
                captured_at=captured,
            ),
            pending_count=2,
            in_flight_count=0,
        )
        new_snap, _ = run_one_tick(snap, ctx, clock)
        persist_mod.write_atomic(new_snap, path)

        loaded = persist_mod.load(path)
        assert loaded == new_snap

    def test_next_wakeup_picks_latest(self, settings: Settings, clock: FakeClock) -> None:
        # Empty actions → no wakeup
        assert next_wakeup([]) is None

        from claude_task_runner.supervisor.actions import ScheduleWakeupAt

        a = ScheduleWakeupAt(when=datetime(2026, 5, 4, 13, 0, tzinfo=UTC))
        b = ScheduleWakeupAt(when=datetime(2026, 5, 4, 14, 0, tzinfo=UTC))
        assert next_wakeup([a, b]) == b.when
        assert next_wakeup([b, a]) == b.when
