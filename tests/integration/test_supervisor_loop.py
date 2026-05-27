"""Integration test: drive the supervisor through a scripted day.

Scripts a sequence of readings (5h climb → throttle → reset →
recover → weekly throttle → drift → recovery) and asserts the
supervisor's state machine arrives at each expected vertex under the
ADR-0022 trace-following decision rule.

No real ``claude`` subprocess; this test runs in milliseconds.

Differences from the pre-ADR-0022 fixture:

* The queue-wide ``dispatch_pct.timezone`` is pinned to ``"UTC"`` so
  the day/night band selection is deterministic across hosts.
* The "weekly hits 92%" path no longer triggers a hard ``PAUSED_WEEKLY``
  state at a static floor — under ADR-0022 the weekly side is binary
  on the trace curve. Scenarios pick a ``(now, weekly_resets,
  weekly_pct)`` triple where observed > target_pct(elapsed) so the
  state machine routes to ``THROTTLED_WEEKLY``.
* The dropped ``END_OF_WEEK_PUSH`` lifecycle test is gone; the EOW
  segment is now a natural rise in the curve from ``early_pct`` to
  ``eow_pct`` rather than a distinct state.
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
    """Queue settings pinned to UTC so day/night band selection is
    deterministic.

    The default ``dispatch_pct.timezone = ""`` defers to system local;
    inside CI / on developer hosts that produces non-deterministic 5h
    band selection. We override only the timezone — every other
    ``dispatch_pct`` field keeps its default value from the package
    TOML.
    """
    base = load_settings(None)
    dp = base.dispatch_pct.model_copy(update={"timezone": "UTC"})
    return base.model_copy(update={"dispatch_pct": dp})


@pytest.fixture
def clock() -> FakeClock:
    # 08:00 UTC is squarely inside the default day band (06:00-21:00).
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
        """Climb, hit 5h stop, reset, then weekly trace catches up."""
        five_reset_initial = datetime(2026, 5, 4, 13, 0, tzinfo=UTC)
        five_reset_after = datetime(2026, 5, 4, 18, 0, tzinfo=UTC)
        # Weekly window: at clock.now()=2026-05-04 08:00 UTC, resets in
        # 2 d 4 h. Elapsed = 1 - (2.1667/7) ≈ 0.690. Breakpoint =
        # 1 - 40/168 ≈ 0.762, so elapsed < breakpoint; pre-EOW target =
        # (0.690 / 0.762) * 60 ≈ 54.4%. We use weekly_pct values below
        # and above this target to drive the weekly side.
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
            # 2. 5h climbs to 50 (day band slowdown=40/stop=60) → SLOWING_DOWN
            _r(
                five=50,
                weekly=10,
                five_resets=five_reset_initial,
                weekly_resets=weekly_reset,
                captured_at=captured,
            ),
            # 3. Hits stop: 5h=65 → THROTTLED_5H
            _r(
                five=65,
                weekly=15,
                five_resets=five_reset_initial,
                weekly_resets=weekly_reset,
                captured_at=captured,
            ),
            # 4. 5h reset crossed; back down to 5% with new reset target.
            _r(
                five=5,
                weekly=20,
                five_resets=five_reset_after,
                weekly_resets=weekly_reset,
                captured_at=captured,
            ),
            # 5. Weekly climbs to 70 — well above the ~54% trace target
            # at elapsed ≈ 0.69, so → THROTTLED_WEEKLY.
            _r(
                five=10,
                weekly=70,
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
        # After the 5h reset reading, we should have crossed back into
        # dispatching territory.
        assert SupervisorState.DISPATCHING in visited[3:]
        assert SupervisorState.THROTTLED_WEEKLY in visited
        assert final.state is SupervisorState.THROTTLED_WEEKLY


class TestWeeklyRelease:
    def test_throttled_then_drops_back(self, settings: Settings, clock: FakeClock) -> None:
        """Once observed weekly drops below the curve target, the
        supervisor returns to DISPATCHING (no separate END_OF_WEEK_PUSH
        state — the curve naturally rises from ``early_pct`` to
        ``eow_pct`` over the EOW segment)."""
        weekly_reset = datetime(2026, 5, 6, 12, 0, tzinfo=UTC)
        five_reset = datetime(2026, 5, 4, 13, 0, tzinfo=UTC)
        captured = clock.now()

        snap = SupervisorSnapshot(state=SupervisorState.IDLE, since=clock.now())

        results = [
            # 1. weekly=70 above ~54% target → THROTTLED_WEEKLY
            _r(
                five=10,
                weekly=70,
                five_resets=five_reset,
                weekly_resets=weekly_reset,
                captured_at=captured,
            ),
            # 2. weekly=10 well below target → DISPATCHING
            _r(
                five=10,
                weekly=10,
                five_resets=five_reset,
                weekly_resets=weekly_reset,
                captured_at=captured,
            ),
        ]

        final, visited = _run_until(snap, results, settings, clock, pending=3)
        assert SupervisorState.THROTTLED_WEEKLY in visited
        assert visited[-1] is SupervisorState.DISPATCHING
        assert final.state is SupervisorState.DISPATCHING


class TestDriftRecovery:
    def test_drift_then_three_clean_polls(self, settings: Settings, clock: FakeClock) -> None:
        five_reset = datetime(2026, 5, 4, 13, 0, tzinfo=UTC)
        weekly_reset = datetime(2026, 5, 6, 12, 0, tzinfo=UTC)
        captured = clock.now()

        snap = SupervisorSnapshot(state=SupervisorState.IDLE, since=clock.now())
        results = [
            # 1. Initial dispatch (low utilization, below curve target).
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
        # After 3 clean polls (default ``drift_recovery_clean_polls``),
        # the supervisor recovers.
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
        # Empty actions → no wakeup.
        assert next_wakeup([]) is None

        from claude_task_runner.supervisor.actions import ScheduleWakeupAt

        a = ScheduleWakeupAt(when=datetime(2026, 5, 4, 13, 0, tzinfo=UTC))
        b = ScheduleWakeupAt(when=datetime(2026, 5, 4, 14, 0, tzinfo=UTC))
        assert next_wakeup([a, b]) == b.when
        assert next_wakeup([b, a]) == b.when
