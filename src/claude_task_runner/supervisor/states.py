"""State enum and persisted state dataclass for the supervisor.

The state machine in :mod:`supervisor.state_machine` is a pure function
``step(state, reading, clock) -> (new_state, actions)``. The state
itself is a frozen pydantic model so it round-trips cleanly through
``supervisor.json``. See ADR-0009 for the testability rationale.

Schema versions
---------------
``SupervisorSnapshot.schema_version`` is independent of the queue's
``schema_version`` (Task / TaskState / RunRecord). v2 was the single-
account snapshot; v3 adds per-account state alongside the legacy
single-account fields; v4 (ADR-0022) drops the ``paused_weekly`` and
``end_of_week_push`` states. The legacy top-level fields remain
populated (mirrored from ``accounts[<active>]`` after each tick).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

SUPERVISOR_SCHEMA_VERSION = 4
"""Supervisor.json schema version.

Bumped from 3 to 4 (ADR-0022) when ``paused_weekly`` and
``end_of_week_push`` were dropped from :class:`SupervisorState`.
The persistence layer rewrites those values to ``idle`` and clears
``scheduled_wakeup_at`` so the next tick reclassifies under the new
trace-following rule."""


class SupervisorState(StrEnum):
    """The high-level state machine vertices.

    Continuous spectrum (FULL/SLOW/STOPPED) of the 5h dispatch_pct
    bands maps onto separate states only for telemetry clarity —
    see ADR-0022 and :mod:`claude_task_runner.throttle.decision`.
    """

    IDLE = "idle"
    """No pending tasks; supervisor polls usage but takes no action."""

    DISPATCHING = "dispatching"
    """Predicted utilization < full-band threshold; full target concurrency."""

    SLOWING_DOWN = "slowing_down"
    """In the slowdown band; target concurrency reduced linearly."""

    THROTTLED_5H = "throttled_5h"
    """5-hour utilization >= the configured no-dispatch threshold.

    Recovery wakeup is scheduled just past the next 5h reset; the
    next clean reading reclassifies."""

    THROTTLED_WEEKLY = "throttled_weekly"
    """Observed weekly utilization is above the trace target at the
    current elapsed fraction of the week. Wakeup is the analytical
    catch-up time (when the curve rises to meet observed),
    clamped to the next 5h reset so the horizon stays readable.

    Distinct from :attr:`THROTTLED_5H` so the operator can tell at a
    glance which window is driving the throttle. Replaces the
    superseded ``PAUSED_WEEKLY`` / ``END_OF_WEEK_PUSH`` pair from
    ADR-0006/0016 — see ADR-0022."""

    ERROR_DRIFT = "error_drift"
    """Last poll raised UsageFormatDrift; require N clean polls to recover."""

    STOPPED = "stopped"
    """Operator-issued stop. Manual intervention required to resume."""


class AccountState(BaseModel):
    """Per-account throttle state for a single Claude account.

    One ``AccountState`` per configured account in
    :attr:`SupervisorSnapshot.accounts`. Shape mirrors the v2 single-
    account ``SupervisorSnapshot`` so existing state-machine code can
    operate on an ``AccountState`` directly without further refactoring.

    The ``paused`` field is operator-controllable via
    ``claude-task-runner account pause/resume`` and gates the account
    out of the dispatch policy without stopping the supervisor.

    The ``last_capture_at`` field is the timestamp of the most recent
    ``claude /usage`` capture for this account; a future multi-account
    usage source will use it to pick the most-overdue account each
    tick. Defaults to ``None`` ("never captured") so cold-start
    snapshots round-trip cleanly.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: SupervisorState
    """Current state machine vertex for this account."""

    since: datetime
    """When this account entered ``state``."""

    last_5h_util_pct: int = Field(ge=0, le=100, default=0)
    last_weekly_util_pct: int = Field(ge=0, le=100, default=0)
    last_5h_reset_at: datetime | None = None
    last_weekly_reset_at: datetime | None = None
    scheduled_wakeup_at: datetime | None = None
    consecutive_clean_polls: int = Field(ge=0, default=0)
    last_drift_message: str = ""

    paused: bool = False
    """When True, the dispatch policy skips this account. Operator-set
    via ``claude-task-runner account pause <name>``."""

    last_capture_at: datetime | None = None
    """Timestamp of the most recent ``/usage`` capture for this
    account. ``None`` means "never captured" (cold start)."""


class InFlightRecord(BaseModel):
    """One in-flight task on the supervisor.

    Records which account dispatched the task so the supervisor can
    enforce per-account concurrency caps in :func:`choose_account` and
    surface per-account in-flight counts in ``account list``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    account: str
    started_at: datetime


class SupervisorSnapshot(BaseModel):
    """Persisted state for ``supervisor.json`` (schema v3).

    v3 introduces per-account state: every configured account has its
    own :class:`AccountState` in ``accounts``, and in-flight tasks
    carry an ``account`` attribution in ``in_flight``. The legacy
    top-level fields (``state``, ``last_5h_util_pct``, ...) are kept
    as a view onto whichever account was last captured — the state
    machine reads them, and the daemon mirrors them from
    ``accounts[<just_captured>]`` after each tick. Multi-account
    callers consult ``accounts[*]`` directly.

    v2 → v3 migration happens in :mod:`supervisor.persistence` at
    load time; the legacy fields are wrapped into a single account
    entry named ``"default"``. One-way migration — re-saving in v3
    cannot be downgraded to v2.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = SUPERVISOR_SCHEMA_VERSION

    state: SupervisorState
    """Current state machine vertex (legacy/global view). For single-
    account configurations this matches ``accounts["default"].state``
    exactly. For multi-account configurations this reflects the most-
    recently-captured account."""

    since: datetime
    """When the supervisor entered ``state`` (global)."""

    last_5h_util_pct: int = Field(ge=0, le=100, default=0)
    last_weekly_util_pct: int = Field(ge=0, le=100, default=0)
    last_5h_reset_at: datetime | None = None
    last_weekly_reset_at: datetime | None = None
    scheduled_wakeup_at: datetime | None = None
    consecutive_clean_polls: int = Field(ge=0, default=0)
    last_drift_message: str = ""

    in_flight_task_ids: list[str] = Field(default_factory=list)
    """Legacy: task IDs currently dispatched (un-attributed). Kept
    for restart reattach and as a quick top-level count; the
    authoritative list (with account attribution) is ``in_flight``."""

    accounts: dict[str, AccountState] = Field(default_factory=dict)
    """Per-account state, keyed by account name (from
    ``settings.accounts[*].name``). Empty only at the moment a fresh
    snapshot is constructed before the daemon populates it; the
    persistence layer's :func:`initial_snapshot` seeds it."""

    in_flight: list[InFlightRecord] = Field(default_factory=list)
    """In-flight tasks with account attribution. Populated by the
    orchestrator at dispatch time. The daemon writes this to the
    snapshot each tick from the live :class:`DispatchSlot` set."""
