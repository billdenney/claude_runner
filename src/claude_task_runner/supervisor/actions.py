"""Action types emitted by the state-machine step function.

The state machine is pure: it returns ``(new_snapshot, [actions])``
rather than performing I/O. The daemon executes actions side-effectfully.

Each action is a frozen dataclass so they're cheap to construct, easy
to log, and trivially diffable in state-machine tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class _ActionBase:
    """Marker base class — instantiate concrete subclasses."""


@dataclass(frozen=True)
class DispatchTask(_ActionBase):
    """Tell the runner to dispatch the next pending task."""

    task_id: str


@dataclass(frozen=True)
class MonitorInFlight(_ActionBase):
    """Inspect in-flight subprocess(es) — apply caps + heartbeat checks."""


@dataclass(frozen=True)
class ScheduleWakeupAt(_ActionBase):
    """Persist a wakeup time so the daemon (or watchdog) can sleep until then."""

    when: datetime


@dataclass(frozen=True)
class Notify(_ActionBase):
    """Emit a notification through configured channels."""

    level: str
    """``"info"``, ``"warn"``, ``"error"``, ``"critical"``."""

    message: str


@dataclass(frozen=True)
class EmitEvent(_ActionBase):
    """Append a structured event to ``events.ndjson``."""

    event_type: str
    """Short slug, e.g. ``"state_transition"`` or ``"drift_detected"``."""

    payload: dict[str, object]


@dataclass(frozen=True)
class StopDispatch(_ActionBase):
    """Refuse to start any new dispatches this tick. Diagnostic-only —
    the actual dispatch decisions are gated by ``target_concurrency`` on
    the :class:`throttle.decision.Decision`. Useful for telemetry."""


# Public union for type-narrowed iteration.
Action = DispatchTask | MonitorInFlight | ScheduleWakeupAt | Notify | EmitEvent | StopDispatch
