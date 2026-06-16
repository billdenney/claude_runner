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
    pid: int | None = Field(default=None, ge=1)
    """OS pid of the ``claude`` subprocess this run spawned.

    Populated by the dispatcher's run-record builder. Distinct from
    :attr:`TaskState.pid` (which is cleared on dispatch finalization):
    the run record is a *historical* artefact and survives finalization,
    so the orchestrator's post-tick reap (:func:`runner.orchestrator
    ._reap_finished`) can re-check ``os.kill(pid, 0)`` AFTER the
    dispatch thread exited and refuse to free the slot when the
    subprocess survived the cap-kill (a TASK_UNINTERRUPTIBLE / D-state
    leak). The supervisor only ever uses this for a read-only liveness
    probe; it never signals a recycled pid because the check is gated
    by the just-exited dispatch thread.

    ``None`` on legacy run records that predate this field, on
    pre-dispatch-hook failures (no subprocess was spawned), and on the
    adopted path's resume of an already-finalized worker."""
    account: str | None = None
    """Which account this attempt was dispatched through. Matches the
    ``name`` of an entry in :class:`AccountSettings`. ``None`` on
    legacy single-account RunRecords (pre-multi-account dispatch);
    set to the account name picked by :func:`runner.account_dispatch
    .choose_account` on multi-account dispatches."""


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
    deliverable_paths: list[Path] = Field(default_factory=list)
    """Paths (relative to ``working_dir``) the task is expected to
    produce. Consulted by the dispatcher's output-evidence gate when
    deciding whether a run that exited cleanly actually produced
    anything externally observable. See ADR-0020. Empty list (default)
    → the output gate falls back to new-commit-on-branch OR
    open-sidecar. Populate this list for tasks that produce only
    side-channel artefacts."""
    additional_dirs: list[Path] = Field(default_factory=list)
    """Extra absolute directory paths the dispatched ``claude`` subprocess
    should be allowed to read/write outside its cwd. Each entry is
    forwarded as a ``--add-dir <path>`` flag. The queue directory itself
    is always passed automatically by the dispatcher (it contains the
    sidecar/, reports/, and per-task source files the agent needs);
    list only *additional* locations here. Missing paths are warned
    about but do not fail dispatch. Empty list (the default) means
    "no extra dirs beyond the always-on queue dir." Backward compatible
    with v2 task YAMLs that pre-date this field."""
    account: str | None = Field(default=None, min_length=1)
    """Pin this task to a specific account name from ``[[accounts]]``.

    When unset (the default), the supervisor's dispatch policy picks
    the lowest-priority available account at dispatch time. Set to an
    account ``name`` when the task must bill to a specific identity
    (e.g. work paper → ``"work"`` account). The dispatcher fails the
    attempt if the named account is unknown; if it exists but has no
    capacity *and* no other slot is free, the policy logs the conflict
    and defers. Backward compatible with v2 task YAMLs that pre-date
    this field."""


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
    ``runner.session.plan_next_spawn``."""
    session_account: str | None = None
    """Account name (matches an entry in :class:`AccountSettings`) under
    which the current ``session_id`` was created. The dispatcher must
    resume the session on this same account — Claude Code sessions are
    namespaced by ``CLAUDE_CONFIG_DIR``, so a session opened under
    ``personal`` is invisible to ``claude`` invoked with the ``work``
    config dir and vice versa.

    Written alongside ``session_id`` on every dispatch that produced a
    session (see ``runner.dispatcher._finalize_state``). ``None`` on
    legacy state YAMLs that pre-date this field; the dispatch policy
    falls back to scanning ``runs[]`` for the most recent attempt's
    ``account`` value when this is unset. New writes always populate
    the explicit field.

    Cleared together with ``session_id`` when an operator runs
    ``queue restart-fresh <task_id>`` to abandon a session whose
    affined account is stuck (throttled / paused / removed)."""
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
    dispatcher_alive_at: datetime | None = None
    """Timestamp of the most recent dispatcher monitor-thread tick.
    Distinct from ``last_heartbeat_at``: this field advances on a fixed
    cadence (``[task_caps].dispatcher_alive_write_interval_s``) whether
    or not the agent emits stream-json events. It proves the dispatcher
    is still pumping the subprocess pipe.

    The per-tick reaper consults both fields. A task whose agent is
    silent for a long stretch (long Bash subprocess, OAuth refresh) but
    whose dispatcher is alive is HEALTHY; only when both fields are stale
    does the reaper fall through to the bounded filesystem-activity
    verification step. ``None`` on legacy state YAMLs that pre-date the
    field — the reaper treats that as "old format" and falls back to
    ``last_heartbeat_at`` alone (the pre-Layer-2 behaviour)."""
    pid: int | None = Field(default=None, ge=1)
    """OS pid of the most recently spawned ``claude`` subprocess for
    this task. Written by the dispatcher right after ``Popen`` (so the
    YAML carries a live pointer to the subprocess while it's running)
    and cleared on dispatch finalization. The supervisor's startup
    silent-orphan reaper (:mod:`supervisor.reconcile_silent`) uses
    this to SIGTERM subprocesses that survived a supervisor restart
    but went silent past ``[task_caps].heartbeat_silence_kill_s``.
    ``None`` on legacy state YAMLs that pre-date the field, on tasks
    that have not yet been dispatched, and on tasks whose most recent
    dispatch has finalized normally."""
    log_path: str | None = None
    """Absolute path to the current attempt's stream-json log file, when
    the dispatcher runs in file-backed mode (``[supervisor].adopt_workers``).
    The worker's stdout is redirected here instead of a supervisor-owned
    pipe, so the stream survives a supervisor restart and a fresh
    supervisor can re-tail it to *adopt* the still-running worker
    (ADR-0025) rather than demoting the task and losing the work.

    Written alongside ``pid`` right after ``Popen`` and cleared on
    dispatch finalization. ``None`` on legacy state YAMLs, on tasks that
    have not been dispatched, on finalized tasks, and whenever adoption
    is disabled (the pipe-backed path records no log file)."""
    stop_reason: str | None = None
    error: str | None = None
    runs: list[RunRecord] = Field(default_factory=list)

    def session_host_account(self) -> str | None:
        """Resolve which account currently hosts the task's session.

        Returns ``None`` when the task has no ``session_id`` — there is
        no session to be affined to.

        When the explicit ``session_account`` field is set (new writes),
        that wins. Otherwise — for state YAMLs that pre-date the field —
        scan ``runs`` newest-first and return the most recent attempt's
        ``account``. Returns ``None`` only when the run list is empty
        or no attempt carried an account (cold start / pre-multi-account).

        The dispatch policy uses this to honor session affinity: a task
        with a session must resume on the host account, even when
        another account has more headroom (see ADR-0024).
        """
        if self.session_id is None:
            return None
        if self.session_account is not None:
            return self.session_account
        for run in reversed(self.runs):
            if run.account is not None:
                return run.account
        return None


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
