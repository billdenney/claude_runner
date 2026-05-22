"""Tests for supervisor.state_machine — pure step function transitions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
    """Default test settings — time-of-day and pacing modulation disabled.

    These tests exercise the static band-classification path; modulation
    is covered by dedicated tests further down. Each test that needs a
    different shape constructs the settings it wants.
    """
    base = load_settings(None)
    five_static = base.throttle.five_hour.model_copy(
        update={
            "daytime_band_full_dispatch_max_pct": None,
            "daytime_band_slowdown_max_pct": None,
            "nighttime_band_full_dispatch_max_pct": None,
            "nighttime_band_slowdown_max_pct": None,
        }
    )
    weekly_static = base.throttle.weekly.model_copy(
        update={"pacing_curve_enabled": False, "eow_push_nighttime_only": False}
    )
    throttle_static = base.throttle.model_copy(
        update={"five_hour": five_static, "weekly": weekly_static}
    )
    return base.model_copy(update={"throttle": throttle_static})


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
        # 50% with static bands 40/60 (modulation disabled in fixture) ⇒ slowdown.
        reading = _reading(five_pct=50, weekly_pct=5)
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


# ----------------------------------------------------------------------------
# Time-of-day and pacing-curve modulation
# ----------------------------------------------------------------------------


def _modulation_settings(
    base: Settings,
    *,
    daytime_full: int | None = None,
    daytime_slow: int | None = None,
    nighttime_full: int | None = None,
    nighttime_slow: int | None = None,
    pacing_enabled: bool = False,
    pre_eow_target_pct: int = 80,
    eow_target_pct: int | None = None,
    pacing_slack_pp: float = 10.0,
    eow_window_s: float | None = None,
    eow_push_nighttime_only: bool = False,
    timezone: str = "UTC",
    day_start: str = "06:00",
    day_end: str = "22:00",
    ramp_minutes: int = 30,
) -> Settings:
    """Build a Settings with explicit modulation knobs, anchored to UTC."""
    five = base.throttle.five_hour.model_copy(
        update={
            "daytime_band_full_dispatch_max_pct": daytime_full,
            "daytime_band_slowdown_max_pct": daytime_slow,
            "nighttime_band_full_dispatch_max_pct": nighttime_full,
            "nighttime_band_slowdown_max_pct": nighttime_slow,
        }
    )
    weekly_update: dict[str, object] = {
        "pacing_curve_enabled": pacing_enabled,
        "pre_eow_target_pct": pre_eow_target_pct,
        "pacing_slack_pp": pacing_slack_pp,
        "eow_push_nighttime_only": eow_push_nighttime_only,
    }
    if eow_target_pct is not None:
        weekly_update["eow_target_pct"] = eow_target_pct
    if eow_window_s is not None:
        weekly_update["eow_window_s"] = eow_window_s
    weekly = base.throttle.weekly.model_copy(update=weekly_update)
    tod = base.throttle.time_of_day.model_copy(
        update={
            "timezone": timezone,
            "day_start": day_start,
            "day_end": day_end,
            "ramp_minutes": ramp_minutes,
        }
    )
    throttle = base.throttle.model_copy(
        update={"five_hour": five, "weekly": weekly, "time_of_day": tod}
    )
    return base.model_copy(update={"throttle": throttle})


class TestTimeOfDayModulation:
    """Time-of-day shrinks/loosens 5h thresholds; weekly unaffected here."""

    def test_daytime_tightens_5h_to_slowdown(self, settings: Settings) -> None:
        """At noon UTC with daytime 15/30, five_pct=25 lands in SLOWING_DOWN.

        (Statically 25 would be in FULL since static band_full_dispatch=70.)
        """
        cfg = _modulation_settings(
            settings,
            daytime_full=15,
            daytime_slow=30,
            nighttime_full=50,
            nighttime_slow=75,
        )
        clock = FakeClock(datetime(2026, 5, 13, 12, 0, tzinfo=UTC))  # core day
        snap = _initial(SupervisorState.DISPATCHING)
        reading = _reading(five_pct=25, weekly_pct=5)
        new, _ = step(_input(snap, reading, cfg, pending=2), clock)
        assert new.state is SupervisorState.SLOWING_DOWN

    def test_daytime_throttles_at_low_pct(self, settings: Settings) -> None:
        """At noon UTC, five_pct=35 exceeds daytime_slow=30 → THROTTLED_5H."""
        cfg = _modulation_settings(
            settings,
            daytime_full=15,
            daytime_slow=30,
            nighttime_full=50,
            nighttime_slow=75,
        )
        clock = FakeClock(datetime(2026, 5, 13, 12, 0, tzinfo=UTC))
        snap = _initial(SupervisorState.DISPATCHING)
        reading = _reading(
            five_pct=35,
            weekly_pct=5,
            five_resets=datetime(2026, 5, 13, 13, 0, tzinfo=UTC),
        )
        new, _ = step(_input(snap, reading, cfg, pending=2), clock)
        assert new.state is SupervisorState.THROTTLED_5H

    def test_nighttime_allows_dispatching(self, settings: Settings) -> None:
        """At 02:00 UTC, five_pct=45 is still in FULL (below nighttime_full=50)."""
        cfg = _modulation_settings(
            settings,
            daytime_full=15,
            daytime_slow=30,
            nighttime_full=50,
            nighttime_slow=75,
        )
        clock = FakeClock(datetime(2026, 5, 13, 2, 0, tzinfo=UTC))
        snap = _initial(SupervisorState.DISPATCHING)
        reading = _reading(five_pct=45, weekly_pct=5)
        new, _ = step(_input(snap, reading, cfg, pending=2), clock)
        assert new.state is SupervisorState.DISPATCHING

    def test_nighttime_slowdown_above_50(self, settings: Settings) -> None:
        """At 02:00 UTC, five_pct=60 enters nighttime slowdown band (50-75)."""
        cfg = _modulation_settings(
            settings,
            daytime_full=15,
            daytime_slow=30,
            nighttime_full=50,
            nighttime_slow=75,
        )
        clock = FakeClock(datetime(2026, 5, 13, 2, 0, tzinfo=UTC))
        snap = _initial(SupervisorState.DISPATCHING)
        reading = _reading(five_pct=60, weekly_pct=5)
        new, _ = step(_input(snap, reading, cfg, pending=2), clock)
        assert new.state is SupervisorState.SLOWING_DOWN

    def test_daytime_only_override_uses_static_at_night(self, settings: Settings) -> None:
        """If only ``daytime_*`` is set, nighttime falls back to ``band_*``.

        Static band 40/60 (PR #13 defaults), so at 02:00 UTC five_pct=50 is in slow.
        """
        cfg = _modulation_settings(
            settings,
            daytime_full=15,
            daytime_slow=30,
            # nighttime fields left None → fall back to static (40/60)
        )
        clock = FakeClock(datetime(2026, 5, 13, 2, 0, tzinfo=UTC))
        snap = _initial(SupervisorState.DISPATCHING)
        reading = _reading(
            five_pct=50,
            weekly_pct=5,
            five_resets=datetime(2026, 5, 13, 5, 0, tzinfo=UTC),
        )
        new, _ = step(_input(snap, reading, cfg, pending=2), clock)
        assert new.state is SupervisorState.SLOWING_DOWN

    def test_no_overrides_uses_static(self, settings: Settings) -> None:
        """With all override fields None, 5h thresholds are exactly the static bands."""
        cfg = _modulation_settings(settings)  # all override fields None
        clock = FakeClock(datetime(2026, 5, 13, 12, 0, tzinfo=UTC))
        snap = _initial(SupervisorState.DISPATCHING)
        reading = _reading(five_pct=50, weekly_pct=5)
        new, _ = step(_input(snap, reading, cfg, pending=2), clock)
        # Static 40/60: 40 <= 50 < 60 → SLOWING_DOWN
        assert new.state is SupervisorState.SLOWING_DOWN


class TestPacingCurveModulation:
    """Dynamic pacing curve shifts weekly bands by observed-vs-target deviation."""

    def test_ahead_of_target_tightens(self, settings: Settings) -> None:
        """At mid-week (elapsed=0.5) target~47%; observed=80 → tighter weekly bands.

        Target 0.5 of pre-EOW segment (0 to 80): target = 0.5/0.85 * 80 ≈ 47.
        Deviation = 80 - 47 = 33; slack 10 → shift 23 → weekly_full = 70-23 = 47.
        weekly_pct=80 ≥ 47 (effective full) → at least SLOWING_DOWN.
        """
        clock = FakeClock(datetime(2026, 5, 13, 12, 0, tzinfo=UTC))
        weekly_reset = clock.now() + timedelta(days=3, hours=12)  # ~half-week away
        cfg = _modulation_settings(settings, pacing_enabled=True)
        snap = _initial(SupervisorState.DISPATCHING)
        reading = _reading(
            five_pct=5,
            weekly_pct=80,
            weekly_resets=weekly_reset,
        )
        new, _ = step(_input(snap, reading, cfg, pending=2), clock)
        # Weekly slot in slowdown or stop band due to tightening. PR 9
        # split the weekly-in-stop band from THROTTLED_5H into the new
        # THROTTLED_WEEKLY state so the operator can tell which window
        # caused the throttle. Both throttle states (5h, weekly) and the
        # SLOWING_DOWN band are acceptable here — the precise effective
        # band depends on pacing math, which other tests pin tightly.
        assert new.state in {
            SupervisorState.SLOWING_DOWN,
            SupervisorState.THROTTLED_5H,
            SupervisorState.THROTTLED_WEEKLY,
        }

    def test_behind_target_keeps_dispatching(self, settings: Settings) -> None:
        """Observed well behind target → bands loosen → DISPATCHING.

        At elapsed=0.5, target~47%, observed=5: deviation negative beyond slack
        loosens bands. five_pct stays low → DISPATCHING.
        """
        clock = FakeClock(datetime(2026, 5, 13, 12, 0, tzinfo=UTC))
        weekly_reset = clock.now() + timedelta(days=3, hours=12)
        cfg = _modulation_settings(settings, pacing_enabled=True)
        snap = _initial(SupervisorState.DISPATCHING)
        reading = _reading(five_pct=5, weekly_pct=5, weekly_resets=weekly_reset)
        new, _ = step(_input(snap, reading, cfg, pending=2), clock)
        assert new.state is SupervisorState.DISPATCHING

    def test_no_resets_at_falls_back_to_static(self, settings: Settings) -> None:
        """Without a ``resets_at`` we can't pace — use the static bands."""
        clock = FakeClock(datetime(2026, 5, 13, 12, 0, tzinfo=UTC))
        cfg = _modulation_settings(settings, pacing_enabled=True)
        snap = _initial(SupervisorState.DISPATCHING)
        # weekly=75 is in static slowdown (70-90); no resets_at provided.
        reading = _reading(five_pct=5, weekly_pct=75)
        new, _ = step(_input(snap, reading, cfg, pending=2), clock)
        assert new.state is SupervisorState.SLOWING_DOWN

    def test_pacing_disabled_uses_static(self, settings: Settings) -> None:
        """``pacing_curve_enabled = False`` ⇒ bands match the static config."""
        clock = FakeClock(datetime(2026, 5, 13, 12, 0, tzinfo=UTC))
        weekly_reset = clock.now() + timedelta(days=3)
        cfg = _modulation_settings(settings, pacing_enabled=False)
        snap = _initial(SupervisorState.DISPATCHING)
        # weekly=80 statically would be in slowdown (70-90) → SLOWING_DOWN.
        reading = _reading(five_pct=5, weekly_pct=80, weekly_resets=weekly_reset)
        new, _ = step(_input(snap, reading, cfg, pending=2), clock)
        assert new.state is SupervisorState.SLOWING_DOWN

    def test_pause_floor_never_overridden(self, settings: Settings) -> None:
        """Even if behind target (curve would loosen), pause_at_pct is the floor."""
        clock = FakeClock(datetime(2026, 5, 13, 12, 0, tzinfo=UTC))
        weekly_reset = clock.now() + timedelta(days=3)
        cfg = _modulation_settings(settings, pacing_enabled=True)
        snap = _initial(SupervisorState.DISPATCHING)
        # weekly=92 >= pause_at_pct=90 → must be PAUSED_WEEKLY regardless of curve
        reading = _reading(
            five_pct=5,
            weekly_pct=92,
            weekly_resets=weekly_reset,
        )
        new, _ = step(_input(snap, reading, cfg, pending=2), clock)
        assert new.state is SupervisorState.PAUSED_WEEKLY


class TestEowPushNighttimeBias:
    """``eow_push_nighttime_only`` gates PAUSED_WEEKLY → END_OF_WEEK_PUSH to night."""

    def test_daytime_blocks_eow_push(self, settings: Settings) -> None:
        """At noon UTC core daytime, the bias keeps state in PAUSED_WEEKLY."""
        clock = FakeClock(datetime(2026, 5, 13, 12, 0, tzinfo=UTC))
        weekly_reset = datetime(2026, 5, 13, 18, 0, tzinfo=UTC)  # 6h ahead, EOW window
        cfg = _modulation_settings(settings, eow_push_nighttime_only=True)
        snap = _initial(SupervisorState.PAUSED_WEEKLY)
        reading = _reading(five_pct=10, weekly_pct=92, weekly_resets=weekly_reset)
        new, _ = step(_input(snap, reading, cfg, pending=2), clock)
        assert new.state is SupervisorState.PAUSED_WEEKLY

    def test_nighttime_allows_eow_push(self, settings: Settings) -> None:
        """At 02:00 UTC core nighttime, the bias allows the transition."""
        clock = FakeClock(datetime(2026, 5, 13, 2, 0, tzinfo=UTC))
        weekly_reset = datetime(2026, 5, 13, 8, 0, tzinfo=UTC)  # 6h ahead, EOW window
        cfg = _modulation_settings(settings, eow_push_nighttime_only=True)
        snap = _initial(SupervisorState.PAUSED_WEEKLY)
        reading = _reading(five_pct=10, weekly_pct=92, weekly_resets=weekly_reset)
        new, _ = step(_input(snap, reading, cfg, pending=2), clock)
        assert new.state is SupervisorState.END_OF_WEEK_PUSH

    def test_morning_ramp_still_blocks(self, settings: Settings) -> None:
        """At 06:00 UTC the morning ramp is active — not core nighttime, gate closed."""
        clock = FakeClock(datetime(2026, 5, 13, 6, 0, tzinfo=UTC))
        weekly_reset = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
        cfg = _modulation_settings(settings, eow_push_nighttime_only=True)
        snap = _initial(SupervisorState.PAUSED_WEEKLY)
        reading = _reading(five_pct=10, weekly_pct=92, weekly_resets=weekly_reset)
        new, _ = step(_input(snap, reading, cfg, pending=2), clock)
        assert new.state is SupervisorState.PAUSED_WEEKLY

    def test_bias_disabled_allows_daytime_push(self, settings: Settings) -> None:
        """With ``eow_push_nighttime_only=False``, daytime EOW push fires as before."""
        clock = FakeClock(datetime(2026, 5, 13, 12, 0, tzinfo=UTC))
        weekly_reset = datetime(2026, 5, 13, 18, 0, tzinfo=UTC)
        cfg = _modulation_settings(settings, eow_push_nighttime_only=False)
        snap = _initial(SupervisorState.PAUSED_WEEKLY)
        reading = _reading(five_pct=10, weekly_pct=92, weekly_resets=weekly_reset)
        new, _ = step(_input(snap, reading, cfg, pending=2), clock)
        assert new.state is SupervisorState.END_OF_WEEK_PUSH
