"""Failure-rate circuit breaker for the scheduler."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(slots=True)
class BreakerState:
    tripped: bool
    reason: str | None = None


class CircuitBreaker:
    """Trips on either consecutive failures or sustained rolling failure rate."""

    def __init__(
        self,
        *,
        max_consecutive_failures: int,
        failure_rate_threshold: float,
        rolling_window: int,
        min_samples: int,
    ) -> None:
        self._max_consecutive = max_consecutive_failures
        self._rate_threshold = failure_rate_threshold
        self._min_samples = min_samples
        self._rolling: deque[bool] = deque(maxlen=rolling_window)
        self._consecutive = 0
        self._state = BreakerState(tripped=False)

    def record_success(self) -> None:
        self._consecutive = 0
        self._rolling.append(True)
        self._evaluate()

    def record_failure(self) -> None:
        self._consecutive += 1
        self._rolling.append(False)
        self._evaluate()

    def tripped(self) -> bool:
        return self._state.tripped

    def state(self) -> BreakerState:
        return BreakerState(tripped=self._state.tripped, reason=self._state.reason)

    def reset(self) -> None:
        self._rolling.clear()
        self._consecutive = 0
        self._state = BreakerState(tripped=False)

    def _evaluate(self) -> None:
        if self._state.tripped:
            return
        if self._consecutive >= self._max_consecutive:
            self._state = BreakerState(
                tripped=True,
                reason=f"{self._consecutive} consecutive task failures",
            )
            return
        if len(self._rolling) >= self._min_samples:
            failures = sum(1 for ok in self._rolling if not ok)
            rate = failures / len(self._rolling)
            if rate > self._rate_threshold:
                self._state = BreakerState(
                    tripped=True,
                    reason=f"failure rate {rate:.0%} over last {len(self._rolling)} runs",
                )
