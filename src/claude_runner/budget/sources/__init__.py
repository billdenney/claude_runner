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
