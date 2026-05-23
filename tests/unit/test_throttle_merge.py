"""Tests for the per-account throttle merge helper (PR 13).

The merge produces a :class:`ThrottleSettings` where the per-account
:class:`AccountPolicy` overrides queue-wide fields when its values
are non-None and inherits queue-wide values when its fields are
None. This is the core machinery that makes per-account overrides
"feel" like a single ThrottleSettings to the state machine without
modifying the state machine itself.
"""

from __future__ import annotations

from claude_task_runner.config.loader import load_settings
from claude_task_runner.config.schema import (
    AccountConcurrencyPolicy,
    AccountPolicy,
    AccountThrottleFiveHour,
    AccountThrottlePolicy,
    AccountThrottleWeekly,
    AccountTimeOfDay,
)
from claude_task_runner.supervisor.throttle_merge import (
    merge_throttle_with_account,
)


def _queue_throttle():
    return load_settings(None).throttle


def _empty_policy() -> AccountPolicy:
    return AccountPolicy()


# ---------------------------------------------------------------------------
# Inheritance: all-None per-account policy yields queue-wide bit-for-bit.
# ---------------------------------------------------------------------------


def test_empty_policy_inherits_everything() -> None:
    queue = _queue_throttle()
    merged = merge_throttle_with_account(queue, _empty_policy())

    # Five-hour bands inherit.
    assert (
        merged.five_hour.daytime_band_full_dispatch_max_pct
        == queue.five_hour.daytime_band_full_dispatch_max_pct
    )
    assert (
        merged.five_hour.nighttime_band_slowdown_max_pct
        == queue.five_hour.nighttime_band_slowdown_max_pct
    )
    # Weekly fields inherit.
    assert merged.weekly.band_slowdown_max_pct == queue.weekly.band_slowdown_max_pct
    assert merged.weekly.pause_at_pct == queue.weekly.pause_at_pct
    assert merged.weekly.eow_window_s == queue.weekly.eow_window_s
    assert merged.weekly.eow_push_nighttime_only == queue.weekly.eow_push_nighttime_only
    assert merged.weekly.pacing_curve_enabled == queue.weekly.pacing_curve_enabled
    # Time of day inherits.
    assert merged.time_of_day.day_end == queue.time_of_day.day_end


# ---------------------------------------------------------------------------
# Override: explicit per-account values replace queue-wide.
# ---------------------------------------------------------------------------


def test_five_hour_overrides_apply() -> None:
    queue = _queue_throttle()
    policy = AccountPolicy(
        throttle=AccountThrottlePolicy(
            five_hour=AccountThrottleFiveHour(
                daytime_band_full_dispatch_max_pct=70,
                daytime_band_slowdown_max_pct=90,
            )
        )
    )
    merged = merge_throttle_with_account(queue, policy)
    assert merged.five_hour.daytime_band_full_dispatch_max_pct == 70
    assert merged.five_hour.daytime_band_slowdown_max_pct == 90
    # Nighttime untouched (inherits queue).
    assert (
        merged.five_hour.nighttime_band_full_dispatch_max_pct
        == queue.five_hour.nighttime_band_full_dispatch_max_pct
    )


def test_weekly_overrides_apply() -> None:
    queue = _queue_throttle()
    policy = AccountPolicy(
        throttle=AccountThrottlePolicy(
            weekly=AccountThrottleWeekly(
                band_slowdown_max_pct=85,
                pause_at_pct=95,
                eow_push_nighttime_only=False,
            )
        )
    )
    merged = merge_throttle_with_account(queue, policy)
    assert merged.weekly.band_slowdown_max_pct == 85
    assert merged.weekly.pause_at_pct == 95
    assert merged.weekly.eow_push_nighttime_only is False
    # Other weekly fields inherit.
    assert merged.weekly.eow_target_pct == queue.weekly.eow_target_pct
    assert merged.weekly.pre_eow_target_pct == queue.weekly.pre_eow_target_pct
    assert merged.weekly.pacing_slack_pp == queue.weekly.pacing_slack_pp


def test_time_of_day_override_applies() -> None:
    queue = _queue_throttle()
    policy = AccountPolicy(
        throttle=AccountThrottlePolicy(
            time_of_day=AccountTimeOfDay(day_end="20:30"),
        )
    )
    merged = merge_throttle_with_account(queue, policy)
    assert merged.time_of_day.day_end == "20:30"
    # day_start and ramp_minutes still inherit (not per-account-overridable).
    assert merged.time_of_day.day_start == queue.time_of_day.day_start
    assert merged.time_of_day.ramp_minutes == queue.time_of_day.ramp_minutes


# ---------------------------------------------------------------------------
# Mixed: some weekly, some five_hour overrides; rest inherit.
# ---------------------------------------------------------------------------


def test_mixed_override_inherit() -> None:
    queue = _queue_throttle()
    policy = AccountPolicy(
        concurrency=AccountConcurrencyPolicy(max_concurrency=3),
        throttle=AccountThrottlePolicy(
            five_hour=AccountThrottleFiveHour(
                daytime_band_full_dispatch_max_pct=70,
                daytime_band_slowdown_max_pct=90,
                nighttime_band_full_dispatch_max_pct=70,
                nighttime_band_slowdown_max_pct=90,
            ),
            weekly=AccountThrottleWeekly(
                band_slowdown_max_pct=85,
                pause_at_pct=95,
                eow_push_nighttime_only=False,
            ),
        ),
    )
    merged = merge_throttle_with_account(queue, policy)

    # Five-hour fully overridden.
    assert merged.five_hour.daytime_band_full_dispatch_max_pct == 70
    assert merged.five_hour.nighttime_band_slowdown_max_pct == 90
    # Weekly: explicit fields override, rest inherit.
    assert merged.weekly.band_slowdown_max_pct == 85
    assert merged.weekly.pause_at_pct == 95
    assert merged.weekly.eow_push_nighttime_only is False
    assert merged.weekly.band_full_dispatch_max_pct == queue.weekly.band_full_dispatch_max_pct
    assert merged.weekly.pacing_curve_enabled == queue.weekly.pacing_curve_enabled


# ---------------------------------------------------------------------------
# Immutability: merge returns a new ThrottleSettings; doesn't mutate inputs.
# ---------------------------------------------------------------------------


def test_merge_does_not_mutate_queue_settings() -> None:
    queue = _queue_throttle()
    original_pause = queue.weekly.pause_at_pct
    policy = AccountPolicy(
        throttle=AccountThrottlePolicy(
            weekly=AccountThrottleWeekly(pause_at_pct=99),
        )
    )
    merged = merge_throttle_with_account(queue, policy)
    assert merged.weekly.pause_at_pct == 99
    # Queue's pause_at unchanged.
    assert queue.weekly.pause_at_pct == original_pause


# ---------------------------------------------------------------------------
# Boolean override semantics: False is a valid override (not just None-vs-set).
# ---------------------------------------------------------------------------


def test_boolean_false_overrides_queue_true() -> None:
    """The work account wants eow_push_nighttime_only=False even though
    queue is True. Make sure False isn't confused with None-meaning-inherit."""
    queue = _queue_throttle()
    assert queue.weekly.eow_push_nighttime_only is True  # default
    policy = AccountPolicy(
        throttle=AccountThrottlePolicy(
            weekly=AccountThrottleWeekly(eow_push_nighttime_only=False),
        )
    )
    merged = merge_throttle_with_account(queue, policy)
    assert merged.weekly.eow_push_nighttime_only is False
