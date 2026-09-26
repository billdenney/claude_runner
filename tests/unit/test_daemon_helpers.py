"""Tests for the pure helpers in supervisor/daemon.py.

``start_daemon`` itself is an integration concern (signals, global
lock, finite-tick mode) covered separately. Here we focus on the
side-effect-free pieces every operator-visible behaviour rides on:
``safe_poll``, ``run_one_tick``, ``execute_actions``, ``next_wakeup``,
``sleep_for_next_poll`` and ``sleep_until_woken``.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.schema import (
    Settings,
)
from claude_task_runner.supervisor.actions import (
    EmitEvent,
    MonitorInFlight,
    Notify,
    ScheduleWakeupAt,
    StopDispatch,
)
from claude_task_runner.supervisor.daemon import (
    SIGNAL_CHECK_INTERVAL_S,
    TickContext,
    execute_actions,
    next_wakeup,
    run_one_tick,
    safe_poll,
    sleep_for_next_poll,
    sleep_until_woken,
)
from claude_task_runner.supervisor.states import SupervisorSnapshot, SupervisorState
from claude_task_runner.usage.drift import (
    UsageCaptureSpawnError,
    UsageCaptureTimeout,
    UsageFormatDrift,
)
from claude_task_runner.usage.models import UsageReading, WindowReading
from claude_task_runner.usage.source import FakeUsageSource

# ---------------------------------------------------------------------------
# safe_poll: each documented exception type must be returned, not raised.
# ---------------------------------------------------------------------------


def _make_reading() -> UsageReading:
    return UsageReading(
        captured_at=datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC),
        five_hour=WindowReading(
            utilization_pct=20,
            resets_at_raw="some time",
            resets_at=datetime(2026, 5, 16, 17, 0, 0, tzinfo=UTC),
        ),
        seven_day=WindowReading(
            # Low weekly so the trace-following rule (ADR-0022) leaves
            # the supervisor in an active state — the throttle decision
            # is driven by observed-vs-target on the weekly curve, not
            # by a fixed slowdown threshold like the old [throttle.weekly]
            # block.
            utilization_pct=10,
            resets_at_raw="some time",
            resets_at=datetime(2026, 5, 20, 11, 0, 0, tzinfo=UTC),
        ),
    )


def test_safe_poll_happy_path_returns_reading() -> None:
    reading = _make_reading()
    src = FakeUsageSource([reading])
    result = safe_poll(src)
    assert result is reading


def test_safe_poll_format_drift_returned_not_raised() -> None:
    bad_drift = UsageFormatDrift("parser missed it")
    src = MagicMock(spec=FakeUsageSource)
    src.read.side_effect = bad_drift
    result = safe_poll(src)
    assert result is bad_drift


def test_safe_poll_capture_timeout_returned_not_raised() -> None:
    timeout = UsageCaptureTimeout("did not become ready")
    src = MagicMock(spec=FakeUsageSource)
    src.read.side_effect = timeout
    result = safe_poll(src)
    assert result is timeout


def test_safe_poll_spawn_error_returned_not_raised() -> None:
    spawn = UsageCaptureSpawnError("claude not found")
    src = MagicMock(spec=FakeUsageSource)
    src.read.side_effect = spawn
    result = safe_poll(src)
    assert result is spawn


# ---------------------------------------------------------------------------
# execute_actions: ensures each action type routes correctly.
# ---------------------------------------------------------------------------


def test_execute_actions_notify_via_callback() -> None:
    received: list[tuple[str, str]] = []
    actions = [Notify(level="warn", message="weekly at 92%")]
    execute_actions(actions, notify_callback=lambda lvl, msg: received.append((lvl, msg)))
    assert received == [("warn", "weekly at 92%")]


def test_execute_actions_notify_without_callback_logs() -> None:
    """No callback → log at info level. Must NOT raise."""
    execute_actions([Notify(level="info", message="just fyi")])


def test_execute_actions_emit_event_via_callback() -> None:
    received: list[tuple[str, dict[str, object]]] = []
    actions = [EmitEvent(event_type="drift_detected", payload={"clean": 0, "needed": 3})]
    execute_actions(actions, event_callback=lambda et, p: received.append((et, p)))
    assert received == [("drift_detected", {"clean": 0, "needed": 3})]


def test_execute_actions_emit_event_without_callback_logs() -> None:
    execute_actions([EmitEvent(event_type="x", payload={})])


def test_execute_actions_advisory_actions_are_no_ops() -> None:
    """MonitorInFlight, StopDispatch, ScheduleWakeupAt are observed by
    the caller (next_wakeup parses one of them) but execute_actions
    itself does nothing with them — verify no exceptions."""
    execute_actions(
        [
            MonitorInFlight(),
            StopDispatch(),
            ScheduleWakeupAt(when=datetime(2026, 5, 17, 0, 0, 0, tzinfo=UTC)),
        ]
    )


# ---------------------------------------------------------------------------
# next_wakeup: returns the latest ScheduleWakeupAt, or None.
# ---------------------------------------------------------------------------


def test_next_wakeup_none_when_no_schedule() -> None:
    assert next_wakeup([Notify(level="info", message="x"), MonitorInFlight()]) is None


def test_next_wakeup_picks_single_scheduled() -> None:
    when = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
    assert next_wakeup([ScheduleWakeupAt(when=when)]) == when


def test_next_wakeup_picks_latest_of_multiple() -> None:
    early = datetime(2026, 5, 17, 8, 0, 0, tzinfo=UTC)
    late = datetime(2026, 5, 17, 22, 0, 0, tzinfo=UTC)
    actions = [
        ScheduleWakeupAt(when=early),
        Notify(level="info", message="..."),
        ScheduleWakeupAt(when=late),
    ]
    assert next_wakeup(actions) == late


# ---------------------------------------------------------------------------
# sleep_for_next_poll: clamps to the wakeup_at if it's sooner.
# ---------------------------------------------------------------------------


def test_sleep_for_next_poll_uses_poll_interval_when_no_wakeup() -> None:
    sleeps: list[float] = []
    sleep_for_next_poll(
        wakeup_at=None,
        poll_interval_s=60.0,
        clock=FakeClock(datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)),
        sleep_fn=lambda s: sleeps.append(s),
    )
    assert sleeps == [60.0]


def test_sleep_for_next_poll_clamps_to_wakeup_if_sooner() -> None:
    sleeps: list[float] = []
    now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
    wakeup = now + timedelta(seconds=10)
    sleep_for_next_poll(
        wakeup_at=wakeup,
        poll_interval_s=60.0,
        clock=FakeClock(now),
        sleep_fn=lambda s: sleeps.append(s),
    )
    # Should sleep ~10s (the wakeup is sooner than the 60s poll).
    assert sleeps == [10.0]


def test_sleep_for_next_poll_does_not_sleep_if_wakeup_in_past(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A past wakeup means the next poll is already due: poll at once, and
    warn, since the tick ran past the wakeup its own decision scheduled.
    Until 2026-09-26 this slept the whole poll interval, and this test,
    despite its name, asserted that."""
    sleeps: list[float] = []
    now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
    wakeup = now - timedelta(seconds=30)
    with caplog.at_level("WARNING", logger="claude_task_runner.supervisor.daemon"):
        sleep_for_next_poll(
            wakeup_at=wakeup,
            poll_interval_s=60.0,
            clock=FakeClock(now),
            sleep_fn=lambda s: sleeps.append(s),
        )
    assert sleeps == []
    assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
        (
            "WARNING",
            "supervisor is not keeping up with its poll interval: this tick ended "
            "30.0 s after the wakeup it scheduled for 2026-05-16T11:59:30+00:00; "
            "polling again at once",
        )
    ]


def test_sleep_for_next_poll_does_not_sleep_or_warn_for_a_wakeup_due_now(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exactly on time is not falling behind."""
    sleeps: list[float] = []
    now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
    with caplog.at_level("WARNING", logger="claude_task_runner.supervisor.daemon"):
        sleep_for_next_poll(
            wakeup_at=now,
            poll_interval_s=60.0,
            clock=FakeClock(now),
            sleep_fn=lambda s: sleeps.append(s),
        )
    assert sleeps == []
    assert caplog.records == []


def test_sleep_for_next_poll_caps_a_later_wakeup_at_the_poll_interval() -> None:
    sleeps: list[float] = []
    now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
    sleep_for_next_poll(
        wakeup_at=now + timedelta(seconds=90),
        poll_interval_s=60.0,
        clock=FakeClock(now),
        sleep_fn=lambda s: sleeps.append(s),
    )
    assert sleeps == [60.0]


def test_sleep_for_next_poll_does_not_sleep_when_delay_zero_or_negative() -> None:
    """poll_interval_s=0 (and no wakeup) → no sleep_fn call."""
    sleeps: list[float] = []
    sleep_for_next_poll(
        wakeup_at=None,
        poll_interval_s=0.0,
        clock=FakeClock(datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)),
        sleep_fn=lambda s: sleeps.append(s),
    )
    assert sleeps == []


# ---------------------------------------------------------------------------
# sleep_until_woken: sleeps in slices so that a signal handler's flag ends
# the sleep. The daemon's own sleep is covered in test_daemon_signal_wake.
# ---------------------------------------------------------------------------


def test_sleep_until_woken_sleeps_the_whole_delay_in_slices() -> None:
    sleeps: list[float] = []
    sleep_until_woken(2.0, woken=lambda: False, sleep_fn=sleeps.append, slice_s=0.5)
    assert sleeps == [0.5, 0.5, 0.5, 0.5]


def test_sleep_until_woken_ends_the_last_slice_at_the_delay() -> None:
    sleeps: list[float] = []
    sleep_until_woken(1.25, woken=lambda: False, sleep_fn=sleeps.append, slice_s=0.5)
    assert sleeps == [0.5, 0.5, 0.25]


def test_sleep_until_woken_stops_after_the_slice_in_which_it_was_woken() -> None:
    """A signal handler that runs during the second slice sets the flag;
    the sleep ends there instead of running out the 60 s."""
    sleeps: list[float] = []
    flag = {"pending": False}

    def sleep_fn(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            flag["pending"] = True

    sleep_until_woken(60.0, woken=lambda: flag["pending"], sleep_fn=sleep_fn, slice_s=0.5)
    assert sleeps == [0.5, 0.5]


def test_sleep_until_woken_does_not_sleep_when_already_woken() -> None:
    """A request made before the sleep, during the tick, is seen first."""
    sleeps: list[float] = []
    sleep_until_woken(60.0, woken=lambda: True, sleep_fn=sleeps.append, slice_s=0.5)
    assert sleeps == []


def test_sleep_until_woken_does_not_sleep_for_a_zero_delay() -> None:
    sleeps: list[float] = []
    sleep_until_woken(0.0, woken=lambda: False, sleep_fn=sleeps.append, slice_s=0.5)
    assert sleeps == []


@pytest.mark.parametrize("delay", [-1.0, math.nan, math.inf, -math.inf])
def test_sleep_until_woken_rejects_a_delay_time_sleep_would_reject(delay: float) -> None:
    """``time.sleep`` raises for a negative, NaN or infinite delay. Here an
    infinite one would otherwise sleep forever, one slice at a time."""
    sleeps: list[float] = []
    with pytest.raises(ValueError, match="delay"):
        sleep_until_woken(delay, woken=lambda: False, sleep_fn=sleeps.append, slice_s=0.5)
    assert sleeps == []


@pytest.mark.parametrize("slice_s", [0.0, -0.5, math.nan, math.inf])
def test_sleep_until_woken_rejects_a_slice_that_is_not_a_positive_number(slice_s: float) -> None:
    """A zero slice never ends the sleep, and an infinite one never checks
    the flag."""
    sleeps: list[float] = []
    with pytest.raises(ValueError, match="slice_s"):
        sleep_until_woken(1.0, woken=lambda: False, sleep_fn=sleeps.append, slice_s=slice_s)
    assert sleeps == []


def test_sleep_until_woken_slices_by_the_signal_check_interval() -> None:
    """The daemon relies on the default slice to bound how late it sees a
    stop; pin it."""
    sleeps: list[float] = []
    sleep_until_woken(1.0, woken=lambda: False, sleep_fn=sleeps.append)
    assert SIGNAL_CHECK_INTERVAL_S == 0.5
    assert sleeps == [0.5, 0.5]


# ---------------------------------------------------------------------------
# run_one_tick: end-to-end pure state-machine driver
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    # Re-use the package defaults; the conftest `default_settings`
    # fixture also does this. Calling load_settings directly here
    # keeps the helper self-contained so other tests in this module
    # don't need to take a fixture they don't need.
    from claude_task_runner.config.loader import load_settings

    return load_settings(None)


def _initial_snapshot() -> SupervisorSnapshot:
    return SupervisorSnapshot.model_validate(
        {
            "state": SupervisorState.IDLE,
            "since": datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC),
        }
    )


def test_run_one_tick_idle_with_no_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """IDLE state + no pending tasks + no in-flight → stays IDLE,
    emits a MonitorInFlight action and that's it."""
    ctx = TickContext(
        settings=_settings(),
        poll_result=_make_reading(),
        pending_count=0,
        in_flight_count=0,
    )
    clock = FakeClock(datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC))
    snap, actions = run_one_tick(_initial_snapshot(), ctx, clock)
    assert snap.state == SupervisorState.IDLE
    # MonitorInFlight is always emitted on the idle-no-work branch.
    assert any(isinstance(a, MonitorInFlight) for a in actions)


def test_run_one_tick_active_with_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pending tasks + low utilisation → DISPATCHING."""
    ctx = TickContext(
        settings=_settings(),
        poll_result=_make_reading(),
        pending_count=5,
        in_flight_count=0,
    )
    clock = FakeClock(datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC))
    snap, _actions = run_one_tick(_initial_snapshot(), ctx, clock)
    # With default thresholds and 20%/40% utilisation, the state should be active.
    assert snap.state in (
        SupervisorState.DISPATCHING,
        SupervisorState.SLOWING_DOWN,
    )


def test_run_one_tick_with_capture_error_skips_state_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capture-level error (timeout / spawn) means no readings; the
    state machine returns the same snapshot."""
    timeout = UsageCaptureTimeout("nope")
    ctx = TickContext(
        settings=_settings(),
        poll_result=timeout,
        pending_count=5,
        in_flight_count=0,
    )
    clock = FakeClock(datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC))
    starting = _initial_snapshot()
    snap, actions = run_one_tick(starting, ctx, clock)
    assert snap.state == starting.state
    # The state machine emits an EmitEvent describing the capture error.
    assert any(isinstance(a, EmitEvent) and a.event_type == "usage_capture_error" for a in actions)
