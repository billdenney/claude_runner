"""Failure classifier — TOML-driven pattern matching.

A pure function that maps an error string to one of four classes:

* ``environmental`` — transient infrastructure: rate limits, 5xx, network.
  Auto-resumed by the supervisor.
* ``operator`` — humans decided to defer / abort. Stays failed; never
  auto-retried.
* ``task`` — project-specific failures (e.g. compilation errors in an R
  extraction queue). Stays failed; operator triages.
* ``unknown`` — none of the above. Stays failed; surfaced to operator.

Pattern lists are configured per queue in ``[failure_classifier]`` of the
settings TOML. Built-in defaults cover the patterns that the previous
bash runner matched. See ADR-0012.

Precedence at classification time: **operator > task > environmental**.
This keeps deliberate operator decisions ("permanently disabled") from
being silently auto-retried as if they were rate limits.

Circuit breaker: callers check ``recent_consecutive_failures`` against
the threshold; this module provides only the per-error classification.
"""

from __future__ import annotations

from typing import Literal

from claude_task_runner.config.schema import FailureClassifierSettings

FailureClass = Literal["environmental", "operator", "task", "unknown"]


def classify(error_text: str | None, settings: FailureClassifierSettings) -> FailureClass:
    """Classify an error string by the configured pattern lists.

    Pattern matching is **case-insensitive substring** — the same shape
    the bash predecessor used. Patterns are compared in order:

    1. ``operator_patterns`` — operator-deferred / permanently-failed
    2. ``task_patterns`` — project-specific failure markers
    3. ``environmental_patterns`` — transient infrastructure errors
    4. otherwise ``"unknown"``

    A ``None`` or empty error text classifies as ``"unknown"``.

    Parameters
    ----------
    error_text
        The error message captured from the run. Often the
        ``RunRecord.error`` field or the last line of stderr.
    settings
        Loaded ``[failure_classifier]`` section.
    """
    if not error_text:
        return "unknown"

    needle = error_text.lower()

    # Operator wins: "permanently disabled" should never auto-retry even
    # if the message also mentions a rate limit.
    for pattern in settings.operator_patterns:
        if pattern.lower() in needle:
            return "operator"

    # Task patterns next: project-specific markers operators added.
    for pattern in settings.task_patterns:
        if pattern.lower() in needle:
            return "task"

    # Environmental last: transient infra. Safe to auto-resume.
    for pattern in settings.environmental_patterns:
        if pattern.lower() in needle:
            return "environmental"

    return "unknown"


def should_auto_resume(failure_class: FailureClass) -> bool:
    """Whether the supervisor should automatically retry this failure.

    Only ``"environmental"`` qualifies. Everything else (operator, task,
    unknown) requires human review — an unknown failure could be a
    silent corruption that auto-retry would mask.
    """
    return failure_class == "environmental"


def circuit_breaker_tripped(
    consecutive_failures: int,
    settings: FailureClassifierSettings,
) -> bool:
    """Return True when the configured threshold has been hit.

    Caller increments ``consecutive_failures`` on each retryable failure
    and resets to 0 on a successful run. When this returns True, the
    task transitions to ``failed_circuit_breaker`` and stops being
    auto-retried regardless of failure class.
    """
    return consecutive_failures >= settings.failure_circuit_breaker_threshold
