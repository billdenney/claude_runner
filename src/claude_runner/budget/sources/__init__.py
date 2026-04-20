from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(slots=True)
class UsageSnapshot:
    used_5h: int
    used_week: int
    next_5h_reset: datetime | None = None
    source: str = "unknown"


class BudgetSource(Protocol):
    """Queries current window usage from an external source."""

    name: str

    def snapshot(self) -> UsageSnapshot: ...


class HistoricalBudgetSource(Protocol):
    """Extension: sources that can surface historical block / week totals.

    Used by ``calibrate_budgets`` (``plan = "auto"``) to infer realistic 5h
    and weekly budgets from the operator's own historical usage rather than
    the static plan presets, which tend to undercount cache-heavy Claude
    Code workflows by 50-100x.
    """

    def historical_block_totals(self) -> list[int]: ...
    def historical_weekly_totals(self) -> list[int]: ...
