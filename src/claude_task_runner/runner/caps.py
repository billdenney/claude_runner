"""Per-task safety caps: token + wall-clock-duration ceilings.

Pure logic. The dispatcher composes :func:`evaluate_caps` with each
stream-json event tick to decide whether to ``SIGTERM`` an in-flight
task. The cap *enforcement* (sending the signal) is in
``runner.dispatcher`` — this module only decides.

Settings come from ``[task_caps]`` with optional per-task overrides on
:attr:`Task.max_tokens_override` and :attr:`Task.max_duration_s_override`.
A value of ``0`` in settings means "unlimited"; ``None`` on the task
override means "use settings value". The override always wins when
non-None.

Note: ``0`` means "no cap" / unlimited for both ``max_tokens`` and
``max_duration_s`` (i.e. the ``[task_caps]`` ``max_tokens_per_task`` and
``max_duration_s_per_task`` settings, and their per-task overrides).
A cap is only enforced when its value is strictly greater than ``0``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from claude_task_runner.config.schema import TaskCapsSettings
from claude_task_runner.queue.schema import Task

CapReason = Literal["tokens", "duration"]


@dataclass(frozen=True)
class CapViolation:
    """Returned when a cap has been exceeded.

    ``which`` says which cap; ``observed`` and ``cap`` give the values
    so the dispatcher can include them in the error message and the
    :attr:`RunRecord.killed_by_cap` field.
    """

    which: CapReason
    observed: float
    cap: float


def effective_token_cap(settings: TaskCapsSettings, task: Task) -> int:
    """Return the active token cap for a task. ``0`` means unlimited."""
    if task.max_tokens_override is not None:
        return task.max_tokens_override
    return settings.max_tokens_per_task


def effective_duration_cap_s(settings: TaskCapsSettings, task: Task) -> float:
    """Return the active duration cap for a task. ``0`` means unlimited."""
    if task.max_duration_s_override is not None:
        return task.max_duration_s_override
    return settings.max_duration_s_per_task


def evaluate_caps(
    *,
    settings: TaskCapsSettings,
    task: Task,
    cumulative_tokens: int,
    started_at: datetime,
    now: datetime,
) -> CapViolation | None:
    """Check whether either cap has been exceeded.

    Returns the first violation found, or ``None`` if all caps are within
    bounds. Token cap is checked first because it's the more common
    runaway mode.

    Caller is responsible for invoking this on a regular cadence (per
    stream-json tick is fine; a slow loop is also fine).
    """
    if cumulative_tokens < 0:
        raise ValueError("cumulative_tokens must be >= 0")
    if now < started_at:
        raise ValueError("now must be >= started_at")

    token_cap = effective_token_cap(settings, task)
    if token_cap > 0 and cumulative_tokens > token_cap:
        return CapViolation(
            which="tokens",
            observed=float(cumulative_tokens),
            cap=float(token_cap),
        )

    duration_cap = effective_duration_cap_s(settings, task)
    if duration_cap > 0:
        elapsed = (now - started_at).total_seconds()
        if elapsed > duration_cap:
            return CapViolation(
                which="duration",
                observed=elapsed,
                cap=duration_cap,
            )

    return None
