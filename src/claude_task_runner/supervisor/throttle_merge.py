"""Merge a per-account throttle policy into the queue-wide settings.

PR 13's wiring: the state machine reads from
:class:`ThrottleSettings`. To honour per-account overrides without
rewriting the state machine, the daemon constructs a *merged*
:class:`ThrottleSettings` per tick — queue-wide values where the
per-account policy is silent (``None``), per-account overrides where
the policy sets an explicit value.

This is a pure function: no I/O, deterministic, ready to call from
the daemon's hot path each tick.
"""

from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel

from claude_task_runner.config.schema import (
    AccountPolicy,
    ThrottleFiveHourSettings,
    ThrottleSettings,
    ThrottleWeeklySettings,
    TimeOfDaySettings,
)

_T = TypeVar("_T", bound=BaseModel)


def _override_non_none(base: _T, override: BaseModel, fields: tuple[str, ...]) -> _T:
    """Return a ``base.model_copy`` with each non-None field from ``override``.

    Pydantic's ``model_copy(update=...)`` accepts a dict of
    {field: value}; we build it by walking ``fields`` and dropping
    any value where ``override``'s corresponding field is None.
    """
    updates: dict[str, Any] = {}
    for f in fields:
        v = getattr(override, f, None)
        if v is not None:
            updates[f] = v
    if not updates:
        return base
    return base.model_copy(update=updates)


def merge_throttle_with_account(
    queue: ThrottleSettings,
    account: AccountPolicy,
) -> ThrottleSettings:
    """Layer a per-account policy on top of queue-wide throttle settings.

    Returns a new :class:`ThrottleSettings`. Fields the account left
    at ``None`` inherit from ``queue``; fields the account set
    explicitly override. The ``pacing_curve_enabled`` / boolean
    fields use the same rule (``None`` inherits).

    The hard ``pause_at_pct`` floor is per-field overridable just
    like everything else — but the pacing curve's safety semantics
    (it never tightens above ``pause_at_pct``) still apply against
    the *merged* value, so an account that raises pause_at to 95
    still cannot have its dynamic bands shifted above 95.
    """
    a_five = account.throttle.five_hour
    merged_five: ThrottleFiveHourSettings = _override_non_none(
        queue.five_hour,
        a_five,
        (
            "daytime_band_full_dispatch_max_pct",
            "daytime_band_slowdown_max_pct",
            "nighttime_band_full_dispatch_max_pct",
            "nighttime_band_slowdown_max_pct",
        ),
    )

    a_week = account.throttle.weekly
    merged_weekly: ThrottleWeeklySettings = _override_non_none(
        queue.weekly,
        a_week,
        (
            "band_full_dispatch_max_pct",
            "band_slowdown_max_pct",
            "pause_at_pct",
            "eow_push_enter_at_pct",
            "eow_target_pct",
            "eow_window_s",
            "eow_runtime_safety_factor",
            "pacing_curve_enabled",
            "pre_eow_target_pct",
            "pacing_slack_pp",
            "eow_push_nighttime_only",
        ),
    )

    a_tod = account.throttle.time_of_day
    merged_tod: TimeOfDaySettings = _override_non_none(
        queue.time_of_day,
        a_tod,
        ("day_end",),
    )

    return queue.model_copy(
        update={
            "five_hour": merged_five,
            "weekly": merged_weekly,
            "time_of_day": merged_tod,
        }
    )


__all__ = ["merge_throttle_with_account"]
