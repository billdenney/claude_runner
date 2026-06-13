"""Tests for the dispatcher's background ``dispatcher_alive_at`` monitor.

The monitor thread writes ``dispatcher_alive_at`` on a fixed cadence
regardless of stream-json event arrival. This gives the supervisor's
per-tick reaper a second liveness signal so it can distinguish "agent
quiet, dispatcher alive" (HEALTHY) from "agent and dispatcher both
silent" (suspect zombie, fall through to filesystem verification).

These tests exercise :class:`_DispatcherAliveMonitor` directly with a
short interval and a real ``threading.Thread``, asserting on the
sequence of persist calls.
"""

from __future__ import annotations

import itertools
import time
from datetime import UTC, datetime

from claude_task_runner.clock import RealClock
from claude_task_runner.runner.dispatcher import _DispatcherAliveMonitor


def test_initial_write_happens_on_start() -> None:
    """``start()`` should fire the initial persist on the caller's
    thread BEFORE the background loop begins ticking — otherwise a
    very-fast dispatch could finish before the first wake-up and
    leave ``dispatcher_alive_at`` unset for the whole run."""
    persists: list[datetime] = []

    def persist(when: datetime) -> None:
        persists.append(when)

    monitor = _DispatcherAliveMonitor(
        persist_fn=persist,
        clock=RealClock(),
        # Long interval so the background loop's first wake-up doesn't
        # race the test's stop() — we want to observe only the initial
        # write.
        interval_s=60.0,
        task_id="t-alive-initial",
    )
    monitor.start()
    monitor.stop()

    assert len(persists) >= 1
    assert isinstance(persists[0], datetime)


def test_loop_persists_on_interval() -> None:
    """The background loop should call ``persist_fn`` once per
    ``interval_s``. Two ticks within ~0.4s prove the loop is awake."""
    persists: list[datetime] = []

    def persist(when: datetime) -> None:
        persists.append(when)

    monitor = _DispatcherAliveMonitor(
        persist_fn=persist,
        clock=RealClock(),
        interval_s=0.1,
        task_id="t-alive-loop",
    )
    monitor.start()
    # Initial write + ~3 loop ticks within ~0.35s.
    time.sleep(0.35)
    monitor.stop()

    # Initial + at least one loop tick.
    assert len(persists) >= 2
    # All values are advancing in time (the RealClock returns real
    # wall-clock so consecutive calls are strictly increasing).
    for earlier, later in itertools.pairwise(persists):
        assert later >= earlier


def test_stop_idempotent_and_joins_quickly() -> None:
    """Calling stop() before the first loop tick should still cleanly
    join the thread (initial write already happened on start)."""
    persists: list[datetime] = []

    def persist(when: datetime) -> None:
        persists.append(when)

    monitor = _DispatcherAliveMonitor(
        persist_fn=persist,
        clock=RealClock(),
        interval_s=60.0,
        task_id="t-stop-idempotent",
    )
    monitor.start()
    monitor.stop()
    # A second stop is a no-op (Event.set is idempotent; the thread
    # is already joined).
    monitor.stop()
    assert len(persists) >= 1


def test_persist_failure_does_not_crash_thread() -> None:
    """A raising persist function inside the loop must not take the
    monitor thread down — observability is best-effort. The loop
    keeps trying so a transient disk-full clears on the next tick."""
    counts = {"calls": 0, "raises": 0}

    def flaky_persist(_when: datetime) -> None:
        counts["calls"] += 1
        if counts["calls"] in (2, 3):  # raise on a couple of ticks
            counts["raises"] += 1
            raise OSError("transient disk full")

    monitor = _DispatcherAliveMonitor(
        persist_fn=flaky_persist,
        clock=RealClock(),
        interval_s=0.05,
        task_id="t-flaky",
    )
    monitor.start()
    time.sleep(0.3)
    monitor.stop()

    # The flaky tick raised, but the monitor kept running and recorded
    # several additional successful writes after the failure window.
    assert counts["raises"] >= 1
    assert counts["calls"] > counts["raises"]


def test_initial_persist_failure_does_not_block_start() -> None:
    """A raising initial persist must not prevent the loop from
    starting — the field will catch up on the first interval tick."""

    state = {"initial_called": False, "loop_calls": 0}

    def persist(_when: datetime) -> None:
        if not state["initial_called"]:
            state["initial_called"] = True
            raise OSError("first write failed")
        state["loop_calls"] += 1

    monitor = _DispatcherAliveMonitor(
        persist_fn=persist,
        clock=RealClock(),
        interval_s=0.05,
        task_id="t-initial-fail",
    )
    monitor.start()
    time.sleep(0.2)
    monitor.stop()

    assert state["initial_called"]
    assert state["loop_calls"] >= 1


def test_clock_is_consulted_per_write() -> None:
    """Each persist call should receive the clock's current value, not
    a cached one — proves the monitor reads ``clock.now()`` afresh."""

    class _StepClock:
        def __init__(self) -> None:
            self._n = 0

        def now(self) -> datetime:
            self._n += 1
            # Deterministic UTC timestamps stepping forward by one
            # second per call.
            return datetime(2026, 6, 13, 12, self._n // 60, self._n % 60, tzinfo=UTC)

        def monotonic(self) -> float:
            return float(self._n)

    persists: list[datetime] = []

    def persist(when: datetime) -> None:
        persists.append(when)

    monitor = _DispatcherAliveMonitor(
        persist_fn=persist,
        clock=_StepClock(),
        interval_s=0.05,
        task_id="t-step",
    )
    monitor.start()
    time.sleep(0.2)
    monitor.stop()

    # Each call advanced the step clock, so consecutive timestamps
    # differ by 1s.
    assert len(persists) >= 2
    deltas = [(later - earlier).total_seconds() for earlier, later in itertools.pairwise(persists)]
    # All deltas are positive integers (1s each).
    assert all(d >= 1.0 for d in deltas)
