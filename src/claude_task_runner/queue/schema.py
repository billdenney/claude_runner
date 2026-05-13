"""Pydantic models for queue v2 schema.

Every persisted file carries ``schema_version: 2`` so future migrations have a
known starting point. See ADR-0007 (no migration of v1 data) and ADR-0014
(every cutoff is a setting).

Three primary entities:

* ``Task`` — the input description, written to ``<queue>/todo/<id>.yaml`` by
  the operator (or by ``/runner-add-task``). Static across attempts.
* ``TaskState`` — the runtime status, written to
  ``<queue>/.claude_task_runner/state/<id>.yaml`` by the runner. Mutates as
  attempts run.
* ``RunRecord`` — one entry per attempt within a TaskState's ``runs`` list,
  capturing tokens, cost, duration, errors.

Plus ``SidecarRequest`` / ``SidecarResponse`` / ``SidecarQuestion`` for the
stop-and-ask protocol; see ``queue/sidecar.py`` for the handler.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

CURRENT_SCHEMA_VERSION = 2

TaskStatus = Literal[
    "pending",
    "running",
    "awaiting_sidecar",
    "possibly_hung",
    "completed",
    "failed",
    "failed_circuit_breaker",
    "weekly_paused",
]
"""Lifecycle states a task can occupy in its state YAML."""

Effort = Annotated[str, Field(min_length=1)]
"""Free-form string; validated against the per-model accepted set in
``runner.effort_levels`` at task-load time. See ADR-0010."""

Priority = Literal["low", "normal", "high"]


class _StrictBase(BaseModel):
    """Forbid unknown keys to surface schema drift early."""

    model_config = ConfigDict(extra="forbid")


class TokenUsage(_StrictBase):
    """One run's token counts, summed from claude stream-json events.

    Cost is tracked separately on :class:`RunRecord` because the runner
    reports cost only on the final ``result`` event, not per-message.
    """

    input_tokens: int = Field(ge=0, default=0)
    output_tokens: int = Field(ge=0, default=0)
    cache_read_tokens: int = Field(ge=0, default=0)
    cache_creation_tokens: int = Field(ge=0, default=0)

    @property
    def total_tokens(self) -> int:
        """Sum used by the EMA / throttle predictor."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )


class RunRecord(_StrictBase):
    """A single dispatch attempt's outcome."""

    schema_version: int = CURRENT_SCHEMA_VERSION
    attempt: int = Field(ge=1)
    started_at: datetime
    finished_at: datetime
    stop_reason: str
    error: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float = Field(ge=0.0, default=0.0)
    duration_s: float = Field(ge=0)
    resumed_from_session: str | None = None
    """If this run was started via ``claude --resume <id>``, the session id
    we resumed from. Distinct from ``TaskState.session_id`` which is the
    session id this run *produced* (may equal resumed_from_session if
    resume succeeded; differs if we fell through to fresh)."""
    killed_by_cap: Literal["tokens", "duration"] | None = None
    """Set when the run was SIGTERM'd by ``runner.caps`` because a
    per-task safety cap was exceeded."""


class Task(_StrictBase):
    """A queued task as authored by the operator.

    The on-disk YAML adds a ``schema_version: 2`` key for forward
    compatibility. Tasks are loaded once at dispatch time; the schema is
    validated then so authoring errors surface fast.
    """

    schema_version: int = CURRENT_SCHEMA_VERSION
    id: str = Field(min_length=1)
    title: str
    prompt: str
    working_dir: Path | None = None
    model: str = "claude-opus-4-7"
    effort: Effort = "medium"
    priority: Priority = "normal"
    depends_on: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    """Free-form labels used for cohort reporting and EMA grouping
    overrides."""
    weekly_critical: bool = False
    """Dispatched first within a window; ensures completion before the
    week closes."""
    weekly_deferrable: bool = False
    """OK to skip until next weekly window; deprioritized in EOW push."""
    force_dispatch_in_eow: bool = False
    """Override the EOW-runtime safety guard for this specific task."""
    max_tokens_override: int | None = Field(default=None, ge=1)
    """Per-task override of ``[task_caps].max_tokens_per_task``."""
    max_duration_s_override: float | None = Field(default=None, gt=0)
    """Per-task override of ``[task_caps].max_duration_s_per_task``."""


class TaskState(_StrictBase):
    """Runtime state for one task, persisted under ``state/<id>.yaml``.

    Updated on every state transition by the runner. Atomic writes via
    temp-file-rename guard against torn reads (see ``queue.store``).
    """

    schema_version: int = CURRENT_SCHEMA_VERSION
    task_id: str = Field(min_length=1)
    status: TaskStatus = "pending"
    session_id: str | None = None
    """Most recent claude session id this task has run under. Used by
    ``runner.session.resume_or_fresh``."""
    attempts: int = Field(ge=0, default=0)
    resume_attempts: int = Field(ge=0, default=0)
    """Counter capped by ``[session].max_resume_attempts``; beyond cap,
    only fresh restarts are attempted."""
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    """Timestamp of the most recent stream-json event observed.
    ``runner.heartbeat`` flags ``possibly_hung`` when this falls behind
    by more than ``[task_caps].heartbeat_silence_alert_s``."""
    stop_reason: str | None = None
    error: str | None = None
    runs: list[RunRecord] = Field(default_factory=list)


class SidecarOption(_StrictBase):
    """One choice within a sidecar question.

    Maps 1:1 to an ``AskUserQuestion`` option in the
    ``/runner-answer-sidecar`` skill so operators answer with a single
    click rather than typing.
    """

    value: str
    label: str
    description: str | None = None


class SidecarQuestion(_StrictBase):
    """A single question within a sidecar request."""

    id: str
    prompt: str
    options: list[SidecarOption] = Field(default_factory=list)
    multi_select: bool = False
    recommended: str | None = None
    """If set, must be the ``value`` of one of ``options``. The skill
    surfaces this option first with a "(Recommended)" suffix."""
    allow_free_text: bool = False
    """Whether the operator can supply arbitrary text. Defaults False so
    the answer flow stays click-only."""


class SidecarRequest(_StrictBase):
    """The runner's question to the operator, awaiting a response.

    Stored at ``<queue>/.claude_task_runner/sidecar/<task_id>/request-NNN.json``.
    Sequence numbering allows a single task to ask multiple questions
    across its lifetime; each request gets a fresh sequence number.
    """

    schema_version: int = CURRENT_SCHEMA_VERSION
    task_id: str
    sequence: int = Field(ge=1)
    created_at: datetime
    summary: str
    context: str
    questions: list[SidecarQuestion]
    state: Literal["pending", "answered"] = "pending"


class SidecarAnswer(_StrictBase):
    """One operator answer matching one question's ``id``."""

    id: str
    value: str | list[str]
    """Single string for single-select; list for ``multi_select=true``."""


class SidecarResponse(_StrictBase):
    """The operator's answer; mirror of ``SidecarRequest``.

    Stored at ``<queue>/.claude_task_runner/sidecar/<task_id>/response-NNN.json``.
    The ``state="answered"`` value indicates the runner can resume the
    task with these inputs.
    """

    schema_version: int = CURRENT_SCHEMA_VERSION
    task_id: str
    sequence: int = Field(ge=1)
    responded_at: datetime
    state: Literal["answered"] = "answered"
    answers: list[SidecarAnswer]
    notes: str = ""
    """Optional free-text. Defaults empty so click-only answering is
    the path of least resistance."""
