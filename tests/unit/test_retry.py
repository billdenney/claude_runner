"""Tests for runner.retry — the consecutive-failure circuit breaker."""

from __future__ import annotations

import pytest

from claude_task_runner.config.schema import FailureClassifierSettings
from claude_task_runner.runner.retry import circuit_breaker_tripped


@pytest.fixture
def settings() -> FailureClassifierSettings:
    return FailureClassifierSettings(failure_circuit_breaker_threshold=3)


class TestCircuitBreaker:
    def test_below_threshold(self, settings: FailureClassifierSettings) -> None:
        assert circuit_breaker_tripped(0, settings) is False
        assert circuit_breaker_tripped(2, settings) is False

    def test_at_threshold(self, settings: FailureClassifierSettings) -> None:
        assert circuit_breaker_tripped(3, settings) is True

    def test_above_threshold(self, settings: FailureClassifierSettings) -> None:
        assert circuit_breaker_tripped(10, settings) is True
