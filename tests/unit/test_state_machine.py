"""Tests for supervisor.state_machine — pure step function transitions."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.config.schema import Settings
from claude_task_runner.supervisor.actions import (
    EmitEvent,
    MonitorInFlight,
    Notify,
    ScheduleWakeupAt,
    StopDispatch,
)
from claude_task_runner.supervisor.state_machine import (
    StepInput,
    request_resume,
    request_stop,
    step,
)
from claude_task_runner.supervisor.states import SupervisorSnapshot, SupervisorState
from claude_task_runner.usage.drift import (
    UsageCaptureSpawnError,
    UsageCaptureTimeout,
    UsageFormatDrift,
)
from claude_task_runner.usage.models import UsageReading, WindowReading


@pytest.fixture
def settings() -> Settings:
    return load_settings(None)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 5, 4, 12, 0, tzinfo=UTC))


def _reading(
    *,
    five_pct: int,
    weekly_pct: int,
    five_resets: datetime | None = None,
    weekly_resets: datetime | None = None,
) -> UsageReading:
    return UsageReading(
        captured_at=datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
        five_hour=WindowReading(utilization_pct=five_pct, resets_at_raw="x", resets_at=five_resets),
        seven_day=WindowReading(
            utilization_pct=weekly_pct, resets_at_raw="x", resets_at=weekly_resets
        ),
    )


def _initial(state: SupervisorState = SupervisorState.IDLE) -> SupervisorSnapshot:
    return SupervisorSnapshot(
        state=state,
        since=datetime(2026, 5, 4, 11, 0, tzinfo=UTC),
    )


def _input(
    snapshot: SupervisorSnapshot,
    reading,
    settings: Settings,
    *,
    pending: int = 0,
    in_flight: int = 0,
) -> StepInput:
    return StepInput(
        snapshot=snapshot,
        reading=reading,
        settings_throttle=settings.throttle,
        settings_supervisor=settings.supervisor,
        settings_usage=settings.usage,
        pending_count=pending,
        in_flight_count=in_flight,
    )


def _action_types(actions) -> list[type]:
    return [type(a) for a in actions]


class TestIdleAndDispatching:
    def test_no_work_idle(self, settings: Settings, clock: FakeClock) -> None:
        snap = _initial(SupervisorState.DISPATCHING)
        reading = _reading(five_pct=10, weekly_pct=5)
        new, _ = step(_input(snap, reading, settings, pending=0, in_flight=0), clock)
        assert new.state is SupervisorState.IDLE

    def test_pending_work_dispatching(self, settings: Settings, clock: FakeClock) -> None:
        snap = _initial()
        reading = _reading(five_pct=10, weekly_pct=5)
        new, _ = step(_input(snap, reading, settings, pending=3), clock)
        assert new.state is SupervisorState.DISPATCHING

    def test_in_flight_only_keeps_active(self, settings: Settings, clock: FakeClock) -> None:
        snap = _initial()
        reading = _reading(five_pct=10, weekly_pct=5)
        new, _ = step(_input(snap, reading, settings, pending=0, in_flight=2), clock)
        # Not idle because in-flight count > 0
        assert new.state is SupervisorState.DISPATCHING


class TestThrottleBands:
    def test_slowdown_5h(self, settings: Settings, clock: FakeClock) -> None:
        snap = _initial(SupervisorState.DISPATCHING)
        reading = _reading(five_pct=80, weekly_pct=5)
        new, _ = step(_input(snap, reading, settings, pending=2), clock)
        assert new.state is SupervisorState.SLOWING_DOWN

    def test_throttled_5h(self, settings: Settings, clock: FakeClock) -> None:
        snap = _initial(SupervisorState.DISPATCHING)
        reading = _reading(
            five_pct=92,
            weekly_pct=5,
            five_resets=datetime(2026, 5, 4, 13, 0, tzinfo=UTC),
        )
        new, actions = step(_input(snap, reading, settings, pending=2), clock)
        assert new.state is SupervisorState.THROTTLED_5H
        assert StopDispatch in _action_types(actions)
        # Wakeup scheduled at next reset + delay (default 300s)
        wakeups = [a for a in actions if isinstance(a, ScheduleWakeupAt)]
        assert len(wakeups) == 1
        assert wakeups[0].when == datetime(2026, 5, 4, 13, 5, tzinfo=UTC)

    def test_paused_weekly(self, settings: Settings, clock: FakeClock) -> None:
        snap = _initial(SupervisorState.DISPATCHING)
        reading = _reading(
            five_pct=10,
            weekly_pct=92,
            weekly_resets=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
        )
        new, actions = step(_input(snap, reading, settings, pending=2), clock)
        assert new.state is SupervisorState.PAUSED_WEEKLY
        assert any(isinstance(a, Notify) and a.level == "warn" for a in actions)
        assert StopDispatch in _action_types(actions)


class TestEowPush:
    def test_paused_weekly_with_eow_window_open(self, settings: Settings, clock: FakeClock) -> None:
        snap = _initial(SupervisorState.PAUSED_WEEKLY)
        # Reset is 6h away (within default eow_window_s=43200) and util=92<98
        reading = _reading(
            five_pct=10,
            weekly_pct=92,
            weekly_resets=datetime(2026, 5, 4, 18, 0, tzinfo=UTC),
        )
        new, _ = step(_input(snap, reading, settings, pending=2), clock)
        assert new.state is SupervisorState.END_OF_WEEK_PUSH

    def test_eow_blocked_by_target_pct(self, settings: Settings, clock: FakeClock) -> None:
        snap = _initial(SupervisorState.PAUSED_WEEKLY)
        # Already at 99% — past eow_target_pct (default 98)
        reading = _reading(
            five_pct=10,
            weekly_pct=99,
            weekly_resets=datetime(2026, 5, 4, 18, 0, tzinfo=UTC),
        )
        new, _ = step(_input(snap, reading, settings, pending=2), clock)
        assert new.state is SupervisorState.PAUSED_WEEKLY

    def test_eow_blocked_outside_window(self, settings: Settings, clock: FakeClock) -> None:
        snap = _initial(SupervisorState.PAUSED_WEEKLY)
        # Reset is 48h away — outside default 12h eow window
        reading = _reading(
            five_pct=10,
            weekly_pct=92,
            weekly_resets=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
        )
        new, _ = step(_input(snap, reading, settings, pending=2), clock)
        assert new.state is SupervisorState.PAUSED_WEEKLY


class TestErrorDrift:
    def test_drift_enters_error(self, settings: Settings, clock: FakeClock) -> None:
        snap = _initial(SupervisorState.DISPATCHING)
        drift = UsageFormatDrift("only 1 block found")
        new, actions = step(_input(snap, drift, settings, pending=2), clock)
        assert new.state is SupervisorState.ERROR_DRIFT
        assert new.consecutive_clean_polls == 0
        assert "only 1 block" in new.last_drift_message
        assert any(isinstance(a, Notify) and a.level == "error" for a in actions)
        assert any(isinstance(a, EmitEvent) and a.event_type == "drift_detected" for a in actions)

    def test_drift_recovery_requires_n_clean_polls(
        self, settings: Settings, clock: FakeClock
    ) -> None:
        snap = _initial(SupervisorState.ERROR_DRIFT)
        snap = snap.model_copy(update={"consecutive_clean_polls": 0})
        good = _reading(
            five_pct=10,
            weekly_pct=5,
            five_resets=datetime(2026, 5, 4, 13, 0, tzinfo=UTC),
        )

        # First clean poll: still in ERROR_DRIFT, counter at 1
        new1, actions1 = step(_input(snap, good, settings, pending=2), clock)
        assert new1.state is SupervisorState.ERROR_DRIFT
        assert new1.consecutive_clean_polls == 1
        assert StopDispatch in _action_types(actions1)

        # Second clean poll: still error, counter at 2
        new2, _ = step(_input(new1, good, settings, pending=2), clock)
        assert new2.state is SupervisorState.ERROR_DRIFT
        assert new2.consecutive_clean_polls == 2

        # Third clean poll: meets threshold (default 3), recovers
        new3, _ = step(_input(new2, good, settings, pending=2), clock)
        assert new3.state is SupervisorState.DISPATCHING
        assert new3.consecutive_clean_polls == 0

    def test_drift_during_drift_resets_counter(self, settings: Settings, clock: FakeClock) -> None:
        snap = _initial(SupervisorState.ERROR_DRIFT)
        snap = snap.model_copy(update={"consecutive_clean_polls": 2})
        new, _ = step(_input(snap, UsageFormatDrift("another drift"), settings, pending=2), clock)
        assert new.state is SupervisorState.ERROR_DRIFT
        assert new.consecutive_clean_polls == 0


class TestCaptureErrors:
    def test_timeout_keeps_state(self, settings: Settings, clock: FakeClock) -> None:
        snap = _initial(SupervisorState.DISPATCHING)
        new, actions = step(_input(snap, UsageCaptureTimeout("slow"), settings, pending=2), clock)
        assert new.state is SupervisorState.DISPATCHING
        assert any(
            isinstance(a, EmitEvent) and a.event_type == "usage_capture_error" for a in actions
        )

    def test_spawn_error_keeps_state(self, settings: Settings, clock: FakeClock) -> None:
        snap = _initial(SupervisorState.DISPATCHING)
        new, _ = step(
            _input(snap, UsageCaptureSpawnError("missing"), settings, pending=2),
            clock,
        )
        assert new.state is SupervisorState.DISPATCHING


class TestStopAndResume:
    def test_stop_is_sticky(self, settings: Settings, clock: FakeClock) -> None:
        snap = request_stop(_initial(), clock=clock)
        assert snap.state is SupervisorState.STOPPED
        # Step doesn't unstick.
        reading = _reading(five_pct=10, weekly_pct=5)
        new, actions = step(_input(snap, reading, settings, pending=10), clock)
        assert new.state is SupervisorState.STOPPED
        assert MonitorInFlight in _action_types(actions)

    def test_resume_returns_to_idle(self, settings: Settings, clock: FakeClock) -> None:
        snap = request_stop(_initial(), clock=clock)
        snap = request_resume(snap, clock=clock)
        assert snap.state is SupervisorState.IDLE

    def test_resume_no_op_when_not_stopped(self, settings: Settings, clock: FakeClock) -> None:
        snap = _initial(SupervisorState.DISPATCHING)
        out = request_resume(snap, clock=clock)
        assert out is snap


class TestStateTransitionEvents:
    def test_emits_state_transition_event(self, settings: Settings, clock: FakeClock) -> None:
        snap = _initial(SupervisorState.IDLE)
        reading = _reading(five_pct=10, weekly_pct=5)
        _, actions = step(_input(snap, reading, settings, pending=2), clock)
        transition_events = [
            a for a in actions if isinstance(a, EmitEvent) and a.event_type == "state_transition"
        ]
        assert len(transition_events) == 1
        assert transition_events[0].payload["from"] == SupervisorState.IDLE.value
        assert transition_events[0].payload["to"] == SupervisorState.DISPATCHING.value
