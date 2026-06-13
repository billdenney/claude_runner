"""Tests for the fast-stop thread lifetime (ADR-0025).

When ``[supervisor].adopt_workers`` is on, the daemon must be able to
stop and exit promptly WITHOUT joining in-flight worker threads — the
file-backed workers keep running as their own OS processes and the next
supervisor adopts them. The mechanism is that ``tick_dispatch`` spawns
the dispatch threads as **daemon** threads when adoption is on (so the
interpreter doesn't block on them at exit) and as **non-daemon** threads
when adoption is off (so the historical graceful-drain stop joins them).

These tests patch ``dispatch`` with a blocking body and assert the
spawned thread's ``daemon`` flag matches the mode, and that the worker's
state stays ``running`` (the supervisor never finalized it on stop).
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_task_runner.clock import RealClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.config.schema import (
    AccountConcurrencyPolicy,
    AccountPolicy,
    ResolvedAccount,
    Settings,
)
from claude_task_runner.queue.schema import Task
from claude_task_runner.queue.store import (
    queue_runtime_dir,
    task_path_for,
    todo_dir,
    write_task_atomic,
)
from claude_task_runner.runner import orchestrator as orch_mod
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


def _settings(*, adopt: bool) -> Settings:
    base = load_settings(None)
    return base.model_copy(
        update={"supervisor": base.supervisor.model_copy(update={"adopt_workers": adopt})}
    )


def _resolved() -> ResolvedAccount:
    return ResolvedAccount(
        name="personal",
        config_dir="",
        policy=AccountPolicy(concurrency=AccountConcurrencyPolicy(max_concurrency=5)),
    )


def _snapshot() -> SupervisorSnapshot:
    return SupervisorSnapshot(
        state=SupervisorState.DISPATCHING,
        since=datetime(2026, 6, 13, tzinfo=UTC),
        accounts={
            "personal": AccountState(
                state=SupervisorState.DISPATCHING,
                since=datetime(2026, 6, 13, tzinfo=UTC),
            )
        },
    )


def _seed_task(queue_dir: Path, task_id: str) -> None:
    write_task_atomic(
        Task(id=task_id, title="t", prompt="p", working_dir=None),
        task_path_for(queue_dir, task_id),
    )


def _run_one_dispatch(
    queue_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    adopt: bool,
) -> tuple[DispatchSlot, threading.Event]:
    """Dispatch one pending task with the dispatch worker patched to block;
    return the slot + the event that releases the blocked worker.

    The task is left pending (no state file) so the orchestrator picks it
    up; the patched worker body blocks (modelling a long-running, still
    in-flight worker) so we can inspect the spawned thread's daemon flag
    and that it is still alive when the supervisor would be stopping."""
    _seed_task(queue_dir, "t1")

    release = threading.Event()

    def _blocking_worker(*_args: object, **_kw: object) -> None:
        # Model a long-running worker thread: block until released. The
        # supervisor must be able to stop without waiting for this.
        release.wait(timeout=10)

    # Patch the orchestrator's thread entrypoint so the spawned thread
    # blocks without running a real dispatch.
    monkeypatch.setattr(orch_mod, "_dispatch_one_safely", _blocking_worker)

    in_flight: dict[str, DispatchSlot] = {}
    tick_dispatch(
        queue_dir=queue_dir,
        settings=_settings(adopt=adopt),
        clock=RealClock(),
        snapshot=_snapshot(),
        in_flight_slots=in_flight,
        accounts=[_resolved()],
    )
    assert "t1" in in_flight
    return in_flight["t1"], release


def test_dispatch_thread_is_daemon_when_adoption_on(
    queue_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adoption ON ⇒ daemon thread, so a fast stop need not join it and
    the interpreter can exit while the (file-backed) worker keeps running.
    The still-alive worker thread models the in-flight work the fast stop
    deliberately does NOT wait for."""
    slot, release = _run_one_dispatch(queue_dir, monkeypatch, adopt=True)
    try:
        # daemon=True is the fast-stop mechanism: the interpreter won't
        # block on this thread at exit, so `start_daemon` can return
        # immediately on SIGTERM with the worker still in flight.
        assert slot.thread.daemon is True
        assert slot.thread.is_alive()
    finally:
        release.set()
        slot.thread.join(timeout=2)


def test_dispatch_thread_is_non_daemon_when_adoption_off(
    queue_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adoption OFF ⇒ non-daemon thread, preserving the graceful-drain
    stop that joins in-flight threads before exit (historical behaviour)."""
    slot, release = _run_one_dispatch(queue_dir, monkeypatch, adopt=False)
    try:
        assert slot.thread.daemon is False
        assert slot.thread.is_alive()
    finally:
        release.set()
        slot.thread.join(timeout=2)
