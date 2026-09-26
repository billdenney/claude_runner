"""Circuit breaker for tasks that keep failing.

The orchestrator re-dispatches every ``failed`` task. After each failed run
the dispatcher counts the task's trailing failed runs, and once
:func:`circuit_breaker_tripped` says the count has reached
``[failure_classifier].failure_circuit_breaker_threshold`` it marks the task
``failed_circuit_breaker`` so it is not retried again.

ADR-0012 also planned to classify each failure by TOML pattern lists and
retry only transient ones. Nothing ever called that classifier, and its
pattern lists were retired (see ADR-0012's update of 2026-09-26).
"""

from __future__ import annotations

from claude_task_runner.config.schema import FailureClassifierSettings


def circuit_breaker_tripped(
    consecutive_failures: int,
    settings: FailureClassifierSettings,
) -> bool:
    """Return True when the configured threshold has been hit.

    Caller counts ``consecutive_failures`` as the task's trailing failed
    runs, which a successful run resets. When this returns True, the task
    transitions to ``failed_circuit_breaker`` and stops being auto-retried.
    """
    return consecutive_failures >= settings.failure_circuit_breaker_threshold
