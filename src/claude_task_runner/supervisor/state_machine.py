"""Pure state-machine step function for the supervisor.

The daemon calls :func:`step` once per poll tick:

    new_snapshot, actions = step(snapshot, reading_or_error, settings, clock,
                                 pending_count=N, in_flight_count=M)

Properties (also enforced by tests in
``tests/property/test_state_machine_hypothesis.py``):

* No I/O, no global state — the step function is a pure function of
  its inputs. Pass a :class:`FakeClock` and ``FakeUsageSource`` and
  the entire dynamics are reproducible.
* Recovery from :class:`states.SupervisorState.ERROR_DRIFT` requires
  ``[usage].drift_recovery_clean_polls`` consecutive clean readings.
  This is the anti-flap guarantee the Plan agent flagged.
* :class:`states.SupervisorState.STOPPED` is sticky — only an explicit
  resume action moves out of it.
* In-flight tasks are NEVER killed by state transitions; the throttle
  bands only gate NEW dispatches.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta

from claude_task_runner.clock import Clock
from claude_task_runner.config.schema import (
    SupervisorSettings,
    ThrottleSettings,
    UsageSettings,
)
from claude_task_runner.supervisor import window as window_mod
from claude_task_runner.supervisor.actions import (
    Action,
    EmitEvent,
    MonitorInFlight,
    Notify,
    ScheduleWakeupAt,
    StopDispatch,
)
from claude_task_runner.supervisor.states import (
    SupervisorSnapshot,
    SupervisorState,
)
from claude_task_runner.usage.drift import (
    UsageCaptureSpawnError,
    UsageCaptureTimeout,
    UsageFormatDrift,
)
from claude_task_runner.usage.models import UsageReading


@dataclass(frozen=True)
class StepInput:
    """Bundle of inputs to :func:`step`.

    Using a dataclass keeps signatures stable as we add more inputs;
    callers construct it once per tick.
    """

    snapshot: SupervisorSnapshot
    reading: UsageReading | UsageFormatDrift | UsageCaptureTimeout | UsageCaptureSpawnError
    settings_throttle: ThrottleSettings
    settings_supervisor: SupervisorSettings
    settings_usage: UsageSettings
    pending_count: int
    in_flight_count: int


def _entry(
    state: SupervisorState,
    *,
    snapshot: SupervisorSnapshot,
    clock: Clock,
    reading: UsageReading | None,
    consecutive_clean_polls: int | None = None,
    last_drift_message: str | None = None,
    scheduled_wakeup_at: object = ...,  # sentinel: unset means keep
) -> SupervisorSnapshot:
    """Build a new snapshot with ``state`` and ``since=clock.now()``.

    Updates utilization / reset fields from ``reading`` when provided;
    otherwise keeps the previous values. ``scheduled_wakeup_at`` is a
    sentinel-based optional override — pass ``None`` to clear, pass
    ``...`` (default) to keep the previous value.
    """
    update: dict[str, object] = {"state": state, "since": clock.now()}
    if reading is not None:
        update["last_5h_util_pct"] = reading.five_hour.utilization_pct
        update["last_weekly_util_pct"] = reading.seven_day.utilization_pct
        if reading.five_hour.resets_at is not None:
            update["last_5h_reset_at"] = reading.five_hour.resets_at
        if reading.seven_day.resets_at is not None:
            update["last_weekly_reset_at"] = reading.seven_day.resets_at
    if consecutive_clean_polls is not None:
        update["consecutive_clean_polls"] = consecutive_clean_polls
    if last_drift_message is not None:
        update["last_drift_message"] = last_drift_message
    if scheduled_wakeup_at is not ...:
        update["scheduled_wakeup_at"] = scheduled_wakeup_at
    return snapshot.model_copy(update=update)


def _classify_active(
    *,
    reading: UsageReading,
    throttle: ThrottleSettings,
) -> SupervisorState:
    """Pick between DISPATCHING / SLOWING_DOWN / THROTTLED_5H / PAUSED_WEEKLY
    given a clean reading.

    PAUSED_WEEKLY beats THROTTLED_5H beats SLOWING_DOWN beats DISPATCHING
    because weekly-cap is the strictest brake.
    """
    weekly_pct = reading.seven_day.utilization_pct
    if weekly_pct >= throttle.weekly.pause_at_pct:
        return SupervisorState.PAUSED_WEEKLY

    five_pct = reading.five_hour.utilization_pct
    if five_pct >= throttle.five_hour.band_slowdown_max_pct:
        return SupervisorState.THROTTLED_5H

    five_in_slow = (
        five_pct >= throttle.five_hour.band_full_dispatch_max_pct
        and five_pct < throttle.five_hour.band_slowdown_max_pct
    )
    weekly_in_slow = (
        weekly_pct >= throttle.weekly.band_full_dispatch_max_pct
        and weekly_pct < throttle.weekly.band_slowdown_max_pct
    )
    weekly_in_stop = weekly_pct >= throttle.weekly.band_slowdown_max_pct

    if weekly_in_stop:
        # Weekly stopped band but not yet pause_at — treat as throttled.
        return SupervisorState.THROTTLED_5H
    if five_in_slow or weekly_in_slow:
        return SupervisorState.SLOWING_DOWN
    return SupervisorState.DISPATCHING


def _wakeup_for_throttle(
    *,
    reading: UsageReading,
    clock: Clock,
    settings_supervisor: SupervisorSettings,
) -> object:
    """Compute a scheduled wakeup just past the next 5-hour reset.

    Returns a datetime when the 5-hour reset time is parseable, else
    keeps the previous schedule by returning the sentinel (caller must
    handle).
    """
    if reading.five_hour.resets_at is None:
        return ...  # keep previous
    return window_mod.schedule_window_start_wakeup(
        window=reading.five_hour,
        clock=clock,
        delay_s=settings_supervisor.window_start_delay_s,
        fallback_window_length_s=window_mod.FIVE_HOUR_LENGTH_S,
    )


def _wakeup_for_weekly_pause(
    *,
    reading: UsageReading,
    clock: Clock,
    settings_supervisor: SupervisorSettings,
    settings_throttle: ThrottleSettings,
) -> object:
    """When weekly is paused, schedule wakeup either at:

    * The EOW window opening (so we re-enter to push toward 98%), or
    * The weekly reset itself (if EOW is disabled / config edge case).
    """
    if reading.seven_day.resets_at is None:
        return ...
    eow_window = settings_throttle.weekly.eow_window_s
    if eow_window > 0:
        # Wake when EOW window opens (resets_at - eow_window_s).
        return reading.seven_day.resets_at - timedelta(seconds=eow_window)
    return reading.seven_day.resets_at + timedelta(seconds=settings_supervisor.window_start_delay_s)


def step(
    inp: StepInput,
    clock: Clock,
) -> tuple[SupervisorSnapshot, list[Action]]:
    """Compute the next supervisor snapshot and any actions to perform.

    ``inp.reading`` may be a successful :class:`UsageReading` or one of
    the usage exceptions; the latter routes us into ERROR_DRIFT (parser
    drift) or simply skipped polls (capture timeout / spawn error).
    """
    snapshot = inp.snapshot
    actions: list[Action] = []

    # STOPPED is sticky — only an explicit operator command (handled
    # outside this pure step) clears it.
    if snapshot.state is SupervisorState.STOPPED:
        return snapshot, [MonitorInFlight()]

    # Capture-level errors don't trigger ERROR_DRIFT (which is parser
    # format drift). They simply skip this tick — utilization unchanged,
    # state unchanged, monitor in-flight only.
    if isinstance(inp.reading, (UsageCaptureSpawnError, UsageCaptureTimeout)):
        actions.append(
            EmitEvent(
                event_type="usage_capture_error",
                payload={"error": str(inp.reading)},
            )
        )
        actions.append(MonitorInFlight())
        return snapshot, actions

    # Parser drift → enter ERROR_DRIFT.
    if isinstance(inp.reading, UsageFormatDrift):
        if snapshot.state is SupervisorState.ERROR_DRIFT:
            # Already in drift; reset the clean-poll counter and
            # update the message (the underlying drift may have changed).
            new_snap = snapshot.model_copy(
                update={
                    "consecutive_clean_polls": 0,
                    "last_drift_message": str(inp.reading),
                }
            )
        else:
            new_snap = _entry(
                SupervisorState.ERROR_DRIFT,
                snapshot=snapshot,
                clock=clock,
                reading=None,
                consecutive_clean_polls=0,
                last_drift_message=str(inp.reading),
            )
            actions.append(
                Notify(
                    level="error",
                    message=f"parser drift: {inp.reading}",
                )
            )
        actions.append(StopDispatch())
        actions.append(MonitorInFlight())
        actions.append(
            EmitEvent(
                event_type="drift_detected",
                payload={"message": str(inp.reading)},
            )
        )
        return new_snap, actions

    # From here on, ``reading`` is a clean :class:`UsageReading`.
    reading: UsageReading = inp.reading

    # Recovery from ERROR_DRIFT: require N clean polls in a row.
    if snapshot.state is SupervisorState.ERROR_DRIFT:
        clean = snapshot.consecutive_clean_polls + 1
        needed = inp.settings_usage.drift_recovery_clean_polls
        if clean < needed:
            new_snap = snapshot.model_copy(
                update={
                    "consecutive_clean_polls": clean,
                    "last_5h_util_pct": reading.five_hour.utilization_pct,
                    "last_weekly_util_pct": reading.seven_day.utilization_pct,
                    "last_5h_reset_at": (reading.five_hour.resets_at or snapshot.last_5h_reset_at),
                    "last_weekly_reset_at": (
                        reading.seven_day.resets_at or snapshot.last_weekly_reset_at
                    ),
                }
            )
            actions.append(StopDispatch())
            actions.append(MonitorInFlight())
            actions.append(
                EmitEvent(
                    event_type="drift_clean_poll",
                    payload={"clean": clean, "needed": needed},
                )
            )
            return new_snap, actions
        # Clean threshold met — fall through to normal classification,
        # clearing the drift bookkeeping.

    # Idle when no work pending and nothing in flight.
    if inp.pending_count == 0 and inp.in_flight_count == 0:
        new_snap = _entry(
            SupervisorState.IDLE,
            snapshot=snapshot,
            clock=clock,
            reading=reading,
            consecutive_clean_polls=0,
            last_drift_message="",
        )
        actions.append(MonitorInFlight())
        return new_snap, actions

    # Active classification: throttle bands.
    target_state = _classify_active(reading=reading, throttle=inp.settings_throttle)

    # End-of-week push: only entered FROM PausedWeekly when the EOW
    # window has opened. (PausedWeekly persists otherwise.)
    if (
        target_state is SupervisorState.PAUSED_WEEKLY
        and window_mod.in_eow_push_window(
            weekly=reading.seven_day,
            clock=clock,
            eow_window_s=inp.settings_throttle.weekly.eow_window_s,
        )
        and reading.seven_day.utilization_pct < inp.settings_throttle.weekly.eow_target_pct
    ):
        target_state = SupervisorState.END_OF_WEEK_PUSH

    # Schedule wakeups proactively for blocked / throttled states so the
    # supervisor (or its watchdog) can sleep until the next reset.
    wakeup: object = ...
    if target_state in (SupervisorState.THROTTLED_5H, SupervisorState.SLOWING_DOWN):
        wakeup = _wakeup_for_throttle(
            reading=reading,
            clock=clock,
            settings_supervisor=inp.settings_supervisor,
        )
    elif target_state is SupervisorState.PAUSED_WEEKLY:
        wakeup = _wakeup_for_weekly_pause(
            reading=reading,
            clock=clock,
            settings_supervisor=inp.settings_supervisor,
            settings_throttle=inp.settings_throttle,
        )
    elif target_state in (
        SupervisorState.DISPATCHING,
        SupervisorState.END_OF_WEEK_PUSH,
    ):
        # Active states: no scheduled wakeup needed (regular polling).
        wakeup = None

    new_snap = _entry(
        target_state,
        snapshot=snapshot,
        clock=clock,
        reading=reading,
        consecutive_clean_polls=0,
        last_drift_message="",
        scheduled_wakeup_at=wakeup,
    )

    # Side actions for state-specific transitions.
    if (
        target_state is SupervisorState.PAUSED_WEEKLY
        and snapshot.state is not SupervisorState.PAUSED_WEEKLY
    ):
        actions.append(
            Notify(
                level="warn",
                message=(
                    f"weekly utilization {reading.seven_day.utilization_pct}% — pausing dispatch"
                ),
            )
        )

    if (
        target_state is SupervisorState.THROTTLED_5H
        and snapshot.state is not SupervisorState.THROTTLED_5H
    ):
        actions.append(
            EmitEvent(
                event_type="throttled_5h_entry",
                payload={"util_pct": reading.five_hour.utilization_pct},
            )
        )

    if isinstance(wakeup, object) and wakeup not in (..., None):
        actions.append(ScheduleWakeupAt(when=wakeup))  # type: ignore[arg-type]

    if target_state in (
        SupervisorState.THROTTLED_5H,
        SupervisorState.PAUSED_WEEKLY,
    ):
        actions.append(StopDispatch())

    actions.append(MonitorInFlight())

    if snapshot.state is not target_state:
        actions.append(
            EmitEvent(
                event_type="state_transition",
                payload={
                    "from": snapshot.state.value,
                    "to": target_state.value,
                    "five_hour_util": reading.five_hour.utilization_pct,
                    "weekly_util": reading.seven_day.utilization_pct,
                },
            )
        )

    return new_snap, actions


def request_stop(snapshot: SupervisorSnapshot, *, clock: Clock) -> SupervisorSnapshot:
    """Operator-issued stop: transition to STOPPED.

    Sticky — only :func:`request_resume` moves out.
    """
    return _entry(
        SupervisorState.STOPPED,
        snapshot=snapshot,
        clock=clock,
        reading=None,
        scheduled_wakeup_at=None,
    )


def request_resume(snapshot: SupervisorSnapshot, *, clock: Clock) -> SupervisorSnapshot:
    """Operator-issued resume: leave STOPPED, return to IDLE.

    The next :func:`step` call reclassifies based on the next reading.
    """
    if snapshot.state is not SupervisorState.STOPPED:
        return snapshot
    return _entry(
        SupervisorState.IDLE,
        snapshot=snapshot,
        clock=clock,
        reading=None,
        consecutive_clean_polls=0,
        last_drift_message="",
    )


def all_states() -> Iterable[SupervisorState]:
    """Convenience iterator over every defined state — used in tests
    and dashboards to verify exhaustive handling."""
    return list(SupervisorState)
