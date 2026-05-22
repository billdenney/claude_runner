"""Tests for the graceful-drain restart pattern (PR 11).

Drain mode is the supervisor's "stop accepting new work, finish what
you're running, then exit" path. Triggered by SIGUSR1 (or
``claude-task-runner supervisor drain``). Once every dispatched
thread has finished, the daemon loop exits cleanly; a fresh
supervisor started afterwards re-reads ``supervisor.json`` and
picks up where the old one left off — without re-dispatching
anything that completed during drain.

Tested behaviour:

* ``tick_dispatch(draining=True)`` reaps finished threads and refreshes
  ``snapshot.in_flight`` but does NOT dispatch new work.
* The reap still happens (so finished threads are removed from
  ``in_flight_slots``).
* In drain mode, even an eligible account + free capacity + pending
  task results in no new dispatch.
* ``snapshot.in_flight`` mirrors the still-running set after drain
  reap, so the persisted snapshot is accurate for a fresh supervisor's
  observability.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from claude_task_runner.clock import RealClock
from claude_task_runner.config.schema import (
    AccountConcurrencyPolicy,
    AccountPolicy,
    AccountSettings,
    ResolvedAccount,
)
from claude_task_runner.queue.schema import Task
from claude_task_runner.queue.store import (
    queue_runtime_dir,
    task_path_for,
    todo_dir,
    write_task_atomic,
)
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


def _make_task(qd: Path, task_id: str) -> Task:
    task = Task.model_validate(
        {"id": task_id, "title": f"Task {task_id}", "prompt": "do the thing"}
    )
    write_task_atomic(task, task_path_for(qd, task_id))
    return task


def _settings(names: list[str]) -> Any:
    return SimpleNamespace(
        concurrency=SimpleNamespace(initial_concurrency=5, max_concurrency=5),
        task_caps=SimpleNamespace(),
        session=SimpleNamespace(),
        hooks=SimpleNamespace(),
        failure_classifier=None,
        dispatch=SimpleNamespace(auto_detect_paths_in_prompt=False),
        claude=SimpleNamespace(config_dir=""),
        accounts=[AccountSettings(name=n, config_dir="") for n in names],
    )


def _resolved(name: str, cap: int = 5) -> ResolvedAccount:
    return ResolvedAccount(
        name=name,
        config_dir="",
        policy=AccountPolicy(concurrency=AccountConcurrencyPolicy(max_concurrency=cap)),
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


# ---------------------------------------------------------------------------
# Core behaviour
# ---------------------------------------------------------------------------


def test_draining_skips_new_dispatch_even_when_capacity_free(queue_dir: Path) -> None:
    """Free capacity + pending task + dispatchable account → still no dispatch."""
    _make_task(queue_dir, "t1")
    in_flight: dict[str, DispatchSlot] = {}

    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
        return_value=None,
    ) as mock_dispatch:
        new_snap = tick_dispatch(
            queue_dir=queue_dir,
            settings=_settings(["personal"]),
            clock=RealClock(),
            snapshot=_dispatchable_snapshot(),
            in_flight_slots=in_flight,
            accounts=[_resolved("personal")],
            draining=True,
        )

    assert in_flight == {}
    mock_dispatch.assert_not_called()
    # Snapshot.in_flight stays empty (no dispatch happened) — but the
    # function still returned a refreshed copy.
    assert new_snap.in_flight == []
    assert new_snap.in_flight_task_ids == []


def test_draining_still_reaps_finished_threads(queue_dir: Path) -> None:
    """A finished thread present at drain entry is removed from in_flight."""
    # Synthesise a finished thread (run a no-op then join).
    finished = threading.Thread(target=lambda: None, daemon=True)
    finished.start()
    finished.join(timeout=1)
    assert not finished.is_alive()

    in_flight: dict[str, DispatchSlot] = {"t1": _slot("t1", finished)}

    new_snap = tick_dispatch(
        queue_dir=queue_dir,
        settings=_settings(["personal"]),
        clock=RealClock(),
        snapshot=_dispatchable_snapshot(),
        in_flight_slots=in_flight,
        accounts=[_resolved("personal")],
        draining=True,
    )

    # Reap removed it from the slot map.
    assert "t1" not in in_flight
    # And the refreshed snapshot in_flight reflects that.
    assert new_snap.in_flight == []
    assert new_snap.in_flight_task_ids == []


def test_draining_preserves_still_running_threads_in_snapshot(queue_dir: Path) -> None:
    """A still-alive thread stays in the slot map and the refreshed snapshot."""
    stop_event = threading.Event()
    alive = threading.Thread(target=lambda: stop_event.wait(timeout=10), daemon=True)
    alive.start()
    in_flight: dict[str, DispatchSlot] = {"t1": _slot("t1", alive)}

    try:
        new_snap = tick_dispatch(
            queue_dir=queue_dir,
            settings=_settings(["personal"]),
            clock=RealClock(),
            snapshot=_dispatchable_snapshot(),
            in_flight_slots=in_flight,
            accounts=[_resolved("personal")],
            draining=True,
        )

        assert "t1" in in_flight
        assert [r.task_id for r in new_snap.in_flight] == ["t1"]
        assert new_snap.in_flight[0].account == "personal"
    finally:
        stop_event.set()
        alive.join(timeout=2)


def test_draining_with_no_in_flight_returns_empty_in_flight(queue_dir: Path) -> None:
    """Daemon's drain-complete check sees ``not in_flight_slots`` and exits."""
    in_flight: dict[str, DispatchSlot] = {}
    new_snap = tick_dispatch(
        queue_dir=queue_dir,
        settings=_settings(["personal"]),
        clock=RealClock(),
        snapshot=_dispatchable_snapshot(),
        in_flight_slots=in_flight,
        accounts=[_resolved("personal")],
        draining=True,
    )
    assert in_flight == {}
    assert new_snap.in_flight == []
    # That last assertion is what the daemon uses to decide to exit.
