from __future__ import annotations

from claude_runner.budget.circuit_breaker import CircuitBreaker


def _breaker(**overrides: int | float) -> CircuitBreaker:
    base = {
        "max_consecutive_failures": 3,
        "failure_rate_threshold": 0.5,
        "rolling_window": 10,
        "min_samples": 4,
    }
    base.update(overrides)
    return CircuitBreaker(**base)  # type: ignore[arg-type]


def test_trips_on_three_consecutive_failures() -> None:
    b = _breaker()
    b.record_failure()
    b.record_failure()
    assert not b.tripped()
    b.record_failure()
    assert b.tripped()
    assert "consecutive" in (b.state().reason or "")


def test_success_clears_consecutive_streak() -> None:
    # Disable the rolling-rate trip so we can isolate consecutive-streak behavior.
    b = _breaker(failure_rate_threshold=0.99, min_samples=1000)
    b.record_failure()
    b.record_failure()
    b.record_success()
    b.record_failure()
    b.record_failure()
    assert not b.tripped()


def test_rolling_rate_trips_above_threshold() -> None:
    b = _breaker(rolling_window=10, min_samples=4, failure_rate_threshold=0.5)
    # 3 fail, 2 succeed → rate 60% with min_samples met.
    b.record_failure()
    b.record_success()
    b.record_failure()
    b.record_success()
    b.record_failure()
    assert b.tripped()


def test_below_min_samples_does_not_trip() -> None:
    b = _breaker(min_samples=6, rolling_window=10, max_consecutive_failures=10)
    for _ in range(5):
        b.record_failure()
    # 5 samples < 6 min_samples, and 5 < max_consecutive_failures(10).
    assert not b.tripped()


def test_reset_clears_state() -> None:
    b = _breaker()
    b.record_failure()
    b.record_failure()
    b.record_failure()
    assert b.tripped()
    b.reset()
    assert not b.tripped()


def test_further_failures_after_trip_are_no_ops() -> None:
    """Once tripped, _evaluate should early-return and preserve the trip
    reason — not overwrite it with a new reason or re-record."""
    b = _breaker()
    for _ in range(3):
        b.record_failure()
    reason = b.state().reason
    # Keep recording — breaker stays tripped with the same reason.
    for _ in range(5):
        b.record_failure()
    assert b.tripped()
    assert b.state().reason == reason
