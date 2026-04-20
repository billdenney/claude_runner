"""Auto-calibration of 5h / weekly token budgets from historical usage.

When ``plan = "auto"``, the controller delegates the budget numbers to
:func:`calibrate_budgets`, which consults the configured budget source for
the operator's own historical usage. This fixes a systematic
under-counting problem with the hard-coded plan presets on Claude Code
subscriptions: the cache-heavy workflows that Claude Code itself drives
routinely consume 50-150M tokens per 5h block, yet the official ``max5``
preset (which predates Claude Code's aggressive cache usage) is 2M.

The calibration policy:

- If the source exposes fewer than 10 non-gap blocks, fall back to the
  ``max5`` preset — not enough signal to calibrate.
- Otherwise set ``budget_5h_tokens`` to ``p90`` of the historical block
  totals (generous enough to let typical batches run, strict enough to
  stop a runaway 10x-concurrency burst).
- ``budget_weekly_tokens`` comes from ``p90`` of completed-week totals
  (excluding the current in-progress week). When there are fewer than 2
  completed weeks of data, fall back to ``block_p90 * 14`` as a rough
  proxy for two full weeks of typical usage.
- Floor both values at the ``max5`` preset so the fallback path is
  always safe.
- Cap both values at an absolute ceiling (``_HARD_5H_CAP``,
  ``_HARD_WEEK_CAP``) so an unusual historical outlier can't let the
  runner burn without bound.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from claude_runner.budget.sources import BudgetSource, HistoricalBudgetSource
from claude_runner.defaults import PLAN_PRESETS

_log = logging.getLogger(__name__)

# Absolute hard caps (tokens) — refuse to auto-configure anything higher
# than this, even if the operator's history shows larger usage. Protects
# against a historical accident setting the ceiling too high.
_HARD_5H_CAP = 300_000_000
_HARD_WEEK_CAP = 3_000_000_000

# Floor (tokens): never go below this regardless of history.
_FLOOR_PLAN = "max5"

# Minimum historical signal required to trust the calibration.
_MIN_BLOCKS = 10


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    budget_5h: int
    budget_weekly: int
    source: str
    n_blocks: int
    n_weeks: int
    reason: str


def _percentile(values: list[int], p: float) -> int:
    """Return the nearest-rank percentile (1-indexed) of ``values``.

    ``p`` is a fraction in ``[0, 1]``. Returns 0 on empty input.
    """
    if not values:
        return 0
    sv = sorted(values)
    idx = max(0, min(len(sv) - 1, round(p * (len(sv) - 1))))
    return sv[idx]


def _is_historical_source(source: object) -> bool:
    return hasattr(source, "historical_block_totals") and hasattr(
        source, "historical_weekly_totals"
    )


def calibrate_budgets(source: BudgetSource | None) -> CalibrationResult:
    """Pick realistic 5h and weekly budgets from a budget source's history.

    ``source`` may be ``None`` or a static source without historical data;
    in both cases the result falls back to the ``max5`` preset with a
    clear reason logged.
    """
    floor = PLAN_PRESETS[_FLOOR_PLAN]
    if source is None or not _is_historical_source(source):
        reason = (
            "no historical source available"
            if source is None
            else f"{getattr(source, 'name', type(source).__name__)} has no historical data"
        )
        _log.info("plan=auto: falling back to max5 preset (%s)", reason)
        return CalibrationResult(
            budget_5h=floor.budget_5h_tokens,
            budget_weekly=floor.budget_weekly_tokens,
            source=getattr(source, "name", "fallback") if source else "fallback",
            n_blocks=0,
            n_weeks=0,
            reason=reason,
        )

    hist_source: HistoricalBudgetSource = source  # type: ignore[assignment]
    try:
        blocks = hist_source.historical_block_totals()
        weeks = hist_source.historical_weekly_totals()
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("plan=auto: historical probe failed (%s); using max5 floor", exc)
        return CalibrationResult(
            budget_5h=floor.budget_5h_tokens,
            budget_weekly=floor.budget_weekly_tokens,
            source=getattr(source, "name", "unknown"),
            n_blocks=0,
            n_weeks=0,
            reason=f"historical probe raised {type(exc).__name__}",
        )

    if len(blocks) < _MIN_BLOCKS:
        _log.info(
            "plan=auto: only %d historical blocks (need %d); using max5 floor",
            len(blocks),
            _MIN_BLOCKS,
        )
        return CalibrationResult(
            budget_5h=floor.budget_5h_tokens,
            budget_weekly=floor.budget_weekly_tokens,
            source=getattr(source, "name", "unknown"),
            n_blocks=len(blocks),
            n_weeks=len(weeks),
            reason=f"only {len(blocks)} historical blocks (need {_MIN_BLOCKS})",
        )

    p90_block = _percentile(blocks, 0.90)
    if len(weeks) >= 2:
        p90_week = _percentile(weeks, 0.90)
    else:
        # Not enough completed weeks — approximate a realistic weekly
        # ceiling as two full weeks of the typical per-block peak.
        # (Claude Max plans reset every 5h, so up to 42 blocks per week.
        # Using p90_block * 14 is roughly "7 blocks/day for 2 days" —
        # deliberately conservative.)
        p90_week = p90_block * 14

    budget_5h = max(floor.budget_5h_tokens, min(p90_block, _HARD_5H_CAP))
    budget_weekly = max(floor.budget_weekly_tokens, min(p90_week, _HARD_WEEK_CAP))

    reason = (
        f"p90 of {len(blocks)} blocks = {p90_block:,}; p90 of {len(weeks)} weeks = {p90_week:,}"
    )
    _log.info(
        "plan=auto: calibrated from %s history — budget_5h=%s, budget_weekly=%s (%s)",
        getattr(source, "name", "unknown"),
        f"{budget_5h:,}",
        f"{budget_weekly:,}",
        reason,
    )
    return CalibrationResult(
        budget_5h=budget_5h,
        budget_weekly=budget_weekly,
        source=getattr(source, "name", "unknown"),
        n_blocks=len(blocks),
        n_weeks=len(weeks),
        reason=reason,
    )
