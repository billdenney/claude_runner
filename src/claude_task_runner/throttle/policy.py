"""Merge queue-side + per-account ``[dispatch_pct.*]`` into a frozen ResolvedPolicy.

The state-machine decision function (``throttle.decision.decide``)
consumes :class:`ResolvedPolicy` — a fully merged, no-Optional view of
the dispatch policy for one account at one tick. Per-account fields
that are ``None`` inherit the queue-wide value for that field; the
queue-wide ``[dispatch_pct.*]`` block is required (all fields
non-optional in the schema), so every resolved field is always present.

Pure function. No I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import TypeVar

from claude_task_runner.config.duration import parse_duration
from claude_task_runner.config.schema import (
    AccountPolicy,
    Settings,
)
from claude_task_runner.throttle import time_of_day as _tod

_T = TypeVar("_T")


@dataclass(frozen=True)
class ResolvedBand:
    """5h-utilization thresholds for one named band (day or night)."""

    fivehr_slowdown_pct: int
    fivehr_stop_pct: int


@dataclass(frozen=True)
class ResolvedNight(ResolvedBand):
    """The night band plus its local-time window."""

    time_start: time
    time_end: time


@dataclass(frozen=True)
class ResolvedWeek:
    """The weekly trace target curve, with the EOW switch already parsed."""

    early_pct: int
    eow_pct: int
    eow_time_switch_s: float


@dataclass(frozen=True)
class ResolvedPolicy:
    """Merged, no-Optional view of the dispatch policy for one account.

    Produced once per tick by :func:`resolve` from the queue-wide
    :class:`Settings` and the per-account :class:`AccountPolicy`.
    Downstream consumers read concrete values without per-field merge
    logic.
    """

    account_name: str
    max_concurrency: int
    timezone: str
    day: ResolvedBand
    night: ResolvedNight
    week: ResolvedWeek


def _pick(override: _T | None, fallback: _T) -> _T:
    """Return ``override`` when non-None, else ``fallback``."""
    return fallback if override is None else override


def resolve(
    queue: Settings,
    account_policy: AccountPolicy,
    account_name: str,
) -> ResolvedPolicy:
    """Compose queue-wide ``[dispatch_pct.*]`` with per-account overrides.

    The per-account ``[concurrency].max_concurrency`` is always
    explicit (the schema requires it ``ge=1`` with default ``1``);
    the queue-wide ``[concurrency]`` is not read here because
    per-account concurrency is the authoritative source for dispatch
    gating per PR 13.
    """
    dp = queue.dispatch_pct
    acct = account_policy.dispatch_pct

    day = ResolvedBand(
        fivehr_slowdown_pct=_pick(acct.day.fivehr_slowdown_pct, dp.day.fivehr_slowdown_pct),
        fivehr_stop_pct=_pick(acct.day.fivehr_stop_pct, dp.day.fivehr_stop_pct),
    )
    night = ResolvedNight(
        fivehr_slowdown_pct=_pick(acct.night.fivehr_slowdown_pct, dp.night.fivehr_slowdown_pct),
        fivehr_stop_pct=_pick(acct.night.fivehr_stop_pct, dp.night.fivehr_stop_pct),
        time_start=_tod.parse_hhmm(_pick(acct.night.time_start, dp.night.time_start)),
        time_end=_tod.parse_hhmm(_pick(acct.night.time_end, dp.night.time_end)),
    )
    week = ResolvedWeek(
        early_pct=_pick(acct.week.early_pct, dp.week.early_pct),
        eow_pct=_pick(acct.week.eow_pct, dp.week.eow_pct),
        eow_time_switch_s=parse_duration(_pick(acct.week.eow_time_switch, dp.week.eow_time_switch)),
    )
    timezone = _pick(acct.timezone, dp.timezone)
    return ResolvedPolicy(
        account_name=account_name,
        max_concurrency=account_policy.concurrency.max_concurrency,
        timezone=timezone,
        day=day,
        night=night,
        week=week,
    )


__all__ = [
    "ResolvedBand",
    "ResolvedNight",
    "ResolvedPolicy",
    "ResolvedWeek",
    "resolve",
]
