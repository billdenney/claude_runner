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
    """

    task_id: str
    account: str
    started_at: datetime
    thread: threading.Thread


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
