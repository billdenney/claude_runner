"""State enum and persisted state dataclass for the supervisor.

The state machine in :mod:`supervisor.state_machine` is a pure function
``step(state, reading, clock) -> (new_state, actions)``. The state
itself is a frozen pydantic model so it round-trips cleanly through
``supervisor.json``. See ADR-0009 for the testability rationale.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from claude_task_runner.queue.schema import CURRENT_SCHEMA_VERSION


class SupervisorState(StrEnum):
    """The high-level state machine vertices.

    Continuous spectrum (FULL/SLOW/STOPPED) of the throttle bands maps
    onto separate states only for telemetry clarity — see ADR-0004 and
    :mod:`runner.concurrency`.
    """

    IDLE = "idle"
    """No pending tasks; supervisor polls usage but takes no action."""

    DISPATCHING = "dispatching"
    """Predicted utilization < full-band threshold; full target concurrency."""

    SLOWING_DOWN = "slowing_down"
    """In the slowdown band; target concurrency reduced linearly."""

    THROTTLED_5H = "throttled_5h"
    """5-hour utilization >= no-dispatch threshold."""

    PAUSED_WEEKLY = "paused_weekly"
    """Weekly utilization >= pause threshold."""

    END_OF_WEEK_PUSH = "end_of_week_push"
    """Weekly cap hit AND reset is imminent; dispatch only short tasks."""

    ERROR_DRIFT = "error_drift"
    """Last poll raised UsageFormatDrift; require N clean polls to recover."""

    STOPPED = "stopped"
    """Operator-issued stop. Manual intervention required to resume."""


class SupervisorSnapshot(BaseModel):
    """Persisted state for ``supervisor.json``.

    Captures everything the supervisor needs to remember across a
    restart: the current state, when it was entered, what the most
    recent usage reading looked like, and what wakeup is scheduled.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = CURRENT_SCHEMA_VERSION

    state: SupervisorState
    """Current state machine vertex."""

    since: datetime
    """When the supervisor entered ``state``. Used for telemetry and
    for the ``ERROR_DRIFT`` clean-poll counter relative to entry."""

    last_5h_util_pct: int = Field(ge=0, le=100, default=0)
    """Most recent 5-hour utilization percentage. Defaults 0 at cold
    start so the supervisor doesn't refuse to dispatch on first tick."""

    last_weekly_util_pct: int = Field(ge=0, le=100, default=0)
    """Most recent weekly (all-models) utilization percentage."""

    last_5h_reset_at: datetime | None = None
    """Most recent parsed 5-hour reset target. ``None`` means we haven't
    yet seen a parseable reset string."""

    last_weekly_reset_at: datetime | None = None
    """Most recent parsed weekly reset target."""

    in_flight_task_ids: list[str] = Field(default_factory=list)
    """Task IDs currently dispatched. Used on supervisor restart so we
    can reattach to live PIDs without killing them."""

    scheduled_wakeup_at: datetime | None = None
    """When the supervisor should wake up next (e.g., a few minutes
    after a window reset). ``None`` means "next regular poll tick"."""

    consecutive_clean_polls: int = Field(ge=0, default=0)
    """Counter used by ERROR_DRIFT recovery. Reset to 0 on entry to
    ERROR_DRIFT and on any drift; incremented on each clean poll. The
    state machine compares against ``[usage].drift_recovery_clean_polls``."""

    last_drift_message: str = ""
    """Most recent ``UsageFormatDrift`` exception message; empty when
    not in (and not recently exiting) ERROR_DRIFT."""
