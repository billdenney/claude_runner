"""Tests for runner/orchestrator.py helper functions and `tick_dispatch`.

The helpers (``_target_concurrency``, ``_has_any_completed``,
``_completed_task_ids``, ``_reap_finished``, ``tick_dispatch``,
``_dispatch_one_safely``) all operate against the filesystem and an
in-memory thread dict — fully testable without external services as
long as we mock ``dispatcher_mod.dispatch``.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from claude_task_runner.clock import RealClock
from claude_task_runner.queue.schema import Task, TaskState
from claude_task_runner.queue.store import (
    queue_runtime_dir,
    state_path_for,
    task_path_for,
    todo_dir,
    write_state_atomic,
    write_task_atomic,
)
from claude_task_runner.runner.in_flight import DispatchSlot
from claude_task_runner.runner.orchestrator import (
    _completed_task_ids,
    _dispatch_one_safely,
    _has_any_completed,
    _reap_finished,
    _target_concurrency,
    tick_dispatch,
)
from claude_task_runner.supervisor.states import SupervisorSnapshot, SupervisorState


def _slot(task_id: str, thread: threading.Thread, account: str = "default") -> DispatchSlot:
    return DispatchSlot(
        task_id=task_id,
        account=account,
        started_at=datetime(2026, 5, 21, tzinfo=UTC),
        thread=thread,
    )


def _resolved(cap: int = 1, name: str = "default") -> list:
    """Build a single-account ResolvedAccount list with a custom cap.

    Use to override the per-account default ``max_concurrency=1`` in
    tick_dispatch tests that want to dispatch more than one task per
    tick under the synthesised "default" account.
    """
    from claude_task_runner.config.schema import (
        AccountConcurrencyPolicy,
        AccountPolicy,
        ResolvedAccount,
    )

    return [
        ResolvedAccount(
            name=name,
            config_dir="",
            policy=AccountPolicy(
                concurrency=AccountConcurrencyPolicy(max_concurrency=cap),
            ),
        ),
    ]


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


def _make_task(qd: Path, task_id: str, **overrides: Any) -> Task:
    payload = {"id": task_id, "title": f"Task {task_id}", "prompt": "do the thing"}
    payload.update(overrides)
    task = Task.model_validate(payload)
    write_task_atomic(task, task_path_for(qd, task_id))
    return task


def _seed_state(qd: Path, task_id: str, status: str, **kw: Any) -> TaskState:
    state = TaskState(task_id=task_id, status=status, **kw)
    write_state_atomic(state, state_path_for(qd, task_id))
    return state


def _make_settings(*, initial: int = 1, max_c: int = 5):
    """Minimal Settings-shaped object that satisfies what the orchestrator
    reads (concurrency, accounts via resolve_accounts, plus dispatch-time
    stubs). Using a simple namespace is sufficient since the orchestrator
    never pydantic-validates the object."""
    from types import SimpleNamespace

    from claude_task_runner.config.schema import AccountSettings

    return SimpleNamespace(
        concurrency=SimpleNamespace(
            initial_concurrency=initial,
            max_concurrency=max_c,
        ),
        # Stubs for fields tick_dispatch never reads directly:
        task_caps=SimpleNamespace(),
        session=SimpleNamespace(),
        hooks=SimpleNamespace(),
        failure_classifier=None,
        dispatch=SimpleNamespace(auto_detect_paths_in_prompt=False),
        claude=SimpleNamespace(config_dir=""),
        accounts=[AccountSettings(name="default", config_dir="")],
    )


def _make_snapshot(state: SupervisorState) -> SupervisorSnapshot:
    """Snapshot seeded with one "default" account mirroring the top-level state.

    PR 9 introduced per-account gating in tick_dispatch; mirroring the
    requested state to ``accounts.default.state`` makes the fixture
    behave like a real daemon write (which copies top-level <->
    accounts[X] for the most-recently-captured account)."""
    from claude_task_runner.supervisor.states import AccountState

    return SupervisorSnapshot.model_validate(
        {
            "state": state,
            "since": datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC),
            "accounts": {
                "default": AccountState(
                    state=state,
                    since=datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC),
                ),
            },
        }
    )


# ---------------------------------------------------------------------------
# _has_any_completed
# ---------------------------------------------------------------------------


def test_has_any_completed_empty(queue_dir: Path) -> None:
    assert _has_any_completed(queue_dir) is False


def test_has_any_completed_only_pending(queue_dir: Path) -> None:
    _seed_state(queue_dir, "t1", "pending")
    _seed_state(queue_dir, "t2", "failed")
    _seed_state(queue_dir, "t3", "running")
    assert _has_any_completed(queue_dir) is False


def test_has_any_completed_true_when_one_completed(queue_dir: Path) -> None:
    _seed_state(queue_dir, "t1", "pending")
    _seed_state(queue_dir, "t2", "completed")
    assert _has_any_completed(queue_dir) is True


def test_has_any_completed_skips_unparseable(queue_dir: Path) -> None:
    """A malformed state file is silently skipped — the function must
    still answer the question about the OTHER state files."""
    bad = queue_dir / ".claude_task_runner" / "state" / "bad.yaml"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("not yaml: ][", encoding="utf-8")
    _seed_state(queue_dir, "good", "completed")
    assert _has_any_completed(queue_dir) is True


# ---------------------------------------------------------------------------
# _completed_task_ids
# ---------------------------------------------------------------------------


def test_completed_task_ids_empty(queue_dir: Path) -> None:
    assert _completed_task_ids(queue_dir) == set()


def test_completed_task_ids_collects_only_completed(queue_dir: Path) -> None:
    _seed_state(queue_dir, "t1", "completed")
    _seed_state(queue_dir, "t2", "completed")
    _seed_state(queue_dir, "t3", "pending")
    _seed_state(queue_dir, "t4", "failed")
    assert _completed_task_ids(queue_dir) == {"t1", "t2"}


def test_completed_task_ids_skips_unparseable(queue_dir: Path) -> None:
    bad = queue_dir / ".claude_task_runner" / "state" / "bad.yaml"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("not yaml: ][", encoding="utf-8")
    _seed_state(queue_dir, "good", "completed")
    assert _completed_task_ids(queue_dir) == {"good"}


# ---------------------------------------------------------------------------
# _target_concurrency
# ---------------------------------------------------------------------------


def test_target_concurrency_uses_initial_before_first_completion(queue_dir: Path) -> None:
    """No completed tasks → use initial_concurrency."""
    settings = _make_settings(initial=2, max_c=5)
    snap = _make_snapshot(SupervisorState.DISPATCHING)
    assert _target_concurrency(queue_dir, settings, snap) == 2


def test_target_concurrency_uses_max_after_first_completion(queue_dir: Path) -> None:
    settings = _make_settings(initial=2, max_c=5)
    snap = _make_snapshot(SupervisorState.DISPATCHING)
    _seed_state(queue_dir, "warmup", "completed")
    assert _target_concurrency(queue_dir, settings, snap) == 5


def test_target_concurrency_halved_in_slowing_down(queue_dir: Path) -> None:
    settings = _make_settings(initial=2, max_c=5)
    snap = _make_snapshot(SupervisorState.SLOWING_DOWN)
    _seed_state(queue_dir, "warmup", "completed")
    # max=5 halved => 2
    assert _target_concurrency(queue_dir, settings, snap) == 2


def test_target_concurrency_floored_at_one(queue_dir: Path) -> None:
    """Even with max=1 and slow-down halving, we never return 0."""
    settings = _make_settings(initial=1, max_c=1)
    snap = _make_snapshot(SupervisorState.SLOWING_DOWN)
    _seed_state(queue_dir, "warmup", "completed")
    assert _target_concurrency(queue_dir, settings, snap) == 1


def test_target_concurrency_clamps_zero_or_negative_to_one(queue_dir: Path) -> None:
    """A misconfigured settings with max=0 still returns 1 (the floor)."""
    settings = _make_settings(initial=0, max_c=0)
    snap = _make_snapshot(SupervisorState.DISPATCHING)
    assert _target_concurrency(queue_dir, settings, snap) == 1


# ---------------------------------------------------------------------------
# _reap_finished
# ---------------------------------------------------------------------------


def test_reap_finished_empty_dict() -> None:
    d: dict[str, DispatchSlot] = {}
    _reap_finished(d)
    assert d == {}


def test_reap_finished_removes_dead_threads_only() -> None:
    """A finished (not is_alive) thread is removed; an alive thread stays."""
    done_event = threading.Event()
    keep_event = threading.Event()

    def quick_done():
        done_event.set()
        # exit immediately

    def long_running():
        keep_event.wait(timeout=5)

    quick = threading.Thread(target=quick_done, daemon=True)
    keep = threading.Thread(target=long_running, daemon=True)
    quick.start()
    keep.start()
    # Wait for quick to exit
    done_event.wait(timeout=2)
    quick.join(timeout=2)
    assert not quick.is_alive()
    assert keep.is_alive()

    d = {"quick": _slot("quick", quick), "keep": _slot("keep", keep)}
    _reap_finished(d)
    assert "quick" not in d
    assert "keep" in d

    # Cleanup.
    keep_event.set()
    keep.join(timeout=2)


# ---------------------------------------------------------------------------
# tick_dispatch
# ---------------------------------------------------------------------------


def test_tick_dispatch_no_op_in_idle_state(queue_dir: Path) -> None:
    settings = _make_settings()
    snap = _make_snapshot(SupervisorState.IDLE)
    in_flight: dict[str, DispatchSlot] = {}
    # No tasks; even if there were, IDLE state means no dispatch.
    tick_dispatch(
        queue_dir=queue_dir,
        settings=settings,
        clock=RealClock(),
        snapshot=snap,
        in_flight_slots=in_flight,
    )
    assert in_flight == {}


def test_tick_dispatch_no_op_in_throttled_state(queue_dir: Path) -> None:
    settings = _make_settings()
    snap = _make_snapshot(SupervisorState.THROTTLED_5H)
    _make_task(queue_dir, "t1")
    in_flight: dict[str, DispatchSlot] = {}
    with patch("claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch") as mock_dispatch:
        tick_dispatch(
            queue_dir=queue_dir,
            settings=settings,
            clock=RealClock(),
            snapshot=snap,
            in_flight_slots=in_flight,
        )
    assert in_flight == {}
    mock_dispatch.assert_not_called()


def test_tick_dispatch_dispatches_in_dispatching_state(queue_dir: Path) -> None:
    """DISPATCHING state with pending tasks and a free slot must spawn."""
    settings = _make_settings(initial=2, max_c=2)
    snap = _make_snapshot(SupervisorState.DISPATCHING)
    _make_task(queue_dir, "t1")
    _make_task(queue_dir, "t2")
    _make_task(queue_dir, "t3")  # third — beyond target=2; not picked
    in_flight: dict[str, DispatchSlot] = {}

    # Make dispatcher.dispatch return immediately so threads exit fast.
    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
        return_value=None,
    ):
        tick_dispatch(
            queue_dir=queue_dir,
            settings=settings,
            clock=RealClock(),
            snapshot=snap,
            in_flight_slots=in_flight,
            accounts=_resolved(cap=2),
        )
        # Threads were spawned; t1 and t2 were picked (alphabetical sort).
        assert set(in_flight.keys()) == {"t1", "t2"}
        # Wait for them to finish so we can clean up.
        for slot in list(in_flight.values()):
            slot.thread.join(timeout=2)


def test_tick_dispatch_respects_in_flight_capacity(queue_dir: Path) -> None:
    """If in_flight already equals target, no new dispatches."""
    settings = _make_settings(initial=1, max_c=1)
    snap = _make_snapshot(SupervisorState.DISPATCHING)
    _make_task(queue_dir, "t1")

    # Pretend we already have one thread in flight.
    stop_evt = threading.Event()
    busy_thread = threading.Thread(target=lambda: stop_evt.wait(timeout=5), daemon=True)
    busy_thread.start()
    in_flight = {"already-running": _slot("already-running", busy_thread)}

    with patch("claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch") as mock_dispatch:
        tick_dispatch(
            queue_dir=queue_dir,
            settings=settings,
            clock=RealClock(),
            snapshot=snap,
            in_flight_slots=in_flight,
        )
    mock_dispatch.assert_not_called()
    assert set(in_flight.keys()) == {"already-running"}

    stop_evt.set()
    busy_thread.join(timeout=2)


def test_tick_dispatch_reaps_before_evaluating_capacity(queue_dir: Path) -> None:
    """A dead thread in in_flight is reaped before computing free slots."""
    settings = _make_settings(initial=1, max_c=1)
    snap = _make_snapshot(SupervisorState.DISPATCHING)
    _make_task(queue_dir, "t1")

    # Pre-finished thread in the in_flight dict (simulates a task that
    # completed between ticks).
    finished_thread = threading.Thread(target=lambda: None, daemon=True)
    finished_thread.start()
    finished_thread.join()  # explicitly wait for it to die
    assert not finished_thread.is_alive()
    in_flight = {"already-finished": _slot("already-finished", finished_thread)}

    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
        return_value=None,
    ):
        tick_dispatch(
            queue_dir=queue_dir,
            settings=settings,
            clock=RealClock(),
            snapshot=snap,
            in_flight_slots=in_flight,
        )
        # already-finished was reaped; t1 was dispatched.
        assert "already-finished" not in in_flight
        assert "t1" in in_flight
        for slot in list(in_flight.values()):
            slot.thread.join(timeout=2)


def test_tick_dispatch_no_eligible_candidates(queue_dir: Path) -> None:
    """No pending tasks → no dispatches."""
    settings = _make_settings()
    snap = _make_snapshot(SupervisorState.DISPATCHING)
    in_flight: dict[str, DispatchSlot] = {}
    with patch("claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch") as mock_dispatch:
        tick_dispatch(
            queue_dir=queue_dir,
            settings=settings,
            clock=RealClock(),
            snapshot=snap,
            in_flight_slots=in_flight,
        )
    mock_dispatch.assert_not_called()
    assert in_flight == {}


def test_tick_dispatch_priority_sort(queue_dir: Path) -> None:
    """Higher-priority tasks dispatch first when slots are limited."""
    settings = _make_settings(initial=1, max_c=1)
    snap = _make_snapshot(SupervisorState.DISPATCHING)
    _make_task(queue_dir, "low-id-but-low-priority", priority="low")
    _make_task(queue_dir, "z-high-id-but-high-priority", priority="high")
    in_flight: dict[str, DispatchSlot] = {}

    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
        return_value=None,
    ):
        tick_dispatch(
            queue_dir=queue_dir,
            settings=settings,
            clock=RealClock(),
            snapshot=snap,
            in_flight_slots=in_flight,
        )
        # High priority wins despite alphabetically-later id.
        assert "z-high-id-but-high-priority" in in_flight
        assert "low-id-but-low-priority" not in in_flight
        for slot in list(in_flight.values()):
            slot.thread.join(timeout=2)


def test_tick_dispatch_one_high_two_normal_high_goes_first(queue_dir: Path) -> None:
    """One high-priority task + two normal must dispatch high first.

    Regression coverage for the operator's expectation that a late
    ``priority: high`` task jumps ahead of alphabetically-earlier
    ``priority: normal`` tasks. The high task here has the
    alphabetically LAST id; if the sort were id-only it would dispatch
    third.
    """
    settings = _make_settings(initial=1, max_c=1)
    snap = _make_snapshot(SupervisorState.DISPATCHING)
    _make_task(queue_dir, "001-normal-alpha", priority="normal")
    _make_task(queue_dir, "002-normal-beta", priority="normal")
    _make_task(queue_dir, "999-high-zulu", priority="high")
    in_flight: dict[str, DispatchSlot] = {}

    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
        return_value=None,
    ):
        tick_dispatch(
            queue_dir=queue_dir,
            settings=settings,
            clock=RealClock(),
            snapshot=snap,
            in_flight_slots=in_flight,
        )
        assert "999-high-zulu" in in_flight
        assert "001-normal-alpha" not in in_flight
        assert "002-normal-beta" not in in_flight
        for slot in list(in_flight.values()):
            slot.thread.join(timeout=2)


def test_tick_dispatch_priority_then_id_within_band(queue_dir: Path) -> None:
    """Within a priority band, ties break by task id (ascending)."""
    settings = _make_settings(initial=3, max_c=3)
    snap = _make_snapshot(SupervisorState.DISPATCHING)
    # Two high-priority tasks: high-zzz id, high-aaa id. Among the
    # three slots, both should be picked and high-aaa precedes high-zzz
    # under (priority_rank, task_id).
    _make_task(queue_dir, "high-zzz", priority="high")
    _make_task(queue_dir, "high-aaa", priority="high")
    _make_task(queue_dir, "normal-mmm", priority="normal")
    in_flight: dict[str, DispatchSlot] = {}

    dispatch_order: list[str] = []
    real_thread_start = threading.Thread.start

    def record_then_start(self: threading.Thread) -> None:
        # The thread name is "dispatch-<task_id>" (see orchestrator).
        if self.name.startswith("dispatch-"):
            dispatch_order.append(self.name.removeprefix("dispatch-"))
        real_thread_start(self)

    with (
        patch(
            "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
            return_value=None,
        ),
        patch.object(threading.Thread, "start", record_then_start),
    ):
        tick_dispatch(
            queue_dir=queue_dir,
            settings=settings,
            clock=RealClock(),
            snapshot=snap,
            in_flight_slots=in_flight,
            accounts=_resolved(cap=3),
        )
        for slot in list(in_flight.values()):
            slot.thread.join(timeout=2)

    assert dispatch_order == ["high-aaa", "high-zzz", "normal-mmm"]


# ---------------------------------------------------------------------------
# _dispatch_one_safely
# ---------------------------------------------------------------------------


def test_dispatch_one_safely_loads_existing_state(queue_dir: Path) -> None:
    task = _make_task(queue_dir, "t1")
    _seed_state(queue_dir, task.id, "failed", attempts=2)
    settings = _make_settings()
    captured: dict[str, Any] = {}

    def fake_dispatch(*, task, state, plan, **kwargs):
        captured["state"] = state
        captured["plan"] = plan

    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
        side_effect=fake_dispatch,
    ):
        _dispatch_one_safely(task, queue_dir, settings, RealClock(), "claude", "", None, "default")

    # Prior state was loaded and forwarded (attempts=2).
    assert captured["state"].attempts == 2


def test_dispatch_one_safely_starts_fresh_when_no_state(queue_dir: Path) -> None:
    task = _make_task(queue_dir, "t1")
    settings = _make_settings()
    captured: dict[str, Any] = {}

    def fake_dispatch(*, task, state, plan, **kwargs):
        captured["state"] = state

    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
        side_effect=fake_dispatch,
    ):
        _dispatch_one_safely(task, queue_dir, settings, RealClock(), "claude", "", None, "default")

    # Fresh state: attempts=0.
    assert captured["state"].attempts == 0
    assert captured["state"].task_id == "t1"


def test_dispatch_one_safely_handles_unparseable_state(queue_dir: Path) -> None:
    """If the existing state file won't parse, fall back to fresh state."""
    task = _make_task(queue_dir, "t1")
    bad_state = state_path_for(queue_dir, task.id)
    bad_state.parent.mkdir(parents=True, exist_ok=True)
    bad_state.write_text("not yaml: ][", encoding="utf-8")
    settings = _make_settings()
    captured: dict[str, Any] = {}

    def fake_dispatch(*, task, state, plan, **kwargs):
        captured["state"] = state

    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
        side_effect=fake_dispatch,
    ):
        _dispatch_one_safely(task, queue_dir, settings, RealClock(), "claude", "", None, "default")

    # Unparseable state ⇒ fresh fallback.
    assert captured["state"].attempts == 0


def test_dispatch_one_safely_logs_exceptions(queue_dir: Path) -> None:
    """An exception inside dispatch must not crash the orchestrator
    thread — it's logged and swallowed."""
    task = _make_task(queue_dir, "t1")
    settings = _make_settings()
    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
        side_effect=RuntimeError("boom"),
    ):
        # Must not raise.
        _dispatch_one_safely(task, queue_dir, settings, RealClock(), "claude", "", None, "default")
