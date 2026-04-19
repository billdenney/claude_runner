"""Fine-grained tests for TokenBudgetController internals that aren't
exercised by the scheduler/end-to-end paths."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from claude_runner.budget.controller import DecisionKind, TokenBudgetController
from claude_runner.budget.sources import UsageSnapshot
from claude_runner.config import Settings
from claude_runner.models import TokenUsage


class _ExplodingSource:
    name = "boom"

    def snapshot(self) -> UsageSnapshot:  # pragma: no cover - raises
        raise RuntimeError("source offline")


class _StaticSource:
    name = "static-test"

    def __init__(self, used_5h: int = 0, used_week: int = 0) -> None:
        self._used_5h = used_5h
        self._used_week = used_week

    def snapshot(self) -> UsageSnapshot:
        return UsageSnapshot(used_5h=self._used_5h, used_week=self._used_week, source=self.name)


def _settings() -> Settings:
    return Settings(plan="max5", budget_source="static")


def test_refresh_returns_none_when_no_source() -> None:
    ctrl = TokenBudgetController(_settings(), source=None)
    assert ctrl.refresh() is None


def test_refresh_logs_and_keeps_prior_snapshot_on_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If source.snapshot() raises, we log and return the last good snapshot."""
    settings = _settings()
    source = _StaticSource(used_5h=1000)
    ctrl = TokenBudgetController(settings, source=source)
    snap = ctrl.refresh()
    assert snap is not None
    # Swap in a raising source; refresh must not crash, must return prior snapshot.
    ctrl._source = _ExplodingSource()  # type: ignore[attr-defined]
    with caplog.at_level("WARNING"):
        recovered = ctrl.refresh()
    assert recovered is snap
    assert any("source offline" in rec.message or "boom" in rec.message for rec in caplog.records)


def test_record_usage_seeds_ema_on_first_duration() -> None:
    """First call with duration_s > 0 and zero'd EMA takes the duration directly."""
    ctrl = TokenBudgetController(_settings(), source=None)
    # Force the seed condition for duration EMA (normally seeded to 60s).
    ctrl._ema_duration_s = 0.0  # type: ignore[attr-defined]
    ctrl.record_usage(TokenUsage(input_tokens=100), duration_s=42.0)
    assert ctrl._ema_duration_s == 42.0  # type: ignore[attr-defined]


def test_record_usage_skips_duration_ema_when_duration_is_zero() -> None:
    """duration_s == 0 (e.g. a backend failure with no run time) should not
    update the duration EMA."""
    ctrl = TokenBudgetController(_settings(), source=None)
    ema_before = ctrl._ema_duration_s  # type: ignore[attr-defined]
    ctrl.record_usage(TokenUsage(input_tokens=10), duration_s=0.0)
    assert ctrl._ema_duration_s == ema_before  # type: ignore[attr-defined]


def test_remaining_5h_and_week_apply_in_flight_estimate() -> None:
    ctrl = TokenBudgetController(_settings(), source=None)
    budget_5h = ctrl.budget_5h
    budget_week = ctrl.budget_week
    ctrl.reserve(1_000)
    assert ctrl.remaining_5h() == max(budget_5h - 1_000, 0)
    assert ctrl.remaining_week() == max(budget_week - 1_000, 0)
    ctrl.release(1_000)
    assert ctrl.remaining_5h() == budget_5h
    assert ctrl.remaining_week() == budget_week


def test_may_start_waits_when_5h_window_would_exceed() -> None:
    """Usage close to the 5h ceiling forces WAIT with a wait_until."""
    settings = _settings()
    clock = [datetime(2026, 4, 14, 12, 0, tzinfo=UTC)]  # Tuesday
    ctrl = TokenBudgetController(settings, source=None, clock=lambda: clock[0])
    five_h = settings.resolved_budget_5h()
    # Burn through nearly all of the 5h budget.
    ctrl.record_usage(TokenUsage(input_tokens=five_h - 100), duration_s=60.0)
    decision = ctrl.may_start(task_estimate=1_000)
    assert decision.kind is DecisionKind.WAIT
    assert decision.wait_until is not None
    assert "5-hour" in decision.reason


def test_may_start_stops_when_weekly_budget_would_exceed() -> None:
    settings = _settings()
    clock = [datetime(2026, 4, 14, 12, 0, tzinfo=UTC)]
    ctrl = TokenBudgetController(settings, source=None, clock=lambda: clock[0])
    weekly = settings.resolved_budget_weekly()
    ctrl.record_usage(TokenUsage(input_tokens=weekly), duration_s=60.0)
    decision = ctrl.may_start(task_estimate=1_000)
    assert decision.kind is DecisionKind.STOP
    assert "weekly" in decision.reason


def test_used_5h_uses_internal_window_when_source_absent() -> None:
    settings = _settings()
    clock = [datetime(2026, 4, 14, 12, 0, tzinfo=UTC)]
    ctrl = TokenBudgetController(settings, source=None, clock=lambda: clock[0])
    ctrl.record_usage(TokenUsage(input_tokens=500), duration_s=1.0)
    # No source snapshot -> rolling window is authoritative.
    assert ctrl.used_5h() == 500
    assert ctrl.used_week() == 500


def test_used_5h_uses_max_of_source_and_internal() -> None:
    settings = _settings()
    clock = [datetime(2026, 4, 14, 12, 0, tzinfo=UTC)]
    source = _StaticSource(used_5h=2_000, used_week=10_000)
    ctrl = TokenBudgetController(settings, source=source, clock=lambda: clock[0])
    ctrl.refresh()
    # Our own rolling window hasn't recorded anything, source wins.
    assert ctrl.used_5h() == 2_000
    assert ctrl.used_week() == 10_000
    # Record more than the source says.
    ctrl.record_usage(TokenUsage(input_tokens=5_000), duration_s=1.0)
    assert ctrl.used_5h() == 5_000
    assert ctrl.used_week() == 10_000


def test_clock_accepts_non_callable_value() -> None:
    """Passing a datetime object directly (instead of a callable) is allowed."""
    fixed = datetime(2026, 4, 14, 12, 0, tzinfo=UTC)
    settings = _settings()
    ctrl = TokenBudgetController(settings, source=None, clock=fixed)
    assert ctrl.now() == fixed


def test_report_source_reflects_last_snapshot() -> None:
    settings = _settings()
    source = _StaticSource(used_5h=100)
    ctrl = TokenBudgetController(settings, source=source)
    ctrl.refresh()
    rep = ctrl.report()
    assert rep.source == "static-test"


def test_report_defaults_to_static_when_no_snapshot() -> None:
    ctrl = TokenBudgetController(_settings(), source=None)
    assert ctrl.report().source == "static"


def test_release_floors_at_zero() -> None:
    ctrl = TokenBudgetController(_settings(), source=None)
    ctrl.reserve(100)
    ctrl.release(500)  # more than reserved
    assert ctrl.remaining_5h() == ctrl.budget_5h


def test_used_week_source_with_zero_week_falls_back_to_internal() -> None:
    """If the source reports 0 for the week, we fall back to our internal window."""
    settings = _settings()
    clock = [datetime(2026, 4, 14, 12, 0, tzinfo=UTC)]
    source = _StaticSource(used_5h=100, used_week=0)
    ctrl = TokenBudgetController(settings, source=source, clock=lambda: clock[0])
    ctrl.refresh()
    ctrl.record_usage(TokenUsage(input_tokens=7_777), duration_s=1.0)
    # Source weekly is 0 -> our internal rolling weekly wins.
    assert ctrl.used_week() == 7_777


def test_source_ceiling_5h_is_disabled() -> None:
    """The private helper intentionally returns 0 so the source can't shrink ceilings."""
    ctrl = TokenBudgetController(_settings(), source=None)
    assert ctrl._source_ceiling_5h() == 0  # type: ignore[attr-defined]
    # And the public budget_5h matches the configured ceiling.
    assert ctrl.budget_5h == ctrl._settings.resolved_budget_5h()  # type: ignore[attr-defined]


def test_may_start_waits_on_weekly_guard_but_stays_under_hard_cap() -> None:
    """Used is above weekly_guard% but below 100% and we're not in the last day."""
    settings = _settings()
    clock = [datetime(2026, 4, 14, 12, 0, tzinfo=UTC)]  # Tuesday
    ctrl = TokenBudgetController(settings, source=None, clock=lambda: clock[0])
    weekly = settings.resolved_budget_weekly()
    # 92% used — above 90% guard but well below 100%.
    ctrl.record_usage(TokenUsage(input_tokens=int(weekly * 0.92)), duration_s=60.0)
    decision = ctrl.may_start(task_estimate=1_000)
    assert decision.kind is DecisionKind.WAIT
    assert decision.wait_until is not None
    assert "weekly guard" in decision.reason


def test_may_start_skips_weekly_guard_on_last_day() -> None:
    """On the last day of the week, the weekly guard relaxes — OK even above 90%."""
    settings = _settings()
    # Use a mutable clock so we can record weekly usage >5h ago (to clear the
    # rolling 5h window) but still sit in the last day of the week when asking.
    current = [datetime(2026, 4, 13, 12, 0, tzinfo=UTC)]  # earlier in the week
    ctrl = TokenBudgetController(settings, source=None, clock=lambda: current[0])
    weekly = settings.resolved_budget_weekly()
    ctrl.record_usage(TokenUsage(input_tokens=int(weekly * 0.92)), duration_s=60.0)
    # Jump to Sunday 23:30 — inside the "last day" window.
    current[0] = datetime(2026, 4, 19, 23, 30, tzinfo=UTC)
    decision = ctrl.may_start(task_estimate=1_000)
    # Should not WAIT on the weekly guard; OK (rolling 5h has aged out).
    assert decision.kind is DecisionKind.OK


def test_target_concurrency_clamps_to_max() -> None:
    """With extremely cheap tasks, concurrency would overshoot — gets clamped."""
    settings = Settings(plan="max5", budget_source="static", max_concurrency=2)
    ctrl = TokenBudgetController(settings, source=None)
    # Record a single cheap, fast task so EMA gets seeded low.
    ctrl.record_usage(TokenUsage(input_tokens=1, output_tokens=1), duration_s=1.0)
    # Per-task throughput is tiny; needed concurrency would be > 2 but clamped.
    assert ctrl.target_concurrency() == 2
