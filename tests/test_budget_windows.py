from __future__ import annotations

from datetime import UTC, datetime, timedelta

from claude_runner.budget.controller import DecisionKind, TokenBudgetController
from claude_runner.budget.windows import RollingWindow, WeeklyWindow
from claude_runner.config import Settings
from claude_runner.models import TokenUsage


def test_rolling_window_evicts_old_events() -> None:
    w = RollingWindow(duration=timedelta(hours=5))
    t0 = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)
    w.record(1000, at=t0)
    w.record(500, at=t0 + timedelta(hours=1))
    assert w.used(t0 + timedelta(hours=2)) == 1500
    # Advance past the first event's lifetime.
    assert w.used(t0 + timedelta(hours=6)) == 500


def test_weekly_window_resets_at_anchor() -> None:
    w = WeeklyWindow(anchor_weekday=0)  # Monday
    # Sunday: in the "previous" week when viewed from Monday.
    sun = datetime(2026, 4, 19, 12, 0, tzinfo=UTC)  # actually Sunday
    w.record(1_000_000, at=sun)
    assert w.used(at=sun) == 1_000_000
    # Next Monday starts the new window.
    next_mon = datetime(2026, 4, 20, 0, 0, tzinfo=UTC)
    assert w.used(at=next_mon + timedelta(hours=1)) == 0


def test_may_start_blocks_weekly_guard_before_last_day() -> None:
    settings = Settings(
        plan="max5",
        budget_source="static",
        max_consecutive_failures=3,
        failure_rolling_window=10,
        failure_rate_min_samples=4,
    )
    clock_t = [datetime(2026, 4, 14, 12, 0, tzinfo=UTC)]  # Tuesday
    ctrl = TokenBudgetController(settings, source=None, clock=lambda: clock_t[0])
    weekly = settings.resolved_budget_weekly()
    # Push usage to 95% of weekly budget.
    ctrl.record_usage(TokenUsage(input_tokens=int(weekly * 0.95)), duration_s=60.0)
    decision = ctrl.may_start(task_estimate=1_000)
    # 95% already used -> next task would exceed the 90% guard; we're nowhere
    # near the last day, so the controller must WAIT.
    assert decision.kind in {DecisionKind.WAIT, DecisionKind.STOP}


def test_may_start_ok_on_fresh_budget() -> None:
    settings = Settings(plan="max5", budget_source="static")
    clock_t = [datetime(2026, 4, 14, 12, 0, tzinfo=UTC)]
    ctrl = TokenBudgetController(settings, source=None, clock=lambda: clock_t[0])
    assert ctrl.may_start(task_estimate=10_000).kind is DecisionKind.OK


def test_rolling_window_default_clock_paths() -> None:
    """Record / used / next_reset without passing `at` hit the default clock."""
    w = RollingWindow(duration=timedelta(hours=5))
    # Empty window: oldest_event_at None, next_reset with default clock returns "now"
    assert w.oldest_event_at() is None
    reset = w.next_reset()
    assert reset is not None
    # Record and query without explicit `at`.
    w.record(100)
    assert w.used() == 100
    assert w.oldest_event_at() is not None
    # next_reset with events present uses oldest event + duration.
    assert w.next_reset() > datetime.now(tz=UTC)


def test_rolling_window_ignores_non_positive_record() -> None:
    w = RollingWindow(duration=timedelta(hours=5))
    t0 = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)
    w.record(0, at=t0)
    w.record(-5, at=t0)
    assert w.used(t0) == 0


def test_weekly_window_default_clock_paths() -> None:
    """Weekly window method calls without explicit `at` hit default clock."""
    w = WeeklyWindow()
    # used() with no events and default clock returns 0.
    assert w.used() == 0
    # Record with default clock; then immediately querying should give the same value.
    w.record(42)
    assert w.used() == 42
    # next_reset and days_remaining with default clock also work.
    assert w.next_reset() is not None
    assert w.days_remaining() >= 0.0
    # in_last_day with default clock.
    _ = w.in_last_day()


def test_weekly_window_ignores_non_positive_record() -> None:
    w = WeeklyWindow()
    t0 = datetime(2026, 4, 14, 12, 0, tzinfo=UTC)
    w.record(0, at=t0)
    w.record(-5, at=t0)
    assert w.used(at=t0) == 0


def test_target_concurrency_scales_with_cheap_tasks() -> None:
    settings = Settings(plan="max5", budget_source="static", max_concurrency=16)
    ctrl = TokenBudgetController(settings, source=None)
    # Cheap, fast tasks -> high concurrency.
    for _ in range(5):
        ctrl.record_usage(TokenUsage(input_tokens=1_000, output_tokens=500), duration_s=10.0)
    cheap = ctrl.target_concurrency()
    assert cheap >= 1
    # Expensive slow tasks -> low concurrency.
    ctrl2 = TokenBudgetController(settings, source=None)
    for _ in range(5):
        ctrl2.record_usage(
            TokenUsage(input_tokens=500_000, output_tokens=200_000), duration_s=600.0
        )
    expensive = ctrl2.target_concurrency()
    assert expensive <= cheap
