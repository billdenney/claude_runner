"""Unit tests for :class:`runner.spawn_gate.SpawnGate`.

The gate counts the dispatch threads that have started a worker but not
yet recorded its pid. A stopping supervisor closes it: that waits, for at
most a bound, for the threads inside, and a daemon thread that reaches it
afterwards starts nothing. ``test_dispatcher_spawn_gate`` checks where
``dispatch()`` enters and leaves it, and ``test_stop_waits_for_worker_start``
checks the supervisor's side.
"""

from __future__ import annotations

import math
import threading
import time

import pytest

from claude_task_runner.runner.spawn_gate import SpawnGate

# How long a test watches for something that must not happen.
_WATCH_S = 0.5


def test_close_with_nothing_starting_returns_at_once() -> None:
    gate = SpawnGate()
    started = time.monotonic()
    assert gate.close(5.0) == []
    assert time.monotonic() - started < 1.0


def test_close_waits_for_a_thread_inside_until_it_leaves() -> None:
    gate = SpawnGate()
    gate.enter("t1")
    timer = threading.Timer(0.3, gate.leave, args=("t1",))
    timer.start()
    started = time.monotonic()

    starting = gate.close(5.0)

    waited = time.monotonic() - started
    timer.join()
    assert starting == []
    assert 0.25 <= waited < 4.0


def test_close_returns_the_tasks_still_starting_when_the_bound_expires() -> None:
    gate = SpawnGate()
    gate.enter("t2")
    gate.enter("t1")
    started = time.monotonic()

    assert gate.close(0.2) == ["t1", "t2"]
    assert time.monotonic() - started >= 0.15


def test_a_task_entered_twice_counts_until_both_leave() -> None:
    gate = SpawnGate()
    gate.enter("t1")
    gate.enter("t1")
    gate.leave("t1")
    assert gate.close(0.0) == ["t1"]
    gate.leave("t1")
    assert gate.close(0.0) == []


def test_leave_without_enter_is_an_error() -> None:
    gate = SpawnGate()
    with pytest.raises(
        ValueError, match=r"^no dispatch thread is starting a worker for task 't1'$"
    ):
        gate.leave("t1")


def _enter_in_thread(gate: SpawnGate, task_id: str, *, daemon: bool) -> threading.Event:
    """Call ``gate.enter`` from a new thread; the event is set once it returns."""
    entered = threading.Event()

    def run() -> None:
        gate.enter(task_id)
        entered.set()

    threading.Thread(target=run, daemon=daemon, name=f"enter-{task_id}").start()
    return entered


def test_a_daemon_thread_reaching_a_closed_gate_never_gets_in() -> None:
    """With adoption on, dispatch threads are daemon threads. One that gets
    to the gate after the stop began waits there until the process exits,
    so it never starts its worker."""
    gate = SpawnGate()
    assert gate.close(0.0) == []

    entered = _enter_in_thread(gate, "late", daemon=True)

    assert not entered.wait(_WATCH_S)
    assert gate.close(0.0) == []


def test_a_non_daemon_thread_reaching_a_closed_gate_goes_on() -> None:
    """With adoption off, dispatch threads are non-daemon, and the
    interpreter joins them at exit; parking one would hang the exit."""
    gate = SpawnGate()
    assert gate.close(0.0) == []

    entered = _enter_in_thread(gate, "late", daemon=False)

    assert entered.wait(5.0)
    assert gate.close(0.0) == ["late"]
    gate.leave("late")
    assert gate.close(0.0) == []


@pytest.mark.parametrize("timeout_s", [-1.0, math.nan, math.inf])
def test_close_rejects_a_timeout_that_is_not_a_finite_non_negative_number(
    timeout_s: float,
) -> None:
    """Rejected before anything changes: the gate stays open."""
    gate = SpawnGate()
    with pytest.raises(ValueError, match="timeout_s"):
        gate.close(timeout_s)

    entered = _enter_in_thread(gate, "t1", daemon=True)

    assert entered.wait(5.0)
    assert gate.close(0.0) == ["t1"]
    gate.leave("t1")
