"""Tests for runner.retry — failure classifier."""

from __future__ import annotations

import pytest

from claude_task_runner.config.schema import FailureClassifierSettings
from claude_task_runner.runner.retry import (
    circuit_breaker_tripped,
    classify,
    should_auto_resume,
)


@pytest.fixture
def settings() -> FailureClassifierSettings:
    return FailureClassifierSettings(
        environmental_patterns=[
            "you've hit your limit",
            "ECONNRESET",
            "API returned HTTP 5",
        ],
        operator_patterns=[
            "Operator: defer",
            "permanently",
        ],
        task_patterns=[
            "compilation failed",
        ],
        failure_circuit_breaker_threshold=3,
    )


class TestClassify:
    def test_environmental_match(self, settings: FailureClassifierSettings) -> None:
        assert classify("You've hit your limit, retry later", settings) == "environmental"
        assert classify("read econnreset on socket", settings) == "environmental"

    def test_operator_match(self, settings: FailureClassifierSettings) -> None:
        assert classify("Operator: defer to morning", settings) == "operator"
        assert classify("This task is permanently disabled", settings) == "operator"

    def test_task_match(self, settings: FailureClassifierSettings) -> None:
        assert classify("compilation failed in step 3", settings) == "task"

    def test_unknown_match(self, settings: FailureClassifierSettings) -> None:
        assert classify("something weird happened", settings) == "unknown"

    def test_empty_is_unknown(self, settings: FailureClassifierSettings) -> None:
        assert classify("", settings) == "unknown"
        assert classify(None, settings) == "unknown"

    def test_operator_beats_environmental(self, settings: FailureClassifierSettings) -> None:
        # Both patterns match in the message; operator must win.
        msg = "Operator: defer because you've hit your limit"
        assert classify(msg, settings) == "operator"

    def test_task_beats_environmental(self, settings: FailureClassifierSettings) -> None:
        msg = "compilation failed and ECONNRESET also happened"
        assert classify(msg, settings) == "task"

    def test_case_insensitive(self, settings: FailureClassifierSettings) -> None:
        assert classify("YOU'VE HIT YOUR LIMIT", settings) == "environmental"
        assert classify("OPERATOR: DEFER", settings) == "operator"


class TestAutoResume:
    def test_only_environmental_resumes(self) -> None:
        assert should_auto_resume("environmental") is True
        assert should_auto_resume("operator") is False
        assert should_auto_resume("task") is False
        assert should_auto_resume("unknown") is False


class TestCircuitBreaker:
    def test_below_threshold(self, settings: FailureClassifierSettings) -> None:
        assert circuit_breaker_tripped(0, settings) is False
        assert circuit_breaker_tripped(2, settings) is False

    def test_at_threshold(self, settings: FailureClassifierSettings) -> None:
        assert circuit_breaker_tripped(3, settings) is True

    def test_above_threshold(self, settings: FailureClassifierSettings) -> None:
        assert circuit_breaker_tripped(10, settings) is True
