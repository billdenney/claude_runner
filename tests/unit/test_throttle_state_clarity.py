"""Tests for the THROTTLED_WEEKLY split and cause-specific messages (PR 9).

Before PR 9, ``_classify_active`` returned ``THROTTLED_5H`` for the
weekly-in-stop band (between slowdown and pause_at). Operators could
not tell from the state alone whether the throttle was driven by the
5h window or the weekly window. PR 9:

* Adds :attr:`SupervisorState.THROTTLED_WEEKLY` for the
  weekly-in-stop branch.
* Adds :func:`_throttle_cause_message` which returns a single sentence
  identifying the cause + the live observed utilization and effective
  bands.
* Emits cause-specific ``Notify`` and ``EmitEvent`` actions on state
  entry so dashboards can render the cause.

These tests exercise both classification and message production.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from claude_task_runner.supervisor.state_machine import (
    _classify_active,
    _EffectiveBands,
    _throttle_cause_message,
)
from claude_task_runner.supervisor.states import SupervisorState
from claude_task_runner.usage.models import UsageReading, WindowReading


def _reading(five_pct: int, weekly_pct: int) -> UsageReading:
    return UsageReading(
        captured_at=datetime(2026, 5, 22, tzinfo=UTC),
        five_hour=WindowReading(
            utilization_pct=five_pct,
            resets_at_raw="x",
            resets_at=datetime(2026, 5, 22, 17, tzinfo=UTC),
        ),
        seven_day=WindowReading(
            utilization_pct=weekly_pct,
            resets_at_raw="x",
            resets_at=datetime(2026, 5, 29, tzinfo=UTC),
        ),
    )


def _bands(
    *,
    five_full: int = 40,
    five_slow: int = 60,
    weekly_full: int = 70,
    weekly_slow: int = 90,
    weekly_pause_at: int = 90,
) -> _EffectiveBands:
    return _EffectiveBands(
        five_hour_full=five_full,
        five_hour_slow=five_slow,
        weekly_full=weekly_full,
        weekly_slow=weekly_slow,
        weekly_pause_at_pct=weekly_pause_at,
    )


# ---------------------------------------------------------------------------
# _classify_active — verify the precedence ladder.
# ---------------------------------------------------------------------------


def test_classify_weekly_pause_beats_5h_throttle() -> None:
    """PAUSED_WEEKLY wins even when 5h is also over its slowdown band."""
    state = _classify_active(
        reading=_reading(five_pct=80, weekly_pct=95),
        bands=_bands(five_slow=60, weekly_pause_at=90),
    )
    assert state is SupervisorState.PAUSED_WEEKLY


def test_classify_5h_throttle_beats_weekly_throttle() -> None:
    """When 5h is above its slowdown band, it wins over weekly-in-stop."""
    state = _classify_active(
        reading=_reading(five_pct=70, weekly_pct=85),
        bands=_bands(five_slow=60, weekly_slow=80, weekly_pause_at=95),
    )
    assert state is SupervisorState.THROTTLED_5H


def test_classify_weekly_throttle_when_5h_low() -> None:
    """The new state: weekly in stop band, 5h fine → THROTTLED_WEEKLY.

    Reproduces the operator's observed case (21% / 78%, pacing-adjusted
    weekly_slow ≈ 44%, weekly_pause_at = 90%): 78 >= 44 and < 90,
    while 5h = 21 < slow_band = 90."""
    state = _classify_active(
        reading=_reading(five_pct=21, weekly_pct=78),
        bands=_bands(five_slow=90, weekly_slow=44, weekly_pause_at=90),
    )
    assert state is SupervisorState.THROTTLED_WEEKLY


def test_classify_slowing_down_at_weekly_slow_band() -> None:
    """Weekly in slowdown band (between full and slow) → SLOWING_DOWN."""
    state = _classify_active(
        reading=_reading(five_pct=20, weekly_pct=72),
        bands=_bands(weekly_full=70, weekly_slow=90),
    )
    assert state is SupervisorState.SLOWING_DOWN


def test_classify_dispatching_when_all_clear() -> None:
    state = _classify_active(
        reading=_reading(five_pct=10, weekly_pct=20),
        bands=_bands(),
    )
    assert state is SupervisorState.DISPATCHING


# ---------------------------------------------------------------------------
# _throttle_cause_message — operator-readable explanations.
# ---------------------------------------------------------------------------


def test_message_identifies_5h_cause() -> None:
    msg = _throttle_cause_message(
        SupervisorState.THROTTLED_5H,
        _reading(five_pct=75, weekly_pct=20),
        _bands(five_slow=60),
    )
    assert "5h utilization 75%" in msg
    assert "60%" in msg
    # Should not blame the weekly window.
    assert "weekly" not in msg.lower()


def test_message_identifies_weekly_cause() -> None:
    msg = _throttle_cause_message(
        SupervisorState.THROTTLED_WEEKLY,
        _reading(five_pct=21, weekly_pct=78),
        _bands(weekly_slow=44, weekly_pause_at=90),
    )
    assert "weekly utilization 78%" in msg
    assert "44%" in msg
    assert "pause_at 90%" in msg


def test_message_identifies_pause_cause() -> None:
    msg = _throttle_cause_message(
        SupervisorState.PAUSED_WEEKLY,
        _reading(five_pct=20, weekly_pct=95),
        _bands(weekly_pause_at=90),
    )
    assert "weekly utilization 95%" in msg
    assert "pause_at 90%" in msg
    assert "hard pause" in msg


def test_message_explains_slowdown_with_both_windows() -> None:
    msg = _throttle_cause_message(
        SupervisorState.SLOWING_DOWN,
        _reading(five_pct=50, weekly_pct=72),
        _bands(five_full=40, five_slow=60, weekly_full=70, weekly_slow=90),
    )
    assert "5h=50%" in msg
    assert "weekly=72%" in msg


def test_message_empty_for_active_states() -> None:
    """DISPATCHING / IDLE / END_OF_WEEK_PUSH don't need a cause string."""
    for state in (
        SupervisorState.DISPATCHING,
        SupervisorState.IDLE,
        SupervisorState.END_OF_WEEK_PUSH,
    ):
        assert _throttle_cause_message(state, _reading(0, 0), _bands()) == ""


# ---------------------------------------------------------------------------
# Event emission on state entry.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("five_pct", "weekly_pct", "weekly_slow", "weekly_pause", "expected_state", "expected_event"),
    [
        (21, 78, 44, 90, SupervisorState.THROTTLED_WEEKLY, "throttled_weekly_entry"),
        (75, 20, 90, 95, SupervisorState.THROTTLED_5H, "throttled_5h_entry"),
        (20, 95, 80, 90, SupervisorState.PAUSED_WEEKLY, "paused_weekly_entry"),
    ],
)
def test_state_machine_emits_cause_specific_event(
    five_pct: int,
    weekly_pct: int,
    weekly_slow: int,
    weekly_pause: int,
    expected_state: SupervisorState,
    expected_event: str,
) -> None:
    """End-to-end: ``step()`` driving with the configured bands produces
    an entry event whose ``event_type`` names the cause.

    Implementation note: this test is small on purpose — full state-
    machine wiring is exercised by the larger ``test_state_machine.py``
    suite; here we just confirm the new event types fire.
    """
    from claude_task_runner.clock import FakeClock
    from claude_task_runner.config.loader import load_settings
    from claude_task_runner.supervisor.actions import EmitEvent
    from claude_task_runner.supervisor.state_machine import StepInput, step
    from claude_task_runner.supervisor.states import SupervisorSnapshot

    base = load_settings(None)
    # Force the configured bands by overriding via a settings copy. The
    # state machine reads from inp.settings_throttle.weekly.
    cfg = base.model_copy(
        update={
            "throttle": base.throttle.model_copy(
                update={
                    "weekly": base.throttle.weekly.model_copy(
                        update={
                            "band_full_dispatch_max_pct": max(0, weekly_slow - 20),
                            "band_slowdown_max_pct": weekly_slow,
                            "pause_at_pct": weekly_pause,
                            "pacing_curve_enabled": False,
                        }
                    ),
                    "five_hour": base.throttle.five_hour.model_copy(
                        update={
                            "band_full_dispatch_max_pct": max(
                                0, 40 if five_pct < 40 else five_pct - 20
                            ),
                            "band_slowdown_max_pct": (
                                90 if five_pct < 60 else max(60, five_pct - 5)
                            ),
                            "daytime_band_full_dispatch_max_pct": None,
                            "daytime_band_slowdown_max_pct": None,
                            "nighttime_band_full_dispatch_max_pct": None,
                            "nighttime_band_slowdown_max_pct": None,
                        }
                    ),
                }
            ),
        }
    )
    clock = FakeClock(datetime(2026, 5, 22, 12, 0, tzinfo=UTC))
    snap = SupervisorSnapshot(
        state=SupervisorState.DISPATCHING,
        since=datetime(2026, 5, 22, 11, 0, tzinfo=UTC),
    )
    reading = _reading(five_pct=five_pct, weekly_pct=weekly_pct)
    step_input = StepInput(
        snapshot=snap,
        reading=reading,
        settings_throttle=cfg.throttle,
        settings_supervisor=cfg.supervisor,
        settings_usage=cfg.usage,
        pending_count=1,
        in_flight_count=0,
    )
    new_snap, actions = step(step_input, clock)
    assert new_snap.state is expected_state
    events = [a for a in actions if isinstance(a, EmitEvent)]
    assert any(e.event_type == expected_event for e in events), (
        f"expected {expected_event}, got {[e.event_type for e in events]}"
    )
