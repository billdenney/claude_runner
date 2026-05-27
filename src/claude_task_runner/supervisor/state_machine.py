"""Pure state-machine step function for the supervisor.

The daemon calls :func:`step` once per poll tick:

    new_snapshot, actions = step(StepInput(...), clock)

Properties (also enforced by property tests):

* No I/O, no global state — pure function of its inputs.
* Recovery from :class:`states.SupervisorState.ERROR_DRIFT` requires
  ``[usage].drift_recovery_clean_polls`` consecutive clean readings.
* :class:`states.SupervisorState.STOPPED` is sticky — only
  :func:`request_resume` moves out of it.
* In-flight tasks are NEVER killed by state transitions; the dispatch
  decision only gates NEW dispatches.

The dispatch math lives in :mod:`claude_task_runner.throttle`. This
module is a thin translator from the throttle package's
:class:`Decision` into a ``(snapshot, actions)`` tuple plus the
non-decision concerns (STOPPED stickiness, IDLE classification,
ERROR_DRIFT routing).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from claude_task_runner.clock import Clock
from claude_task_runner.config.schema import (
    SupervisorSettings,
    UsageSettings,
)
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
from claude_task_runner.throttle.decision import Decision, decide
from claude_task_runner.throttle.policy import ResolvedPolicy
from claude_task_runner.usage.drift import (
    UsageApiAuthExpired,
    UsageCaptureSpawnError,
    UsageCaptureTimeout,
    UsageFormatDrift,
)
from claude_task_runner.usage.models import UsageReading


@dataclass(frozen=True)
class StepInput:
    """Bundle of inputs to :func:`step`.

    The dispatch policy is pre-resolved by the daemon
    (``throttle.policy.resolve(queue_settings, account_policy,
    account_name)``); ``step`` consumes the merged
    :class:`ResolvedPolicy` directly.
    """

    snapshot: SupervisorSnapshot
    reading: UsageReading | UsageFormatDrift | UsageCaptureTimeout | UsageCaptureSpawnError
    policy: ResolvedPolicy
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
    """Build a new snapshot with ``state`` and ``since=clock.now()``."""
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


def _emit_state_specific_events(
    decision: Decision,
    *,
    previous_state: SupervisorState,
    actions: list[Action],
) -> None:
    """Append per-transition Notify / EmitEvent actions for the new state."""
    new_state = decision.state

    if new_state is SupervisorState.THROTTLED_WEEKLY and previous_state is not new_state:
        actions.append(Notify(level="warn", message=decision.message))
        actions.append(
            EmitEvent(
                event_type="throttled_weekly_entry",
                payload={
                    "observed_pct": decision.observed_weekly_pct,
                    "target_pct": decision.target_pct,
                },
            )
        )

    if new_state is SupervisorState.THROTTLED_5H and previous_state is not new_state:
        actions.append(Notify(level="info", message=decision.message))
        actions.append(
            EmitEvent(
                event_type="throttled_5h_entry",
                payload={
                    "five_hour_util_pct": decision.observed_5h_pct,
                    "fivehr_slowdown_pct": decision.fivehr_slowdown_pct,
                    "fivehr_stop_pct": decision.fivehr_stop_pct,
                    "band": decision.band,
                },
            )
        )

    if new_state is SupervisorState.SLOWING_DOWN and previous_state is not new_state:
        actions.append(Notify(level="info", message=decision.message))


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

    # STOPPED is sticky — only :func:`request_resume` clears it.
    if snapshot.state is SupervisorState.STOPPED:
        return snapshot, [MonitorInFlight()]

    # PR 14: auth-expired routes to ERROR_DRIFT.
    #
    # This isinstance check must come BEFORE the UsageCaptureSpawnError
    # branch — UsageApiAuthExpired is a subclass of
    # UsageCaptureSpawnError, and the broader branch would otherwise
    # swallow it as a transient spawn error and skip the tick.
    if isinstance(inp.reading, UsageApiAuthExpired):
        if snapshot.state is SupervisorState.ERROR_DRIFT:
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
                    message=f"OAuth bearer rejected: {inp.reading}",
                )
            )
        actions.append(StopDispatch())
        actions.append(MonitorInFlight())
        actions.append(
            EmitEvent(
                event_type="oauth_auth_expired",
                payload={"message": str(inp.reading)},
            )
        )
        return new_snap, actions

    # Capture-level errors: skip this tick — utilization unchanged,
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

    # From here on, reading is a clean UsageReading.
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
        # Clean threshold met — fall through to normal classification.

    # IDLE when no work pending and nothing in flight.
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

    # Trace-following dispatch decision (ADR-0022).
    decision = decide(
        inp.policy,
        reading,
        clock,
        poll_interval_s=inp.settings_usage.poll_interval_s,
        window_start_delay_s=inp.settings_supervisor.window_start_delay_s,
    )

    new_snap = _entry(
        decision.state,
        snapshot=snapshot,
        clock=clock,
        reading=reading,
        consecutive_clean_polls=0,
        last_drift_message="",
        scheduled_wakeup_at=decision.wakeup_at,
    )

    _emit_state_specific_events(
        decision,
        previous_state=snapshot.state,
        actions=actions,
    )

    if decision.wakeup_at is not None:
        actions.append(ScheduleWakeupAt(when=decision.wakeup_at))

    if decision.state in (SupervisorState.THROTTLED_5H, SupervisorState.THROTTLED_WEEKLY):
        actions.append(StopDispatch())

    actions.append(MonitorInFlight())

    if snapshot.state is not decision.state:
        actions.append(
            EmitEvent(
                event_type="state_transition",
                payload={
                    "from": snapshot.state.value,
                    "to": decision.state.value,
                    "five_hour_util": reading.five_hour.utilization_pct,
                    "weekly_util": reading.seven_day.utilization_pct,
                },
            )
        )

    return new_snap, actions


def request_stop(snapshot: SupervisorSnapshot, *, clock: Clock) -> SupervisorSnapshot:
    """Operator-issued stop: transition to STOPPED. Sticky."""
    return _entry(
        SupervisorState.STOPPED,
        snapshot=snapshot,
        clock=clock,
        reading=None,
        scheduled_wakeup_at=None,
    )


def request_resume(snapshot: SupervisorSnapshot, *, clock: Clock) -> SupervisorSnapshot:
    """Operator-issued resume: leave STOPPED, return to IDLE."""
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
