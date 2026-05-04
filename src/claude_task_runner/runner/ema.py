"""Per-task-type EMA of token usage and wall-clock duration.

A *task type* is the combination of ``(model, effort, allowed_tools_hash)``
— tasks with the same triple share an EMA cohort because their cost
profile should be similar. Tasks with bespoke tag lists (per
:attr:`Task.tags`) can override the cohort key — useful when the
operator knows two superficially-similar tasks have very different
runtimes.

The EMA file is a single JSON document at
``<queue>/.claude_task_runner/ema.json``. Atomic writes via
:mod:`tempfile` mirror :mod:`queue.store`. See ADR-0011.

Cold start: with fewer than ``[ema].prior_warmup_samples`` real
observations, predictions blend the configured cold-start prior with
the observed mean. After the warm-up threshold the prior is dropped.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from claude_task_runner.clock import Clock
from claude_task_runner.config.schema import EMAPrior, EMASettings
from claude_task_runner.queue.schema import CURRENT_SCHEMA_VERSION, Task

EMA_FILE_NAME = "ema.json"


class EMAFileError(ValueError):
    """The EMA JSON file is malformed or violates schema."""


class TaskTypeEMA(BaseModel):
    """One task-type's EMA bucket."""

    model_config = ConfigDict(extra="forbid")

    task_type: str
    sample_count: int = Field(ge=0, default=0)
    token_ema: float = Field(ge=0.0, default=0.0)
    duration_s_ema: float = Field(gt=-1e-9, default=0.0)
    last_updated: datetime | None = None


class EMAFile(BaseModel):
    """Root document persisted as ``ema.json``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = CURRENT_SCHEMA_VERSION
    buckets: dict[str, TaskTypeEMA] = Field(default_factory=dict)


def task_type_key(task: Task) -> str:
    """Compute the EMA bucket key for a task.

    Default: ``"{model}|{effort}|tools-{sha256[:8]}"``. Hashing the tool
    list keeps keys stable even when the tool order changes (we sort
    before hashing).

    Operators can override the bucketing by setting
    ``Task.tags = ["ema-cohort:<label>"]`` — the first matching tag is
    appended to the key, isolating the task into its own (or a shared)
    cohort.
    """
    tools_hash = hashlib.sha256(",".join(sorted(task.allowed_tools)).encode("utf-8")).hexdigest()[
        :8
    ]
    base = f"{task.model}|{task.effort}|tools-{tools_hash}"
    cohort = next(
        (t.split(":", 1)[1] for t in task.tags if t.startswith("ema-cohort:")),
        None,
    )
    return f"{base}|cohort-{cohort}" if cohort else base


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise EMAFileError(f"failed to read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EMAFileError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise EMAFileError(f"{path}: top-level JSON must be an object")
    return data


def load(path: Path) -> EMAFile:
    """Load the EMA file at ``path``. Returns an empty document if absent."""
    if not path.exists():
        return EMAFile()
    payload = _read_payload(path)
    sv = payload.get("schema_version", CURRENT_SCHEMA_VERSION)
    if sv != CURRENT_SCHEMA_VERSION:
        raise EMAFileError(
            f"{path}: schema_version={sv} does not match supported {CURRENT_SCHEMA_VERSION}"
        )
    try:
        return EMAFile.model_validate(payload)
    except ValidationError as exc:
        raise EMAFileError(f"{path}: {exc}") from exc


def write_atomic(ema: EMAFile, path: Path) -> None:
    """Atomic JSON write of the EMA document."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    payload = ema.model_dump(mode="json")
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=parent,
        delete=False,
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True, default=str)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def update_bucket(
    ema: EMAFile,
    task_type: str,
    *,
    observed_tokens: int,
    observed_duration_s: float,
    clock: Clock,
    alpha: float,
) -> EMAFile:
    """Return a new ``EMAFile`` with ``task_type``'s bucket updated.

    First sample seeds the EMA with the observed value (no smoothing
    against zero, which would suppress the first reading by ``1-alpha``).
    Subsequent samples apply standard exponential smoothing.

    Pure: returns a fresh ``EMAFile`` rather than mutating the input.
    """
    if not 0.0 < alpha <= 1.0:
        raise ValueError(f"alpha must be in (0, 1], got {alpha}")
    if observed_tokens < 0:
        raise ValueError("observed_tokens must be >= 0")
    if observed_duration_s < 0:
        raise ValueError("observed_duration_s must be >= 0")

    existing = ema.buckets.get(task_type)
    if existing is None or existing.sample_count == 0:
        new_bucket = TaskTypeEMA(
            task_type=task_type,
            sample_count=1,
            token_ema=float(observed_tokens),
            duration_s_ema=float(observed_duration_s),
            last_updated=clock.now(),
        )
    else:
        new_bucket = TaskTypeEMA(
            task_type=task_type,
            sample_count=existing.sample_count + 1,
            token_ema=alpha * observed_tokens + (1.0 - alpha) * existing.token_ema,
            duration_s_ema=(alpha * observed_duration_s + (1.0 - alpha) * existing.duration_s_ema),
            last_updated=clock.now(),
        )

    return EMAFile(
        schema_version=ema.schema_version,
        buckets={**ema.buckets, task_type: new_bucket},
    )


def predict_tokens(
    ema: EMAFile,
    task: Task,
    *,
    settings: EMASettings,
) -> float:
    """Predict the token cost of running ``task``.

    Returns the EMA value if the bucket has at least
    ``prior_warmup_samples`` samples; otherwise blends the configured
    prior with the observed EMA proportional to sample count.
    """
    return _predict(
        ema=ema,
        task=task,
        settings=settings,
        attr="token_ema",
        prior_field="tokens",
    )


def predict_duration_s(
    ema: EMAFile,
    task: Task,
    *,
    settings: EMASettings,
) -> float:
    """Predict the wall-clock duration of running ``task``."""
    return _predict(
        ema=ema,
        task=task,
        settings=settings,
        attr="duration_s_ema",
        prior_field="duration_s",
    )


def _prior_for(settings: EMASettings, task: Task) -> EMAPrior | None:
    by_model = settings.priors.get(task.model)
    if not by_model:
        return None
    return by_model.get(task.effort)


def _predict(
    *,
    ema: EMAFile,
    task: Task,
    settings: EMASettings,
    attr: str,
    prior_field: str,
) -> float:
    """Shared blend logic for token and duration predictions."""
    bucket = ema.buckets.get(task_type_key(task))
    prior = _prior_for(settings, task)
    prior_value: float | None = float(getattr(prior, prior_field)) if prior is not None else None

    if bucket is None or bucket.sample_count == 0:
        # No real samples yet — fall back to prior, or 0 if no prior set.
        return prior_value if prior_value is not None else 0.0

    observed: float = float(getattr(bucket, attr))

    if bucket.sample_count >= settings.prior_warmup_samples:
        # Warm — drop the prior entirely.
        return observed

    if prior_value is None:
        return observed

    # Blend prior and observed in proportion to sample count.
    blend_weight = bucket.sample_count / settings.prior_warmup_samples
    return blend_weight * observed + (1.0 - blend_weight) * prior_value


def list_buckets(ema: EMAFile) -> Iterable[TaskTypeEMA]:
    """Convenience iterator over buckets in deterministic key order."""
    for key in sorted(ema.buckets):
        yield ema.buckets[key]
