"""In-flight dispatch slot tracking.

The orchestrator and the force-dispatch handler share a single
``dict[str, DispatchSlot]`` keyed by ``task_id``. Each slot carries
the live ``threading.Thread`` plus the per-dispatch metadata the
supervisor's snapshot needs (account attribution, start timestamp).

Kept in its own tiny module so both ``runner.orchestrator`` and
``runner.force_dispatch`` can import it without circular references.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime

from claude_task_runner.supervisor.states import InFlightRecord


@dataclass
class DispatchSlot:
    """One in-flight task slot.

    Mutable on purpose: the dispatch thread runs detached, and we
    update e.g. heartbeats / observed status in the slot from the
    orchestrator's tick. Equality is identity-based via Python's
    default dataclass semantics.

    ``subprocess_leak_notified_at`` is set the first time the
    orchestrator's post-tick reap (:func:`runner.orchestrator
    ._reap_finished`) finds the dispatch thread exited but the recorded
    subprocess pid still alive — a TASK_UNINTERRUPTIBLE / D-state leak
    that ``_terminate`` could not finish. Once set, the slot is held
    open (NOT freed) and re-checks happen silently each tick until the
    kernel releases the pid; only the first detection emits an
    operator-visible notification. ``None`` on a healthy slot.
    """

    task_id: str
    account: str
    started_at: datetime
    thread: threading.Thread
    subprocess_leak_notified_at: datetime | None = None


def to_in_flight_records(slots: dict[str, DispatchSlot]) -> list[InFlightRecord]:
    """Snapshot ``slots`` as a list of ``InFlightRecord`` for persistence.

    Order is deterministic (sorted by ``task_id``) so successive
    persisted snapshots produce identical bytes when the slot set
    hasn't changed — keeps diff-watching tools quiet.
    """
    return [
        InFlightRecord(task_id=slot.task_id, account=slot.account, started_at=slot.started_at)
        for slot in sorted(slots.values(), key=lambda s: s.task_id)
    ]
