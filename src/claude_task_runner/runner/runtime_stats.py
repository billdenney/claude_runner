"""Predictions over the EMA: p90 estimates and EOW-push fitness checks.

Thin wrapper over :mod:`runner.ema` that lifts EMA point estimates into
percentile-style predictions, used by callers that need a "will this
task fit before the next window reset?" estimate.

Until enough real samples accumulate, p90 is approximated as
``ema * runtime_p90_multiplier`` (default 1.5). After
``ema.prior_warmup_samples`` samples we still use the multiplier — true
percentiles would require keeping a sample buffer per task type, which
is a future enhancement.
"""

from __future__ import annotations

from claude_task_runner.config.schema import EMASettings
from claude_task_runner.queue.schema import Task
from claude_task_runner.runner import ema as ema_mod
from claude_task_runner.runner.ema import EMAFile


def p90_tokens(ema: EMAFile, task: Task, *, settings: EMASettings) -> float:
    """90th-percentile token estimate for a task."""
    return ema_mod.predict_tokens(ema, task, settings=settings) * settings.runtime_p90_multiplier


def p90_duration_s(ema: EMAFile, task: Task, *, settings: EMASettings) -> float:
    """90th-percentile duration estimate for a task, in seconds."""
    return (
        ema_mod.predict_duration_s(ema, task, settings=settings) * settings.runtime_p90_multiplier
    )


def fits_in_window(
    ema: EMAFile,
    task: Task,
    *,
    settings: EMASettings,
    seconds_until_reset: float,
    safety_factor: float,
) -> bool:
    """Decide whether a task is short enough to dispatch in EOW push.

    Returns True iff ``p90_duration_s(task) <= safety_factor * seconds_until_reset``.

    Tasks with no EMA history (and no configured prior) get
    ``p90 == 0`` and pass — but the supervisor refuses cold-start tasks
    in EOW push unless ``Task.force_dispatch_in_eow`` is True. That
    operator override lives in the supervisor; this function is purely
    geometric.
    """
    if seconds_until_reset <= 0:
        return False
    p90 = p90_duration_s(ema, task, settings=settings)
    return p90 <= safety_factor * seconds_until_reset


def has_warm_samples(ema: EMAFile, task: Task, *, settings: EMASettings) -> bool:
    """Whether the EMA bucket for this task has reached warm-up.

    Used by :func:`fits_in_window` callers to distinguish "fits because
    EMA confidently says so" from "fits trivially because no data exists".
    """
    bucket = ema.buckets.get(ema_mod.task_type_key(task))
    if bucket is None:
        return False
    return bucket.sample_count >= settings.prior_warmup_samples
