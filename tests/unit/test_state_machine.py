"""Tests for ``supervisor.state_machine`` — pure step function transitions.

The state machine is a thin translator from the throttle package's
:class:`Decision` into a ``(snapshot, actions)`` tuple plus the
non-decision concerns (STOPPED stickiness, IDLE classification,
ERROR_DRIFT routing, capture-error skip). The throttle math itself is
exercised in :mod:`tests.unit.test_decision`; here we cover:

* STOPPED stickiness.
* IDLE classification when there's no pending work and nothing in
  flight.
* ERROR_DRIFT routing for :class:`UsageFormatDrift` /
  :class:`UsageApiAuthExpired` and recovery via N clean polls.
* :class:`UsageCaptureSpawnError` / :class:`UsageCaptureTimeout` skip
  the tick without state change.
* End-to-end DISPATCHING / SLOWING_DOWN / THROTTLED_5H /
  THROTTLED_WEEKLY classification driven by a hand-built
  :class:`ResolvedPolicy`.
* Action and event emission on transitions.
* Wakeup scheduling.
* ``request_stop`` / ``request_resume``.
* ``all_states`` enumerates the seven surviving states.

Dropped tests (removed because the underlying mechanism no longer
exists post-ADR-0022):

* ``TestPacingCurveModulation`` — there is no slack-and-shift any more;
  weekly is binary trace-following (covered in test_decision.py).
* ``TestEowPushNighttimeBias`` — ``END_OF_WEEK_PUSH`` is gone.
* ``TestPausedWeekly`` (the old version) — ``PAUSED_WEEKLY`` is gone;
  its role is subsumed by ``THROTTLED_WEEKLY``.
* The ``TestTimeOfDayModulation`` band-override tests — overrides are
  now expressed as the day/night ``ResolvedBand`` pair on the policy,
  not as overlay percentages. The 5h-decision branch is exercised
  in ``test_decision.py`` and the day/night-band selection in
  ``test_dispatch_time_of_day.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.config.schema import SupervisorSettings, UsageSettings
from claude_task_runner.supervisor.actions import (
    EmitEvent,
    MonitorInFlight,
    Notify,
    ScheduleWakeupAt,
    StopDispatch,
)
from claude_task_runner.supervisor.state_machine import (
    StepInput,
    all_states,
    request_resume,
    request_stop,
    step,
)
from claude_task_runner.supervisor.states import SupervisorSnapshot, SupervisorState
from claude_task_runner.throttle.curve import SEVEN_DAYS_S
from claude_task_runner.throttle.policy import (
    ResolvedBand,
    ResolvedNight,
    ResolvedPolicy,
    ResolvedWeek,
)
from claude_task_runner.usage.drift import (
    UsageApiAuthExpired,
    UsageCaptureSpawnError,
    UsageCaptureTimeout,
    UsageFormatDrift,
)
from claude_task_runner.usage.models import UsageReading, WindowReading

# ----------------------------------------------------------------------------
# Fixtures and helpers
# ----------------------------------------------------------------------------


def _policy(
    *,
    max_concurrency: int = 5,
    timezone: str = "UTC",
    day_slow: int = 40,
    day_stop: int = 60,
    night_slow: int = 70,
    night_stop: int = 90,
    night_start: time = time(21, 0),
    night_end: time = time(6, 0),
    early_pct: int = 60,
    eow_pct: int = 95,
    eow_switch_s: float = 40 * 3600,
) -> ResolvedPolicy:
    """Build a :class:`ResolvedPolicy` directly, bypassing the loader.

    Matches the helper in ``test_decision.py``; pinning ``timezone="UTC"``
    keeps the day/night band selection deterministic across hosts.
    """
    return ResolvedPolicy(
        account_name="t",
        max_concurrency=max_concurrency,
        timezone=timezone,
        day=ResolvedBand(fivehr_slowdown_pct=day_slow, fivehr_stop_pct=day_stop),
        night=ResolvedNight(
            fivehr_slowdown_pct=night_slow,
            fivehr_stop_pct=night_stop,
            time_start=night_start,
            time_end=night_end,
        ),
        week=ResolvedWeek(early_pct=early_pct, eow_pct=eow_pct, eow_time_switch_s=eow_switch_s),
    )


@pytest.fixture
def policy() -> ResolvedPolicy:
    return _policy()


@pytest.fixture
def clock() -> FakeClock:
    # Noon UTC — squarely inside the default day band (night 21:00-06:00).
    return FakeClock(datetime(2026, 5, 4, 12, 0, tzinfo=UTC))


@pytest.fixture
def _base_settings():
    """Loaded once per test — the package TOML carries the full default
    population for nested settings classes whose pydantic models have
    no field defaults."""
    return load_settings(None)


@pytest.fixture
def supervisor_settings(_base_settings) -> SupervisorSettings:
    return _base_settings.supervisor


@pytest.fixture
def usage_settings(_base_settings) -> UsageSettings:
    return _base_settings.usage


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
    policy: ResolvedPolicy,
    supervisor_settings: SupervisorSettings,
    usage_settings: UsageSettings,
    *,
    pending: int = 0,
    in_flight: int = 0,
) -> StepInput:
    return StepInput(
        snapshot=snapshot,
        reading=reading,
        policy=policy,
        settings_supervisor=supervisor_settings,
        settings_usage=usage_settings,
        pending_count=pending,
        in_flight_count=in_flight,
    )


def _action_types(actions) -> list[type]:
    return [type(a) for a in actions]


# ----------------------------------------------------------------------------
# IDLE / DISPATCHING
# ----------------------------------------------------------------------------


class TestIdleAndDispatching:
    def test_no_work_no_in_flight_idle(
        self,
        policy: ResolvedPolicy,
        clock: FakeClock,
        supervisor_settings: SupervisorSettings,
        usage_settings: UsageSettings,
    ) -> None:
        """No pending tasks AND no in-flight tasks → IDLE."""
        snap = _initial(SupervisorState.DISPATCHING)
        reading = _reading(five_pct=10, weekly_pct=5)
        new, actions = step(
            _input(snap, reading, policy, supervisor_settings, usage_settings),
            clock,
        )
        assert new.state is SupervisorState.IDLE
        assert MonitorInFlight in _action_types(actions)

    def test_pending_work_dispatching(
        self,
        policy: ResolvedPolicy,
        clock: FakeClock,
        supervisor_settings: SupervisorSettings,
        usage_settings: UsageSettings,
    ) -> None:
        """Pending tasks at low utilization → DISPATCHING."""
        snap = _initial()
        reading = _reading(
            five_pct=10,
            weekly_pct=5,
            five_resets=clock.now() + timedelta(hours=2),
            weekly_resets=clock.now() + timedelta(days=4),
        )
        new, _ = step(
            _input(
                snap,
                reading,
                policy,
                supervisor_settings,
                usage_settings,
                pending=3,
            ),
            clock,
        )
        assert new.state is SupervisorState.DISPATCHING

    def test_in_flight_only_keeps_active(
        self,
        policy: ResolvedPolicy,
        clock: FakeClock,
        supervisor_settings: SupervisorSettings,
        usage_settings: UsageSettings,
    ) -> None:
        """In-flight count > 0 with no pending is NOT idle — the
        supervisor stays in a dispatch-capable state so the next clean
        reading reclassifies normally."""
        snap = _initial()
        reading = _reading(
            five_pct=10,
            weekly_pct=5,
            five_resets=clock.now() + timedelta(hours=2),
            weekly_resets=clock.now() + timedelta(days=4),
        )
        new, _ = step(
            _input(
                snap,
                reading,
                policy,
                supervisor_settings,
                usage_settings,
                pending=0,
                in_flight=2,
            ),
            clock,
        )
        assert new.state is SupervisorState.DISPATCHING


# ----------------------------------------------------------------------------
# 5h-driven states: SLOWING_DOWN, THROTTLED_5H
# ----------------------------------------------------------------------------


class TestFiveHourThrottle:
    def test_slowdown_5h(
        self,
        policy: ResolvedPolicy,
        clock: FakeClock,
        supervisor_settings: SupervisorSettings,
        usage_settings: UsageSettings,
    ) -> None:
        """5h utilization in the slowdown band → SLOWING_DOWN.

        Day band default: slowdown=40, stop=60. observed=50 sits squarely
        in the ramp.
        """
        snap = _initial(SupervisorState.DISPATCHING)
        reading = _reading(
            five_pct=50,
            weekly_pct=5,
            five_resets=clock.now() + timedelta(hours=2),
            weekly_resets=clock.now() + timedelta(days=4),
        )
        new, actions = step(
            _input(
                snap,
                reading,
                policy,
                supervisor_settings,
                usage_settings,
                pending=2,
            ),
            clock,
        )
        assert new.state is SupervisorState.SLOWING_DOWN
        # SLOWING_DOWN entry emits a Notify on transition.
        assert any(isinstance(a, Notify) and a.level == "info" for a in actions)

    def test_throttled_5h(
        self,
        policy: ResolvedPolicy,
        clock: FakeClock,
        supervisor_settings: SupervisorSettings,
        usage_settings: UsageSettings,
    ) -> None:
        """5h utilization at or above stop_pct → THROTTLED_5H."""
        snap = _initial(SupervisorState.DISPATCHING)
        five_reset = datetime(2026, 5, 4, 13, 0, tzinfo=UTC)
        reading = _reading(
            five_pct=65,
            weekly_pct=5,
            five_resets=five_reset,
            weekly_resets=clock.now() + timedelta(days=4),
        )
        new, actions = step(
            _input(
                snap,
                reading,
                policy,
                supervisor_settings,
                usage_settings,
                pending=2,
            ),
            clock,
        )
        assert new.state is SupervisorState.THROTTLED_5H
        assert StopDispatch in _action_types(actions)
        # Wakeup is scheduled at the next 5h reset + window_start_delay_s
        # (default 300s).
        wakeups = [a for a in actions if isinstance(a, ScheduleWakeupAt)]
        assert len(wakeups) == 1
        expected = five_reset + timedelta(seconds=supervisor_settings.window_start_delay_s)
        assert wakeups[0].when == expected

    def test_throttled_5h_emits_event_with_band(
        self,
        policy: ResolvedPolicy,
        clock: FakeClock,
        supervisor_settings: SupervisorSettings,
        usage_settings: UsageSettings,
    ) -> None:
        """Entry to THROTTLED_5H emits ``throttled_5h_entry`` with the new
        payload keys: ``five_hour_util_pct``, ``fivehr_slowdown_pct``,
        ``fivehr_stop_pct``, ``band``."""
        snap = _initial(SupervisorState.DISPATCHING)
        reading = _reading(
            five_pct=70,
            weekly_pct=5,
            five_resets=clock.now() + timedelta(hours=2),
            weekly_resets=clock.now() + timedelta(days=4),
        )
        _, actions = step(
            _input(
                snap,
                reading,
                policy,
                supervisor_settings,
                usage_settings,
                pending=2,
            ),
            clock,
        )
        entry_events = [
            a for a in actions if isinstance(a, EmitEvent) and a.event_type == "throttled_5h_entry"
        ]
        assert len(entry_events) == 1
        payload = entry_events[0].payload
        assert payload["five_hour_util_pct"] == 70
        assert payload["fivehr_slowdown_pct"] == 40
        assert payload["fivehr_stop_pct"] == 60
        assert payload["band"] == "day"


# ----------------------------------------------------------------------------
# Weekly-driven state: THROTTLED_WEEKLY
# ----------------------------------------------------------------------------


class TestThrottledWeekly:
    def test_observed_above_target_throttles_weekly(
        self,
        policy: ResolvedPolicy,
        clock: FakeClock,
        supervisor_settings: SupervisorSettings,
        usage_settings: UsageSettings,
    ) -> None:
        """At elapsed=0.5 (mid-week), curve target with default
        ``early_pct=60`` and ``eow_time_switch=40h`` evaluates to roughly
        39% — observed=60 is above target → THROTTLED_WEEKLY.

        See ``test_decision.py::TestThrottledWeekly`` for the math
        verification; here we only verify the state-machine wraps it.
        """
        snap = _initial(SupervisorState.DISPATCHING)
        weekly_resets = clock.now() + timedelta(seconds=SEVEN_DAYS_S / 2)
        reading = _reading(
            five_pct=5,
            weekly_pct=60,
            five_resets=clock.now() + timedelta(hours=2),
            weekly_resets=weekly_resets,
        )
        new, actions = step(
            _input(
                snap,
                reading,
                policy,
                supervisor_settings,
                usage_settings,
                pending=2,
            ),
            clock,
        )
        assert new.state is SupervisorState.THROTTLED_WEEKLY
        # Transition to THROTTLED_WEEKLY emits a warn-level Notify and a
        # ``throttled_weekly_entry`` event with new payload keys.
        assert any(isinstance(a, Notify) and a.level == "warn" for a in actions)
        entries = [
            a
            for a in actions
            if isinstance(a, EmitEvent) and a.event_type == "throttled_weekly_entry"
        ]
        assert len(entries) == 1
        payload = entries[0].payload
        assert payload["observed_pct"] == 60
        assert payload["target_pct"] is not None
        assert payload["target_pct"] < 60.0
        # Old keys (``weekly_util_pct``, ``weekly_slow_band_pct``,
        # ``weekly_pause_at_pct``) MUST NOT appear.
        assert "weekly_util_pct" not in payload
        assert "weekly_slow_band_pct" not in payload
        assert "weekly_pause_at_pct" not in payload
        # Throttle states always emit StopDispatch.
        assert StopDispatch in _action_types(actions)
        # A wakeup is scheduled.
        assert any(isinstance(a, ScheduleWakeupAt) for a in actions)

    def test_weekly_unparseable_falls_back_to_5h(
        self,
        policy: ResolvedPolicy,
        clock: FakeClock,
        supervisor_settings: SupervisorSettings,
        usage_settings: UsageSettings,
    ) -> None:
        """If ``seven_day.resets_at`` is None the curve has nothing to
        anchor to; weekly is treated as 'allow dispatch' so the 5h
        side classifies on its own."""
        snap = _initial(SupervisorState.DISPATCHING)
        reading = _reading(
            five_pct=10,
            weekly_pct=95,  # would be over target if the curve was anchored
            five_resets=clock.now() + timedelta(hours=2),
            weekly_resets=None,
        )
        new, _ = step(
            _input(
                snap,
                reading,
                policy,
                supervisor_settings,
                usage_settings,
                pending=2,
            ),
            clock,
        )
        assert new.state is SupervisorState.DISPATCHING


# ----------------------------------------------------------------------------
# Drift handling
# ----------------------------------------------------------------------------


class TestErrorDrift:
    def test_drift_enters_error(
        self,
        policy: ResolvedPolicy,
        clock: FakeClock,
        supervisor_settings: SupervisorSettings,
        usage_settings: UsageSettings,
    ) -> None:
        snap = _initial(SupervisorState.DISPATCHING)
        drift = UsageFormatDrift("only 1 block found")
        new, actions = step(
            _input(
                snap,
                drift,
                policy,
                supervisor_settings,
                usage_settings,
                pending=2,
            ),
            clock,
        )
        assert new.state is SupervisorState.ERROR_DRIFT
        assert new.consecutive_clean_polls == 0
        assert "only 1 block" in new.last_drift_message
        assert any(isinstance(a, Notify) and a.level == "error" for a in actions)
        assert any(isinstance(a, EmitEvent) and a.event_type == "drift_detected" for a in actions)
        assert StopDispatch in _action_types(actions)

    def test_drift_recovery_requires_n_clean_polls(
        self,
        policy: ResolvedPolicy,
        clock: FakeClock,
        supervisor_settings: SupervisorSettings,
        usage_settings: UsageSettings,
    ) -> None:
        snap = _initial(SupervisorState.ERROR_DRIFT)
        snap = snap.model_copy(update={"consecutive_clean_polls": 0})
        good = _reading(
            five_pct=10,
            weekly_pct=5,
            five_resets=datetime(2026, 5, 4, 13, 0, tzinfo=UTC),
            weekly_resets=clock.now() + timedelta(days=4),
        )

        # First clean poll: still in ERROR_DRIFT, counter at 1.
        new1, actions1 = step(
            _input(
                snap,
                good,
                policy,
                supervisor_settings,
                usage_settings,
                pending=2,
            ),
            clock,
        )
        assert new1.state is SupervisorState.ERROR_DRIFT
        assert new1.consecutive_clean_polls == 1
        assert StopDispatch in _action_types(actions1)

        # Second clean poll: still in error, counter at 2.
        new2, _ = step(
            _input(
                new1,
                good,
                policy,
                supervisor_settings,
                usage_settings,
                pending=2,
            ),
            clock,
        )
        assert new2.state is SupervisorState.ERROR_DRIFT
        assert new2.consecutive_clean_polls == 2

        # Third clean poll: hits the default threshold (3) and recovers.
        new3, _ = step(
            _input(
                new2,
                good,
                policy,
                supervisor_settings,
                usage_settings,
                pending=2,
            ),
            clock,
        )
        assert new3.state is SupervisorState.DISPATCHING
        assert new3.consecutive_clean_polls == 0

    def test_drift_during_drift_resets_counter(
        self,
        policy: ResolvedPolicy,
        clock: FakeClock,
        supervisor_settings: SupervisorSettings,
        usage_settings: UsageSettings,
    ) -> None:
        snap = _initial(SupervisorState.ERROR_DRIFT)
        snap = snap.model_copy(update={"consecutive_clean_polls": 2})
        new, _ = step(
            _input(
                snap,
                UsageFormatDrift("another drift"),
                policy,
                supervisor_settings,
                usage_settings,
                pending=2,
            ),
            clock,
        )
        assert new.state is SupervisorState.ERROR_DRIFT
        assert new.consecutive_clean_polls == 0


# ----------------------------------------------------------------------------
# Capture-level errors are skipped
# ----------------------------------------------------------------------------


class TestCaptureErrors:
    def test_timeout_keeps_state(
        self,
        policy: ResolvedPolicy,
        clock: FakeClock,
        supervisor_settings: SupervisorSettings,
        usage_settings: UsageSettings,
    ) -> None:
        snap = _initial(SupervisorState.DISPATCHING)
        new, actions = step(
            _input(
                snap,
                UsageCaptureTimeout("slow"),
                policy,
                supervisor_settings,
                usage_settings,
                pending=2,
            ),
            clock,
        )
        assert new.state is SupervisorState.DISPATCHING
        assert any(
            isinstance(a, EmitEvent) and a.event_type == "usage_capture_error" for a in actions
        )

    def test_spawn_error_keeps_state(
        self,
        policy: ResolvedPolicy,
        clock: FakeClock,
        supervisor_settings: SupervisorSettings,
        usage_settings: UsageSettings,
    ) -> None:
        snap = _initial(SupervisorState.DISPATCHING)
        new, _ = step(
            _input(
                snap,
                UsageCaptureSpawnError("missing"),
                policy,
                supervisor_settings,
                usage_settings,
                pending=2,
            ),
            clock,
        )
        assert new.state is SupervisorState.DISPATCHING


# ----------------------------------------------------------------------------
# Auth-expired routing (PR 14)
# ----------------------------------------------------------------------------


class TestAuthExpired:
    """``UsageApiAuthExpired`` is a subclass of
    :class:`UsageCaptureSpawnError`, but the state machine routes it
    explicitly to ERROR_DRIFT so the operator notices."""

    def test_auth_expired_enters_error_drift(
        self,
        policy: ResolvedPolicy,
        clock: FakeClock,
        supervisor_settings: SupervisorSettings,
        usage_settings: UsageSettings,
    ) -> None:
        snap = _initial(SupervisorState.DISPATCHING)
        new, actions = step(
            _input(
                snap,
                UsageApiAuthExpired("HTTP 401"),
                policy,
                supervisor_settings,
                usage_settings,
                pending=2,
            ),
            clock,
        )
        assert new.state is SupervisorState.ERROR_DRIFT
        assert "HTTP 401" in new.last_drift_message
        action_types = {type(a) for a in actions}
        assert Notify in action_types
        assert StopDispatch in action_types
        assert any(
            isinstance(a, EmitEvent) and a.event_type == "oauth_auth_expired" for a in actions
        )

    def test_auth_expired_during_drift_updates_message(
        self,
        policy: ResolvedPolicy,
        clock: FakeClock,
        supervisor_settings: SupervisorSettings,
        usage_settings: UsageSettings,
    ) -> None:
        """A second auth-expired tick refreshes ``last_drift_message``
        but does NOT re-fire Notify (one alert per entry, not per tick).
        """
        snap = _initial(SupervisorState.ERROR_DRIFT)
        snap = snap.model_copy(
            update={
                "last_drift_message": "HTTP 401",
                "consecutive_clean_polls": 2,
            }
        )
        new, actions = step(
            _input(
                snap,
                UsageApiAuthExpired("HTTP 401 again"),
                policy,
                supervisor_settings,
                usage_settings,
                pending=2,
            ),
            clock,
        )
        assert new.state is SupervisorState.ERROR_DRIFT
        assert "HTTP 401 again" in new.last_drift_message
        assert new.consecutive_clean_polls == 0
        assert not any(isinstance(a, Notify) for a in actions)


# ----------------------------------------------------------------------------
# Stop / Resume
# ----------------------------------------------------------------------------


class TestStopAndResume:
    def test_stop_is_sticky(
        self,
        policy: ResolvedPolicy,
        clock: FakeClock,
        supervisor_settings: SupervisorSettings,
        usage_settings: UsageSettings,
    ) -> None:
        """STOPPED never moves under :func:`step` — only
        :func:`request_resume` returns to IDLE."""
        snap = request_stop(_initial(), clock=clock)
        assert snap.state is SupervisorState.STOPPED
        reading = _reading(five_pct=10, weekly_pct=5)
        new, actions = step(
            _input(
                snap,
                reading,
                policy,
                supervisor_settings,
                usage_settings,
                pending=10,
            ),
            clock,
        )
        assert new.state is SupervisorState.STOPPED
        assert MonitorInFlight in _action_types(actions)

    def test_resume_returns_to_idle(self, clock: FakeClock) -> None:
        snap = request_stop(_initial(), clock=clock)
        snap = request_resume(snap, clock=clock)
        assert snap.state is SupervisorState.IDLE

    def test_resume_no_op_when_not_stopped(self, clock: FakeClock) -> None:
        snap = _initial(SupervisorState.DISPATCHING)
        out = request_resume(snap, clock=clock)
        assert out is snap

    @pytest.mark.parametrize(
        "reading",
        [
            pytest.param(UsageFormatDrift("only 1 block found"), id="drift"),
            pytest.param(
                _reading(
                    five_pct=80,
                    weekly_pct=80,
                    five_resets=datetime(2026, 5, 4, 13, 0, tzinfo=UTC),
                    weekly_resets=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
                ),
                id="clean-80pct",
            ),
            pytest.param(UsageCaptureTimeout("slow"), id="capture-timeout"),
            pytest.param(UsageCaptureSpawnError("missing"), id="spawn-error"),
            pytest.param(UsageApiAuthExpired("HTTP 401"), id="auth-expired"),
        ],
    )
    def test_stopped_is_sticky_under_various_readings(
        self,
        reading,
        policy: ResolvedPolicy,
        clock: FakeClock,
        supervisor_settings: SupervisorSettings,
        usage_settings: UsageSettings,
    ) -> None:
        """STOPPED short-circuits ahead of every reading classifier
        (drift, clean-but-high utilization, capture errors, auth-expired)
        and returns exactly ``[MonitorInFlight()]`` with the snapshot
        unchanged. Only :func:`request_resume` leaves STOPPED."""
        snap = request_stop(_initial(SupervisorState.THROTTLED_5H), clock=clock)
        assert snap.state is SupervisorState.STOPPED

        new, actions = step(
            _input(
                snap,
                reading,
                policy,
                supervisor_settings,
                usage_settings,
                pending=10,
                in_flight=3,
            ),
            clock,
        )

        # Stays STOPPED — the snapshot is returned untouched (same
        # object: the STOPPED branch returns ``snapshot`` directly).
        assert new is snap
        assert new.state is SupervisorState.STOPPED
        # The exact emitted command set is a single MonitorInFlight — no
        # StopDispatch, no Notify, no EmitEvent leak through the sticky
        # short-circuit.
        assert actions == [MonitorInFlight()]


# ----------------------------------------------------------------------------
# State-transition events
# ----------------------------------------------------------------------------


class TestStateTransitionEvents:
    def test_emits_state_transition_event(
        self,
        policy: ResolvedPolicy,
        clock: FakeClock,
        supervisor_settings: SupervisorSettings,
        usage_settings: UsageSettings,
    ) -> None:
        """Crossing into a different state emits one
        ``state_transition`` event with ``from``, ``to``,
        ``five_hour_util``, ``weekly_util``."""
        snap = _initial(SupervisorState.IDLE)
        reading = _reading(
            five_pct=10,
            weekly_pct=5,
            five_resets=clock.now() + timedelta(hours=2),
            weekly_resets=clock.now() + timedelta(days=4),
        )
        _, actions = step(
            _input(
                snap,
                reading,
                policy,
                supervisor_settings,
                usage_settings,
                pending=2,
            ),
            clock,
        )
        transition_events = [
            a for a in actions if isinstance(a, EmitEvent) and a.event_type == "state_transition"
        ]
        assert len(transition_events) == 1
        payload = transition_events[0].payload
        assert payload["from"] == SupervisorState.IDLE.value
        assert payload["to"] == SupervisorState.DISPATCHING.value
        assert payload["five_hour_util"] == 10
        assert payload["weekly_util"] == 5

    def test_no_event_when_state_unchanged(
        self,
        policy: ResolvedPolicy,
        clock: FakeClock,
        supervisor_settings: SupervisorSettings,
        usage_settings: UsageSettings,
    ) -> None:
        """Stay-in-state ticks do not emit ``state_transition``."""
        snap = _initial(SupervisorState.DISPATCHING)
        reading = _reading(
            five_pct=10,
            weekly_pct=5,
            five_resets=clock.now() + timedelta(hours=2),
            weekly_resets=clock.now() + timedelta(days=4),
        )
        _, actions = step(
            _input(
                snap,
                reading,
                policy,
                supervisor_settings,
                usage_settings,
                pending=2,
            ),
            clock,
        )
        transitions = [
            a for a in actions if isinstance(a, EmitEvent) and a.event_type == "state_transition"
        ]
        assert transitions == []


# ----------------------------------------------------------------------------
# Wakeup scheduling
# ----------------------------------------------------------------------------


class TestWakeupScheduling:
    def test_throttled_5h_schedules_wakeup_at_next_reset(
        self,
        policy: ResolvedPolicy,
        clock: FakeClock,
        supervisor_settings: SupervisorSettings,
        usage_settings: UsageSettings,
    ) -> None:
        snap = _initial(SupervisorState.DISPATCHING)
        five_reset = datetime(2026, 5, 4, 13, 0, tzinfo=UTC)
        reading = _reading(
            five_pct=80,
            weekly_pct=5,
            five_resets=five_reset,
            weekly_resets=clock.now() + timedelta(days=4),
        )
        new, actions = step(
            _input(
                snap,
                reading,
                policy,
                supervisor_settings,
                usage_settings,
                pending=2,
            ),
            clock,
        )
        wakeups = [a for a in actions if isinstance(a, ScheduleWakeupAt)]
        assert len(wakeups) == 1
        expected = five_reset + timedelta(seconds=supervisor_settings.window_start_delay_s)
        assert wakeups[0].when == expected
        assert new.scheduled_wakeup_at == expected

    def test_dispatching_clears_wakeup(
        self,
        policy: ResolvedPolicy,
        clock: FakeClock,
        supervisor_settings: SupervisorSettings,
        usage_settings: UsageSettings,
    ) -> None:
        """When the decision returns DISPATCHING the wakeup is None;
        the snapshot's ``scheduled_wakeup_at`` is cleared."""
        snap = _initial(SupervisorState.DISPATCHING).model_copy(
            update={"scheduled_wakeup_at": datetime(2026, 5, 4, 13, 0, tzinfo=UTC)}
        )
        reading = _reading(
            five_pct=10,
            weekly_pct=5,
            five_resets=clock.now() + timedelta(hours=2),
            weekly_resets=clock.now() + timedelta(days=4),
        )
        new, actions = step(
            _input(
                snap,
                reading,
                policy,
                supervisor_settings,
                usage_settings,
                pending=2,
            ),
            clock,
        )
        assert new.state is SupervisorState.DISPATCHING
        assert new.scheduled_wakeup_at is None
        assert not any(isinstance(a, ScheduleWakeupAt) for a in actions)


# ----------------------------------------------------------------------------
# Exhaustive state enumeration
# ----------------------------------------------------------------------------


class TestAllStates:
    def test_all_states_returns_seven_surviving_states(self) -> None:
        """ADR-0022 drops ``PAUSED_WEEKLY`` and ``END_OF_WEEK_PUSH``;
        seven surviving states remain."""
        states = list(all_states())
        expected = {
            SupervisorState.IDLE,
            SupervisorState.DISPATCHING,
            SupervisorState.SLOWING_DOWN,
            SupervisorState.THROTTLED_5H,
            SupervisorState.THROTTLED_WEEKLY,
            SupervisorState.STOPPED,
            SupervisorState.ERROR_DRIFT,
        }
        assert set(states) == expected
        assert len(states) == 7

    def test_dropped_states_no_longer_in_enum(self) -> None:
        """``paused_weekly`` and ``end_of_week_push`` are gone."""
        values = {s.value for s in SupervisorState}
        assert "paused_weekly" not in values
        assert "end_of_week_push" not in values


# ----------------------------------------------------------------------------
# IDLE classification from every state (no pending, nothing in flight)
# ----------------------------------------------------------------------------


class TestIdleFromEveryState:
    """When ``pending==0 and in_flight==0`` and the reading is clean, the
    step function classifies to IDLE — from every prior state, with two
    documented exceptions baked into the machine:

    * STOPPED is sticky (only ``request_resume`` leaves it).
    * ERROR_DRIFT must first clear ``drift_recovery_clean_polls`` clean
      polls in a row before any further classification (IDLE included)
      can fire; this test primes the counter so the threshold is met on
      the tick under test, exercising the fall-through into the IDLE
      branch.

    Enumerates the full :class:`SupervisorState` enum so a newly-added
    state can't silently skip the IDLE classification.
    """

    @pytest.mark.parametrize("prior", list(SupervisorState))
    def test_no_work_classifies_idle(
        self,
        prior: SupervisorState,
        policy: ResolvedPolicy,
        clock: FakeClock,
        supervisor_settings: SupervisorSettings,
        usage_settings: UsageSettings,
    ) -> None:
        snap = _initial(prior)
        if prior is SupervisorState.ERROR_DRIFT:
            # Prime so this tick is the Nth clean poll → recovery
            # threshold met → fall through to the IDLE branch.
            snap = snap.model_copy(
                update={
                    "consecutive_clean_polls": usage_settings.drift_recovery_clean_polls - 1
                }
            )

        reading = _reading(
            five_pct=10,
            weekly_pct=5,
            five_resets=clock.now() + timedelta(hours=2),
            weekly_resets=clock.now() + timedelta(days=4),
        )
        new, actions = step(
            _input(
                snap,
                reading,
                policy,
                supervisor_settings,
                usage_settings,
                pending=0,
                in_flight=0,
            ),
            clock,
        )

        if prior is SupervisorState.STOPPED:
            # Sticky: never reaches the IDLE check.
            assert new.state is SupervisorState.STOPPED
            assert actions == [MonitorInFlight()]
        else:
            assert new.state is SupervisorState.IDLE, (
                f"prior={prior.value} should classify to IDLE with no work"
            )
            assert MonitorInFlight in _action_types(actions)
            # IDLE entry zeroes the drift bookkeeping.
            assert new.consecutive_clean_polls == 0
            assert new.last_drift_message == ""
