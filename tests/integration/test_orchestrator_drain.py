"""Integration test: drain-mode reap-to-exit progression (PR 11).

Covers the audit's COVERAGE gap on ``tick_dispatch(draining=True)``
(orchestrator.py): in drain mode the function only reaps finished
threads and refreshes the snapshot, returning early without
dispatching. The daemon's loop exit condition is
``drain_flag["draining"] and not in_flight_slots`` (see
``supervisor.daemon``).

Where ``tests/unit/test_drain_mode.py`` pins each single-call
behaviour in isolation, this test drives the *multi-tick* sequence the
daemon actually walks: a task is still running on the first drain tick
(so the loop must NOT exit), the thread finishes between ticks, and the
final drain tick reaps it — emptying ``in_flight_slots`` so the loop's
exit condition flips to True. No real ``claude`` subprocess; the
dispatched work is a controllable in-process thread.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from claude_task_runner.clock import RealClock
from claude_task_runner.config.schema import (
    AccountConcurrencyPolicy,
    AccountPolicy,
    AccountSettings,
    ResolvedAccount,
)
from claude_task_runner.queue.store import queue_runtime_dir, todo_dir
from claude_task_runner.runner.in_flight import DispatchSlot
from claude_task_runner.runner.orchestrator import tick_dispatch
from claude_task_runner.supervisor.states import (
    AccountState,
    SupervisorSnapshot,
    SupervisorState,
)


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


def _settings() -> Any:
    return SimpleNamespace(
        concurrency=SimpleNamespace(initial_concurrency=5, max_concurrency=5),
        task_caps=SimpleNamespace(),
        session=SimpleNamespace(),
        hooks=SimpleNamespace(),
        failure_classifier=None,
        dispatch=SimpleNamespace(auto_detect_paths_in_prompt=False),
        claude=SimpleNamespace(config_dir=""),
        accounts=[AccountSettings(name="personal", config_dir="")],
    )


def _resolved() -> ResolvedAccount:
    return ResolvedAccount(
        name="personal",
        config_dir="",
        policy=AccountPolicy(concurrency=AccountConcurrencyPolicy(max_concurrency=5)),
    )


def _dispatchable_snapshot() -> SupervisorSnapshot:
    return SupervisorSnapshot(
        state=SupervisorState.DISPATCHING,
        since=datetime(2026, 5, 22, tzinfo=UTC),
        accounts={
            "personal": AccountState(
                state=SupervisorState.DISPATCHING,
                since=datetime(2026, 5, 22, tzinfo=UTC),
            ),
        },
    )


def _slot(task_id: str, thread: threading.Thread) -> DispatchSlot:
    return DispatchSlot(
        task_id=task_id,
        account="personal",
        started_at=datetime(2026, 5, 22, tzinfo=UTC),
        thread=thread,
    )


def _loop_should_exit(draining: bool, in_flight_slots: dict[str, DispatchSlot]) -> bool:
    """Mirror of the daemon's drain-complete exit condition.

    The daemon exits when ``drain_flag["draining"] and not
    in_flight_slots`` (``supervisor.daemon``). Replicated here so the
    test asserts on the exact predicate the loop uses, not a proxy.
    """
    return draining and not in_flight_slots


def test_drain_reaps_to_exit_across_ticks(queue_dir: Path) -> None:
    """A running task blocks exit on tick 1; the final reap empties the
    slot map so the loop can exit on tick 2."""
    settings = _settings()
    snap = _dispatchable_snapshot()

    # One in-flight task whose work we control: it stays alive until we
    # set the event, modelling a dispatch thread still finishing during
    # drain.
    stop_event = threading.Event()
    worker = threading.Thread(target=lambda: stop_event.wait(timeout=10), daemon=True)
    worker.start()
    in_flight: dict[str, DispatchSlot] = {"t1": _slot("t1", worker)}

    # The operator drains; the supervisor keeps ticking.
    draining = True

    try:
        # --- Tick 1: worker still running -------------------------------
        snap = tick_dispatch(
            queue_dir=queue_dir,
            settings=settings,
            clock=RealClock(),
            snapshot=snap,
            in_flight_slots=in_flight,
            accounts=[_resolved()],
            draining=draining,
        )
        # Still in flight → snapshot mirrors it, and the loop must NOT exit.
        assert "t1" in in_flight
        assert [r.task_id for r in snap.in_flight] == ["t1"]
        assert snap.in_flight[0].account == "personal"
        assert _loop_should_exit(draining, in_flight) is False

        # --- Between ticks: the dispatched task completes ---------------
        stop_event.set()
        worker.join(timeout=2)
        assert not worker.is_alive()

        # --- Tick 2 (final reap): the finished thread is removed --------
        snap = tick_dispatch(
            queue_dir=queue_dir,
            settings=settings,
            clock=RealClock(),
            snapshot=snap,
            in_flight_slots=in_flight,
            accounts=[_resolved()],
            draining=draining,
        )
        # Slot map drained, snapshot reflects the empty set, and the
        # loop's exit predicate is now True.
        assert in_flight == {}
        assert snap.in_flight == []
        assert snap.in_flight_task_ids == []
        assert _loop_should_exit(draining, in_flight) is True
    finally:
        stop_event.set()
        worker.join(timeout=2)
