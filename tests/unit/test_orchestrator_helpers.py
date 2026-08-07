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
    load_state,
    queue_runtime_dir,
    state_path_for,
    task_path_for,
    todo_dir,
    write_state_atomic,
    write_task_atomic,
)
from claude_task_runner.runner import readiness
from claude_task_runner.runner.in_flight import DispatchSlot
from claude_task_runner.runner.orchestrator import (
    _completed_task_ids,
    _dispatch_one_safely,
    _has_any_completed,
    _reap_finished,
    _recorded_subprocess_pid,
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
# _reap_finished subprocess-leak detection (Bug 5)
# ---------------------------------------------------------------------------


def _exited_thread() -> threading.Thread:
    """Return a Thread that has already exited (so is_alive() is False)."""
    t = threading.Thread(target=lambda: None, daemon=True)
    t.start()
    t.join(timeout=2)
    assert not t.is_alive()
    return t


def _seed_run_with_pid(qd: Path, task_id: str, pid: int | None, status: str = "running") -> None:
    """Persist a TaskState whose most recent run record carries ``pid``."""
    from claude_task_runner.queue.schema import RunRecord, TokenUsage

    run = RunRecord(
        attempt=1,
        started_at=datetime(2026, 5, 21, tzinfo=UTC),
        finished_at=datetime(2026, 5, 21, 0, 1, tzinfo=UTC),
        stop_reason="killed_by_cap",
        usage=TokenUsage(),
        duration_s=60.0,
        pid=pid,
    )
    state = TaskState(task_id=task_id, status=status, runs=[run])
    write_state_atomic(state, state_path_for(qd, task_id))


def test_reap_finished_frees_slot_when_pid_is_dead(queue_dir: Path) -> None:
    """Standard path: pid in run record is no longer alive → slot is freed.

    The post-tick check probes ``_pid_alive``; with the dispatcher_mod
    helper returning ``False``, the slot is deleted exactly like the
    legacy queue_dir=None path."""
    t = _exited_thread()
    d = {"t1": _slot("t1", t)}
    _seed_run_with_pid(queue_dir, "t1", pid=99_999_999)

    with patch(
        "claude_task_runner.runner.dispatcher._pid_alive",
        return_value=False,
    ):
        _reap_finished(d, queue_dir=queue_dir, clock=RealClock())

    assert "t1" not in d


def test_reap_finished_frees_slot_when_runs_have_no_pid(queue_dir: Path) -> None:
    """Legacy run records (pid field absent / None) cannot be probed → free slot.

    Skipping the probe matches the pre-Bug-5 behaviour for state YAMLs
    written before the field landed, and for pre-dispatch-hook failures
    that never spawned a subprocess."""
    t = _exited_thread()
    d = {"t1": _slot("t1", t)}
    _seed_run_with_pid(queue_dir, "t1", pid=None)

    _reap_finished(d, queue_dir=queue_dir, clock=RealClock())
    assert "t1" not in d


def test_reap_finished_holds_slot_when_pid_is_alive(queue_dir: Path) -> None:
    """Subprocess-leak path: pid in run record is still alive → slot held.

    The orchestrator refuses to free the slot so the queue doesn't
    re-dispatch onto the same account while a leaked subprocess is
    holding resources. A notify_callback and event_callback fire once
    on first detection; the slot's ``subprocess_leak_notified_at`` is
    stamped to deduplicate further re-checks."""
    t = _exited_thread()
    slot = _slot("t1", t)
    d = {"t1": slot}
    _seed_run_with_pid(queue_dir, "t1", pid=12345)
    notifs: list[tuple[str, str]] = []
    events: list[tuple[str, dict]] = []

    with patch(
        "claude_task_runner.runner.dispatcher._pid_alive",
        return_value=True,
    ):
        _reap_finished(
            d,
            queue_dir=queue_dir,
            clock=RealClock(),
            notify_callback=lambda level, msg: notifs.append((level, msg)),
            event_callback=lambda name, payload: events.append((name, payload)),
        )

    assert "t1" in d
    assert slot.subprocess_leak_notified_at is not None
    assert len(notifs) == 1
    assert notifs[0][0] == "critical"
    assert "t1" in notifs[0][1]
    assert "12345" in notifs[0][1]
    assert events == [("subprocess_leak_detected", {"task_id": "t1", "pid": 12345})]


def test_reap_finished_does_not_renotify_known_leak(queue_dir: Path) -> None:
    """Already-notified leak: subsequent reap_finished calls stay silent.

    Once ``subprocess_leak_notified_at`` is set, the orchestrator keeps
    probing on every tick (cheap), holds the slot, but does NOT spam
    the notify/event callbacks. This is what lets the supervisor sit
    on a leak for hours without flooding the operator."""
    t = _exited_thread()
    slot = _slot("t1", t)
    slot.subprocess_leak_notified_at = datetime(2026, 5, 21, tzinfo=UTC)
    d = {"t1": slot}
    _seed_run_with_pid(queue_dir, "t1", pid=12345)
    notifs: list = []
    events: list = []

    with patch(
        "claude_task_runner.runner.dispatcher._pid_alive",
        return_value=True,
    ):
        _reap_finished(
            d,
            queue_dir=queue_dir,
            clock=RealClock(),
            notify_callback=lambda level, msg: notifs.append((level, msg)),
            event_callback=lambda name, payload: events.append((name, payload)),
        )

    assert "t1" in d
    assert notifs == []
    assert events == []


def test_reap_finished_recovers_slot_when_pid_finally_dies(queue_dir: Path) -> None:
    """A held slot frees normally once the leaked pid finally dies.

    Operator workflow: the slot is held while the kernel keeps the
    process in D-state, then SIGKILL eventually lands (or the kernel
    unblocks) — the next reap sees the pid is gone and the slot is
    released so the queue can dispatch again."""
    t = _exited_thread()
    slot = _slot("t1", t)
    slot.subprocess_leak_notified_at = datetime(2026, 5, 21, tzinfo=UTC)
    d = {"t1": slot}
    _seed_run_with_pid(queue_dir, "t1", pid=12345)

    with patch(
        "claude_task_runner.runner.dispatcher._pid_alive",
        return_value=False,
    ):
        _reap_finished(d, queue_dir=queue_dir, clock=RealClock())

    assert "t1" not in d


def test_recorded_subprocess_pid_returns_pid_for_running_task(queue_dir: Path) -> None:
    """Non-deferred task: the helper returns ``runs[-1].pid`` so the leak
    guard can probe the just-finished dispatch's subprocess."""
    _seed_run_with_pid(queue_dir, "t1", pid=4242, status="running")
    assert _recorded_subprocess_pid(queue_dir, "t1") == 4242


def test_recorded_subprocess_pid_returns_none_for_deferred_task(queue_dir: Path) -> None:
    """Deferred task (ADR-0029): the helper returns None despite a pid on
    ``runs[-1]`` — that run belongs to an earlier real dispatch, not the
    deferral (which spawned no subprocess and appended no run)."""
    _seed_run_with_pid(queue_dir, "t1", pid=4242, status="deferred")
    assert _recorded_subprocess_pid(queue_dir, "t1") is None


def test_recorded_subprocess_pid_none_when_state_missing(queue_dir: Path) -> None:
    """No state file → nothing to probe → None."""
    assert _recorded_subprocess_pid(queue_dir, "nope") is None


def test_reap_finished_frees_deferred_slot_despite_alive_pid(queue_dir: Path) -> None:
    """Regression (ADR-0029): a ``deferred`` task frees its slot even when
    ``runs[-1].pid`` probes alive — the deferral spawned no subprocess.

    A pre-dispatch exit-1 deferral parks the task in ``deferred`` WITHOUT
    spawning a worker and WITHOUT appending a RunRecord (ADR-0026), so
    ``runs[-1]`` here is a STALE record from an earlier *real* dispatch.
    Its OS pid has long exited and may now be RECYCLED by an unrelated
    process — which ``_pid_alive`` reports alive (it even reports alive
    on ``EPERM`` for a foreign-owned pid). Before the fix,
    :func:`_recorded_subprocess_pid` handed that recycled pid to the leak
    guard, which HELD the slot forever: one deferred task pinned a
    low-concurrency account (``work`` at ``max_concurrency=1`` sat at 0%
    dispatch for days). The deferring dispatch had nothing to leak, so
    the slot MUST free and NO leak notification may fire — contrast
    :func:`test_reap_finished_holds_slot_when_pid_is_alive`, which is the
    identical setup but ``status="running"`` (a genuine leak, held)."""
    t = _exited_thread()
    slot = _slot("t1", t)
    d = {"t1": slot}
    # Stale prior run carries a real pid, but the task is now `deferred`.
    _seed_run_with_pid(queue_dir, "t1", pid=12345, status="deferred")
    notifs: list[tuple[str, str]] = []
    events: list[tuple[str, dict]] = []

    with patch(
        "claude_task_runner.runner.dispatcher._pid_alive",
        return_value=True,
    ):
        _reap_finished(
            d,
            queue_dir=queue_dir,
            clock=RealClock(),
            notify_callback=lambda level, msg: notifs.append((level, msg)),
            event_callback=lambda name, payload: events.append((name, payload)),
        )

    assert "t1" not in d
    assert slot.subprocess_leak_notified_at is None
    assert notifs == []
    assert events == []


def test_reap_finished_still_holds_running_leak_after_deferral_fix(queue_dir: Path) -> None:
    """The ADR-0029 deferral carve-out must NOT weaken the real leak guard:
    a ``running`` task with a live ``runs[-1].pid`` is still a genuine
    subprocess leak and its slot stays held. (Guards against a fix that
    over-broadly returns None for any status.)"""
    t = _exited_thread()
    slot = _slot("t1", t)
    d = {"t1": slot}
    _seed_run_with_pid(queue_dir, "t1", pid=12345, status="running")

    with patch(
        "claude_task_runner.runner.dispatcher._pid_alive",
        return_value=True,
    ):
        _reap_finished(d, queue_dir=queue_dir, clock=RealClock())

    assert "t1" in d
    assert slot.subprocess_leak_notified_at is not None


def test_reap_finished_legacy_signature_still_frees_slot() -> None:
    """Backwards-compat: a single-arg call (no queue_dir) skips the leak check.

    A handful of internal helpers and prior-revision tests pass just
    the slot dict. The check is purely defence in depth; absent the
    queue_dir the function must keep its historical free-the-slot
    behaviour."""
    t = _exited_thread()
    d = {"t1": _slot("t1", t)}

    _reap_finished(d)
    assert "t1" not in d


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


@pytest.mark.parametrize("target", [1, 2, 3, 5])
def test_tick_dispatch_no_dispatch_at_exact_capacity_boundary(queue_dir: Path, target: int) -> None:
    """Boundary of ``available = max(0, target - in_flight_count)``.

    When ``in_flight_count == target`` the available-slot count is
    exactly 0 and the loop must short-circuit before any dispatch —
    even though there is a pending, eligible task waiting. The existing
    ``test_tick_dispatch_respects_in_flight_capacity`` only pins
    ``target == 1``; this parametrization exercises the equality edge
    for several targets so an off-by-one in the subtraction (e.g.
    ``target - count + 1``) is caught.
    """
    settings = _make_settings(initial=target, max_c=target)
    snap = _make_snapshot(SupervisorState.DISPATCHING)
    # A pending task that WOULD be eligible if any slot were free.
    _make_task(queue_dir, "pending-but-blocked")

    # Fill in_flight to exactly `target` with live (busy) threads.
    stop_evt = threading.Event()
    busy_threads: list[threading.Thread] = []
    in_flight: dict[str, DispatchSlot] = {}
    for i in range(target):
        th = threading.Thread(target=lambda: stop_evt.wait(timeout=5), daemon=True)
        th.start()
        busy_threads.append(th)
        in_flight[f"busy-{i}"] = _slot(f"busy-{i}", th)
    assert len(in_flight) == target

    try:
        with patch(
            "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch"
        ) as mock_dispatch:
            tick_dispatch(
                queue_dir=queue_dir,
                settings=settings,
                clock=RealClock(),
                snapshot=snap,
                in_flight_slots=in_flight,
                accounts=_resolved(cap=target),
            )
        # available == 0 → no dispatch, in_flight unchanged.
        mock_dispatch.assert_not_called()
        assert set(in_flight.keys()) == {f"busy-{i}" for i in range(target)}
    finally:
        stop_evt.set()
        for th in busy_threads:
            th.join(timeout=2)


def test_tick_dispatch_dispatches_exactly_one_below_capacity_boundary(
    queue_dir: Path,
) -> None:
    """One slot below target (``in_flight == target - 1``) → exactly one dispatch.

    The complement of the equality boundary above: confirms the
    short-circuit is at ``available == 0`` and not one slot too early.
    """
    target = 3
    settings = _make_settings(initial=target, max_c=target)
    snap = _make_snapshot(SupervisorState.DISPATCHING)
    _make_task(queue_dir, "newcomer")

    stop_evt = threading.Event()
    busy_threads: list[threading.Thread] = []
    in_flight: dict[str, DispatchSlot] = {}
    for i in range(target - 1):  # one slot free
        th = threading.Thread(target=lambda: stop_evt.wait(timeout=5), daemon=True)
        th.start()
        busy_threads.append(th)
        in_flight[f"busy-{i}"] = _slot(f"busy-{i}", th)

    try:
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
                accounts=_resolved(cap=target),
            )
        assert "newcomer" in in_flight
        assert len(in_flight) == target
        in_flight["newcomer"].thread.join(timeout=2)
    finally:
        stop_evt.set()
        for th in busy_threads:
            th.join(timeout=2)


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


# ---------------------------------------------------------------------------
# _dispatch_one_safely: the readiness backstop (ADR-0030)
#
# Every dispatch path funnels through this function — the orchestrator's
# selector and force_dispatch.tick_consume both spawn it — so the gate here
# is what makes "`requires` is checked on every dispatch decision" hold
# structurally instead of depending on each caller remembering to ask. It
# also covers the selector's inherent race: candidates are chosen once per
# tick, and a requirement can be withdrawn between selection and spawn.
# ---------------------------------------------------------------------------


def _task_requiring(qd: Path, task_id: str, rel_path: str) -> Task:
    return _make_task(qd, task_id, requires=[{"kind": "file", "path": rel_path}])


def test_dispatch_one_safely_refuses_when_requirement_unmet(queue_dir: Path) -> None:
    """No spawn at all — not a spawn that fails downstream."""
    task = _task_requiring(queue_dir, "t1", "inputs/missing.md")
    settings = _make_settings()
    calls: list[Any] = []

    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
        side_effect=lambda **kw: calls.append(kw),
    ):
        _dispatch_one_safely(task, queue_dir, settings, RealClock(), "claude", "", None, "default")

    assert calls == []


def test_dispatch_one_safely_records_the_hold_it_refused_on(queue_dir: Path) -> None:
    """A refusal an operator can read, not a silent no-op in a thread."""
    task = _task_requiring(queue_dir, "t1", "inputs/missing.md")
    settings = _make_settings()

    with patch("claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch"):
        _dispatch_one_safely(task, queue_dir, settings, RealClock(), "claude", "", None, "default")

    state = load_state(state_path_for(queue_dir, task.id))
    assert state.status == "deferred"
    assert readiness.is_hold_reason(state.deferred_reason)
    assert "missing.md" in (state.deferred_reason or "")


def test_dispatch_one_safely_dispatches_once_requirement_is_met(queue_dir: Path) -> None:
    """The gate is not a one-way door: with the file present it spawns."""
    task = _task_requiring(queue_dir, "t1", "inputs/present.md")
    target = queue_dir / "inputs" / "present.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("trimmed", encoding="utf-8")
    settings = _make_settings()
    calls: list[Any] = []

    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
        side_effect=lambda **kw: calls.append(kw),
    ):
        _dispatch_one_safely(task, queue_dir, settings, RealClock(), "claude", "", None, "default")

    assert len(calls) == 1


def test_dispatch_one_safely_without_requires_is_unaffected(queue_dir: Path) -> None:
    """Empty `requires` (the default, and the vast majority of tasks) must
    not acquire a new failure mode from the backstop."""
    task = _make_task(queue_dir, "t1")
    settings = _make_settings()
    calls: list[Any] = []

    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
        side_effect=lambda **kw: calls.append(kw),
    ):
        _dispatch_one_safely(task, queue_dir, settings, RealClock(), "claude", "", None, "default")

    assert len(calls) == 1
