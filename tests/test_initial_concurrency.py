"""Tests for ``Settings.initial_concurrency`` + EMA-warm ramp in
``TokenBudgetController.target_concurrency``."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from claude_runner.budget.controller import TokenBudgetController
from claude_runner.config import Settings
from claude_runner.models import TokenUsage


def _settings(**overrides) -> Settings:
    base = dict(
        budget_source="static",
        plan="max5",
        max_concurrency=10,
        min_utilization=0.8,
        weekly_guard=0.9,
    )
    base.update(overrides)
    return Settings(**base)


def test_initial_concurrency_defaults_to_max_when_unset() -> None:
    """No ``initial_concurrency`` in config → historical behavior."""
    s = _settings(max_concurrency=10)
    assert s.initial_concurrency is None
    # The controller should use max_concurrency as the effective initial.
    # When EMA is cold, target should be the utilization-maximizing computation
    # clamped to max_concurrency. With no usage recorded, the existing code
    # path returns max_concurrency (historical behavior preserved).
    c = TokenBudgetController(s, source=None)
    # EMA is cold here, so target_concurrency returns min(initial, max) =
    # min(max, max) = max — same as before this feature landed.
    assert c.target_concurrency() == 10


def test_initial_concurrency_caps_target_before_ema_is_warm() -> None:
    """With initial=2, max=10, no tasks yet completed → target is 2."""
    s = _settings(initial_concurrency=2, max_concurrency=10)
    c = TokenBudgetController(s, source=None)
    assert c.ema_is_warm() is False
    assert c.target_concurrency() == 2


def test_initial_concurrency_releases_after_first_usage() -> None:
    """Once a task reports usage, the utilization-based target kicks in."""
    s = _settings(initial_concurrency=2, max_concurrency=10, ema_warm_after=1)
    c = TokenBudgetController(s, source=None)
    assert c.target_concurrency() == 2  # cold
    # Simulate one task completion: small usage → EMA says few tokens/sec, so
    # target_concurrency grows up to max_concurrency.
    c.record_usage(
        TokenUsage(input_tokens=100, output_tokens=10, cache_read_tokens=0),
        duration_s=60.0,
    )
    assert c.ema_is_warm() is True
    assert c.target_concurrency() == 10


def test_ema_warm_after_can_require_multiple_completions() -> None:
    """ema_warm_after=3 → cold for the first 2 completions."""
    s = _settings(initial_concurrency=1, max_concurrency=10, ema_warm_after=3)
    c = TokenBudgetController(s, source=None)
    assert c.target_concurrency() == 1
    c.record_usage(TokenUsage(input_tokens=50), duration_s=30.0)
    assert c.ema_is_warm() is False
    assert c.target_concurrency() == 1
    c.record_usage(TokenUsage(input_tokens=50), duration_s=30.0)
    assert c.ema_is_warm() is False
    assert c.target_concurrency() == 1
    c.record_usage(TokenUsage(input_tokens=50), duration_s=30.0)
    # Third completion → warm.
    assert c.ema_is_warm() is True


def test_completions_with_zero_usage_do_not_count_toward_warmup() -> None:
    """A task that crashed without emitting usage shouldn't falsely warm the
    EMA — it has no token-cost signal to contribute."""
    s = _settings(initial_concurrency=2, max_concurrency=10, ema_warm_after=1)
    c = TokenBudgetController(s, source=None)
    # Two "crashes" with zero billable tokens → still cold.
    c.record_usage(TokenUsage(input_tokens=0, output_tokens=0), duration_s=0.5)
    c.record_usage(TokenUsage(input_tokens=0, output_tokens=0), duration_s=0.5)
    assert c.ema_is_warm() is False
    assert c.target_concurrency() == 2


def test_config_rejects_initial_greater_than_max() -> None:
    """Pydantic validator catches the nonsensical combination at load."""
    with pytest.raises(ValidationError):
        Settings(
            budget_source="static",
            plan="max5",
            max_concurrency=2,
            initial_concurrency=5,
        )


def test_report_reflects_dynamic_target() -> None:
    """``BudgetReport.target_concurrency`` should track the warm-up state."""
    s = _settings(initial_concurrency=2, max_concurrency=10, ema_warm_after=1)
    c = TokenBudgetController(s, source=None)
    r1 = c.report()
    assert r1.target_concurrency == 2
    c.record_usage(TokenUsage(input_tokens=100), duration_s=10.0)
    r2 = c.report()
    assert r2.target_concurrency == 10
