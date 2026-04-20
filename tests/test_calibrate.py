"""Tests for the ``plan = "auto"`` budget auto-calibration path."""

from __future__ import annotations

import pytest

from claude_runner.budget.calibrate import (
    _HARD_5H_CAP,
    _HARD_WEEK_CAP,
    calibrate_budgets,
)
from claude_runner.budget.controller import TokenBudgetController
from claude_runner.config import Settings
from claude_runner.defaults import PLAN_PRESETS


class _FakeHistSource:
    """A budget source that exposes deterministic historical totals."""

    name = "fake"

    def __init__(self, blocks: list[int], weeks: list[int]) -> None:
        self._blocks = blocks
        self._weeks = weeks

    def snapshot(self):  # pragma: no cover - not exercised in calibrate tests
        raise NotImplementedError

    def historical_block_totals(self) -> list[int]:
        return list(self._blocks)

    def historical_weekly_totals(self) -> list[int]:
        return list(self._weeks)


def test_calibrate_fallback_when_source_is_none() -> None:
    r = calibrate_budgets(None)
    assert r.budget_5h == PLAN_PRESETS["max5"].budget_5h_tokens
    assert r.budget_weekly == PLAN_PRESETS["max5"].budget_weekly_tokens
    assert r.source == "fallback"
    assert r.n_blocks == 0


def test_calibrate_fallback_when_source_lacks_historical_methods() -> None:
    class StaticOnly:
        name = "static"

        def snapshot(self): ...

    r = calibrate_budgets(StaticOnly())
    assert r.budget_5h == PLAN_PRESETS["max5"].budget_5h_tokens
    assert r.source == "static"
    assert "no historical data" in r.reason


def test_calibrate_fallback_when_too_few_blocks() -> None:
    src = _FakeHistSource(blocks=[50_000_000] * 5, weeks=[500_000_000])
    r = calibrate_budgets(src)
    assert r.budget_5h == PLAN_PRESETS["max5"].budget_5h_tokens
    assert r.n_blocks == 5
    assert "need 10" in r.reason


def test_calibrate_picks_p90_of_blocks() -> None:
    # 10 blocks with totals 10M ... 100M (sorted)
    blocks = [i * 10_000_000 for i in range(1, 11)]  # [10M, 20M, ..., 100M]
    weeks = [300_000_000, 400_000_000]
    src = _FakeHistSource(blocks=blocks, weeks=weeks)
    r = calibrate_budgets(src)
    # p90 of 10 values (indices 0..9) → index round(0.9*9)=8 → 90M
    assert r.budget_5h == 90_000_000
    # p90 of 2 values → index round(0.9*1)=1 → 400M
    assert r.budget_weekly == 400_000_000
    assert r.n_blocks == 10
    assert r.n_weeks == 2


def test_calibrate_approximates_weekly_from_block_when_few_weeks() -> None:
    blocks = [i * 10_000_000 for i in range(1, 11)]
    src = _FakeHistSource(blocks=blocks, weeks=[])  # no completed weeks
    r = calibrate_budgets(src)
    # p90 of 10 values → 90M; approximation is 90M * 14
    assert r.budget_weekly == 90_000_000 * 14


def test_calibrate_floors_at_max5_preset() -> None:
    """Very low historical usage → still get at least the max5 preset."""
    blocks = [1_000] * 15  # 1K tokens x 15 blocks (absurdly low)
    weeks = [10_000, 12_000, 8_000]
    src = _FakeHistSource(blocks=blocks, weeks=weeks)
    r = calibrate_budgets(src)
    assert r.budget_5h == PLAN_PRESETS["max5"].budget_5h_tokens
    assert r.budget_weekly == PLAN_PRESETS["max5"].budget_weekly_tokens


def test_calibrate_caps_at_hard_ceiling() -> None:
    """An outlier history doesn't let the budget run away unbounded."""
    # Above the 300M/3B caps
    blocks = [500_000_000] * 15
    weeks = [5_000_000_000] * 5
    src = _FakeHistSource(blocks=blocks, weeks=weeks)
    r = calibrate_budgets(src)
    assert r.budget_5h == _HARD_5H_CAP
    assert r.budget_weekly == _HARD_WEEK_CAP


def test_calibrate_survives_source_exception() -> None:
    """A probe that raises is caught and falls back to max5."""

    class Explodes:
        name = "explodes"

        def snapshot(self): ...
        def historical_block_totals(self) -> list[int]:
            raise RuntimeError("boom")

        def historical_weekly_totals(self) -> list[int]:
            raise RuntimeError("boom")

    r = calibrate_budgets(Explodes())
    assert r.budget_5h == PLAN_PRESETS["max5"].budget_5h_tokens
    assert "RuntimeError" in r.reason


def test_controller_uses_calibration_for_budget_when_plan_is_auto() -> None:
    settings = Settings(
        budget_source="static",
        plan="auto",
    )
    blocks = [i * 10_000_000 for i in range(1, 11)]  # p90 = 90M
    weeks = [200_000_000, 300_000_000]
    source = _FakeHistSource(blocks, weeks)
    controller = TokenBudgetController(settings, source=source)
    assert controller.budget_5h == 90_000_000
    assert controller.budget_week == 300_000_000  # p90 of 2 = index 1 (0-based)
    assert controller.calibration is not None
    assert controller.calibration.n_blocks == 10


def test_controller_ignores_calibration_for_other_plans() -> None:
    """plan != auto keeps the static preset / custom value regardless of source."""
    settings = Settings(budget_source="static", plan="max5")
    blocks = [500_000_000] * 20
    source = _FakeHistSource(blocks, [])
    controller = TokenBudgetController(settings, source=source)
    assert controller.calibration is None
    assert controller.budget_5h == PLAN_PRESETS["max5"].budget_5h_tokens


@pytest.mark.parametrize(
    "plan,expected_5h",
    [
        ("pro", PLAN_PRESETS["pro"].budget_5h_tokens),
        ("max5", PLAN_PRESETS["max5"].budget_5h_tokens),
        ("max20", PLAN_PRESETS["max20"].budget_5h_tokens),
        ("team", PLAN_PRESETS["team"].budget_5h_tokens),
    ],
)
def test_static_plans_still_work(plan: str, expected_5h: int) -> None:
    settings = Settings(budget_source="static", plan=plan)  # type: ignore[arg-type]
    controller = TokenBudgetController(settings, source=None)
    assert controller.budget_5h == expected_5h
    assert controller.calibration is None
