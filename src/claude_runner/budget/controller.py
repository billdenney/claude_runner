"""Token budget controller — concurrency sizing and start/wait/stop gating."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

from claude_runner.budget.sources import BudgetSource, UsageSnapshot
from claude_runner.budget.windows import RollingWindow, WeeklyWindow
from claude_runner.config import Settings
from claude_runner.models import TokenUsage

_log = logging.getLogger(__name__)

_FIVE_HOUR_SECONDS = 5 * 3600
_EMA_ALPHA = 0.3


class DecisionKind(str, Enum):
    OK = "ok"
    WAIT = "wait"
    STOP = "stop"


@dataclass(slots=True)
class Decision:
    kind: DecisionKind
    wait_until: datetime | None = None
    reason: str = ""


@dataclass(slots=True)
class BudgetReport:
    used_5h: int
    budget_5h: int
    used_week: int
    budget_week: int
    target_concurrency: int
    ema_tokens_per_task: float
    ema_duration_s: float
    source: str


class TokenBudgetController:
    def __init__(
        self,
        settings: Settings,
        *,
        source: BudgetSource | None,
        clock: object = None,
    ) -> None:
        self._settings = settings
        self._source = source
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._rolling = RollingWindow(duration=timedelta(hours=5))
        self._weekly = WeeklyWindow()
        self._last_snapshot: UsageSnapshot | None = None
        self._last_refresh_at: datetime | None = None
        self._in_flight_estimate = 0
        self._ema_tokens: float = 0.0
        self._ema_duration_s: float = 60.0  # seed: 1 minute
        self._observed_min_estimate = 0

    @property
    def budget_5h(self) -> int:
        override = self._source_ceiling_5h()
        return override if override else self._settings.resolved_budget_5h()

    @property
    def budget_week(self) -> int:
        return self._settings.resolved_budget_weekly()

    def _source_ceiling_5h(self) -> int:
        # When ccusage gives us a concrete "used" number, we can still rely on the
        # configured budget — we don't let the source shrink the ceiling.
        return 0

    def now(self) -> datetime:
        clock = self._clock
        return clock() if callable(clock) else clock  # type: ignore[return-value]

    # ----- usage accounting --------------------------------------------

    def refresh(self) -> UsageSnapshot | None:
        if self._source is None:
            return None
        try:
            snapshot = self._source.snapshot()
        except Exception as exc:
            _log.warning("budget source %s failed: %s", getattr(self._source, "name", "?"), exc)
            return self._last_snapshot
        self._last_snapshot = snapshot
        self._last_refresh_at = self.now()
        return snapshot

    def record_usage(self, usage: TokenUsage, *, duration_s: float) -> None:
        now = self.now()
        billable = usage.billable_total
        self._rolling.record(billable, at=now)
        self._weekly.record(billable, at=now)
        if billable > 0:
            if self._ema_tokens <= 0:
                self._ema_tokens = float(billable)
            else:
                self._ema_tokens = _EMA_ALPHA * billable + (1 - _EMA_ALPHA) * self._ema_tokens
        if duration_s > 0:
            if self._ema_duration_s <= 0:
                self._ema_duration_s = duration_s
            else:
                self._ema_duration_s = (
                    _EMA_ALPHA * duration_s + (1 - _EMA_ALPHA) * self._ema_duration_s
                )

    def reserve(self, estimate: int) -> None:
        self._in_flight_estimate += max(estimate, 0)

    def release(self, estimate: int) -> None:
        self._in_flight_estimate = max(self._in_flight_estimate - max(estimate, 0), 0)

    # ----- queries -----------------------------------------------------

    def used_5h(self) -> int:
        """Tokens used in the current 5-hour window — from source if available, else internal."""
        if self._last_snapshot is not None:
            return max(self._last_snapshot.used_5h, self._rolling.used(self.now()))
        return self._rolling.used(self.now())

    def used_week(self) -> int:
        if self._last_snapshot is not None and self._last_snapshot.used_week > 0:
            return max(self._last_snapshot.used_week, self._weekly.used(self.now()))
        return self._weekly.used(self.now())

    def remaining_5h(self) -> int:
        return max(self.budget_5h - self.used_5h() - self._in_flight_estimate, 0)

    def remaining_week(self) -> int:
        return max(self.budget_week - self.used_week() - self._in_flight_estimate, 0)

    def target_concurrency(self) -> int:
        """Pick N so that expected utilization ≥ min_utilization of the 5h budget."""
        target_tps = self._settings.min_utilization * self.budget_5h / _FIVE_HOUR_SECONDS
        per_task_tps = max(self._ema_tokens / self._ema_duration_s, 1.0)
        needed = max(1, int(-(-target_tps // per_task_tps)))  # ceil
        return min(needed, self._settings.max_concurrency)

    def may_start(self, task_estimate: int) -> Decision:
        now = self.now()
        estimate = max(task_estimate, 1)
        remaining_week = self.budget_week - self.used_week()
        remaining_5h = self.budget_5h - self.used_5h() - self._in_flight_estimate

        weekly_used_after = self.used_week() + self._in_flight_estimate + estimate
        weekly_cap = self._settings.weekly_guard * self.budget_week

        if weekly_used_after > self.budget_week:
            return Decision(
                kind=DecisionKind.STOP,
                reason=f"weekly budget ({self.budget_week}) would be exceeded",
            )
        if weekly_used_after > weekly_cap and not self._weekly.in_last_day(now):
            next_rolling_reset = self._rolling.next_reset(now)
            return Decision(
                kind=DecisionKind.WAIT,
                wait_until=min(next_rolling_reset, self._weekly.next_reset(now)),
                reason=(
                    f"weekly guard {self._settings.weekly_guard:.0%} hit with "
                    f"{self._weekly.days_remaining(now):.1f}d left"
                ),
            )
        if remaining_5h < estimate:
            return Decision(
                kind=DecisionKind.WAIT,
                wait_until=self._rolling.next_reset(now),
                reason="5-hour window would be exceeded by this task",
            )
        _ = remaining_week
        return Decision(kind=DecisionKind.OK)

    def report(self) -> BudgetReport:
        source = self._last_snapshot.source if self._last_snapshot else "static"
        return BudgetReport(
            used_5h=self.used_5h(),
            budget_5h=self.budget_5h,
            used_week=self.used_week(),
            budget_week=self.budget_week,
            target_concurrency=self.target_concurrency(),
            ema_tokens_per_task=self._ema_tokens,
            ema_duration_s=self._ema_duration_s,
            source=source,
        )
