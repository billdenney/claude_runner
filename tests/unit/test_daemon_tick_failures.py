"""Daemon tick-failure counters and dispatch-outage escalation.

Each of the three subsystems the daemon drives per tick — force
dispatch (``fd_mod.tick_consume``), the per-tick silent-orphan reaper
(``reconcile_silent.reap_silent_orphans_tick``), and the dispatch step
(``orch_mod.tick_dispatch``) — runs inside a ``try/except Exception``
so a crash in one does not take the supervisor down. That keep-alive
behaviour is correct, but a *sustained* outage of any one subsystem
would otherwise be invisible: the process "looks alive" while a whole
subsystem is silently dead.

These tests pin the observability that closes that gap:

* Each failure bumps the matching counter on
  :class:`TickFailureCounters` (surfaced via
  :attr:`DaemonHandle.tick_failures`).
* Consecutive ``tick_dispatch`` failures past
  :data:`DISPATCH_OUTAGE_ESCALATION_TICKS` escalate to a critical
  notify + a ``supervisor_dispatch_outage`` event, and a later clean
  tick clears the outage flag.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.queue.store import queue_runtime_dir, todo_dir
from claude_task_runner.supervisor import daemon as daemon_mod
from claude_task_runner.supervisor import reconcile_silent as rs_mod
from claude_task_runner.supervisor.daemon import (
    DISPATCH_OUTAGE_ESCALATION_TICKS,
    start_daemon,
)
from claude_task_runner.usage.models import UsageReading, WindowReading
from claude_task_runner.usage.source import FakeUsageSource


def _queue(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


def _reading(captured_at: datetime) -> UsageReading:
    return UsageReading(
        captured_at=captured_at,
        five_hour=WindowReading(
            utilization_pct=10,
            resets_at_raw="x",
            resets_at=captured_at + timedelta(hours=5),
        ),
        seven_day=WindowReading(
            utilization_pct=10,
            resets_at_raw="x",
            resets_at=captured_at + timedelta(days=7),
        ),
    )


@pytest.fixture
def _isolate_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the global supervisor lock at a per-test path so these
    tests don't contend with a real supervisor (or each other)."""
    monkeypatch.setattr(
        "claude_task_runner.supervisor.pidfile.global_lock_path",
        lambda: tmp_path / "test_global.lock",
    )


def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default poll_interval_s is 60s; skip the inter-tick sleep so the
    test finishes immediately."""
    monkeypatch.setattr(daemon_mod, "sleep_for_next_poll", lambda **kw: None)


# ---------------------------------------------------------------------------
# Finding 1: force-dispatch tick failure is counted, loop survives
# ---------------------------------------------------------------------------


def test_force_dispatch_failure_increments_counter_and_loop_survives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_lock: None,
) -> None:
    """A raising ``tick_consume`` bumps ``force_dispatch_total`` once per
    tick without aborting the loop, and leaves dispatch untouched."""
    from claude_task_runner.runner import force_dispatch as fd_mod
    from claude_task_runner.runner import orchestrator as orch_mod

    _no_sleep(monkeypatch)
    monkeypatch.setattr(orch_mod, "tick_dispatch", lambda **kw: kw["snapshot"])

    def boom(**_kw: object) -> None:
        raise RuntimeError("force-dispatch broke")

    monkeypatch.setattr(fd_mod, "tick_consume", boom)

    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    handle = start_daemon(
        queue_dir=_queue(tmp_path),
        settings=load_settings(None),
        source=FakeUsageSource([_reading(now)] * 3),
        pending_count_fn=lambda: 0,
        in_flight_count_fn=lambda: 0,
        clock=FakeClock(now),
        install_signal_handlers=False,
        max_ticks=3,
    )

    # Three ticks ran (loop survived all three failures).
    assert handle.tick_failures.force_dispatch_total == 3
    # The failure was isolated to force-dispatch.
    assert handle.tick_failures.dispatch_total == 0
    assert handle.tick_failures.reap_total == 0
    assert handle.tick_failures.dispatch_outage is False


# ---------------------------------------------------------------------------
# Finding 2: per-tick reap failure is counted, loop survives
# ---------------------------------------------------------------------------


def test_reap_failure_increments_counter_and_loop_survives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_lock: None,
) -> None:
    """A raising ``reap_silent_orphans_tick`` bumps ``reap_total`` once
    per tick (default interval = every tick) without aborting the loop."""
    from claude_task_runner.runner import force_dispatch as fd_mod
    from claude_task_runner.runner import orchestrator as orch_mod

    _no_sleep(monkeypatch)
    monkeypatch.setattr(orch_mod, "tick_dispatch", lambda **kw: kw["snapshot"])
    monkeypatch.setattr(fd_mod, "tick_consume", lambda **kw: None)

    def boom(*_a: object, **_kw: object) -> list[object]:
        raise RuntimeError("reaper broke")

    monkeypatch.setattr(rs_mod, "reap_silent_orphans_tick", boom)

    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    handle = start_daemon(
        queue_dir=_queue(tmp_path),
        settings=load_settings(None),
        source=FakeUsageSource([_reading(now)] * 3),
        pending_count_fn=lambda: 0,
        in_flight_count_fn=lambda: 0,
        clock=FakeClock(now),
        install_signal_handlers=False,
        max_ticks=3,
    )

    assert handle.tick_failures.reap_total == 3
    assert handle.tick_failures.force_dispatch_total == 0
    assert handle.tick_failures.dispatch_total == 0


# ---------------------------------------------------------------------------
# Finding 3: dispatch failure counted; consecutive failures escalate
# ---------------------------------------------------------------------------


def test_dispatch_failure_increments_consecutive_counter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_lock: None,
) -> None:
    """Below the escalation threshold, ``tick_dispatch`` failures bump
    both the total and consecutive counters but do not flip the outage
    flag or emit the outage event."""
    from claude_task_runner.runner import force_dispatch as fd_mod
    from claude_task_runner.runner import orchestrator as orch_mod

    _no_sleep(monkeypatch)
    monkeypatch.setattr(fd_mod, "tick_consume", lambda **kw: None)

    def boom(**_kw: object) -> object:
        raise RuntimeError("dispatch broke")

    monkeypatch.setattr(orch_mod, "tick_dispatch", boom)

    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    ticks = DISPATCH_OUTAGE_ESCALATION_TICKS - 1
    events: list[tuple[str, dict[str, object]]] = []
    handle = start_daemon(
        queue_dir=_queue(tmp_path),
        settings=load_settings(None),
        source=FakeUsageSource([_reading(now)] * ticks),
        pending_count_fn=lambda: 1,
        in_flight_count_fn=lambda: 0,
        clock=FakeClock(now),
        event_callback=lambda kind, payload: events.append((kind, payload)),
        install_signal_handlers=False,
        max_ticks=ticks,
    )

    assert handle.tick_failures.dispatch_total == ticks
    assert handle.tick_failures.dispatch_consecutive == ticks
    # One short of the threshold: no outage yet.
    assert handle.tick_failures.dispatch_outage is False
    assert not [k for k, _ in events if k == "supervisor_dispatch_outage"]


def test_dispatch_outage_escalates_once_past_threshold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_lock: None,
) -> None:
    """Past ``DISPATCH_OUTAGE_ESCALATION_TICKS`` consecutive failures the
    daemon escalates exactly once (the ``dispatch_outage`` flag de-dupes
    so a sustained outage doesn't spam the operator every tick): one
    ``supervisor_dispatch_outage`` event and one critical notify even
    though the failures keep coming."""
    from claude_task_runner.runner import force_dispatch as fd_mod
    from claude_task_runner.runner import orchestrator as orch_mod

    _no_sleep(monkeypatch)
    monkeypatch.setattr(fd_mod, "tick_consume", lambda **kw: None)
    monkeypatch.setattr(
        orch_mod,
        "tick_dispatch",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("dispatch broke")),
    )

    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    # Two ticks PAST the threshold to prove de-dup (escalation must not
    # re-fire on every subsequent failing tick).
    ticks = DISPATCH_OUTAGE_ESCALATION_TICKS + 2
    notifications: list[tuple[str, str]] = []
    events: list[tuple[str, dict[str, object]]] = []
    handle = start_daemon(
        queue_dir=_queue(tmp_path),
        settings=load_settings(None),
        source=FakeUsageSource([_reading(now)] * ticks),
        pending_count_fn=lambda: 1,
        in_flight_count_fn=lambda: 0,
        clock=FakeClock(now),
        notify_callback=lambda level, msg: notifications.append((level, msg)),
        event_callback=lambda kind, payload: events.append((kind, payload)),
        install_signal_handlers=False,
        max_ticks=ticks,
    )

    assert handle.tick_failures.dispatch_consecutive == ticks
    assert handle.tick_failures.dispatch_total == ticks
    assert handle.tick_failures.dispatch_outage is True

    outage_events = [p for k, p in events if k == "supervisor_dispatch_outage"]
    # Escalation event fires exactly once despite ``ticks`` failures.
    assert len(outage_events) == 1
    # Captured at the threshold-crossing tick.
    assert outage_events[0]["consecutive_failures"] == DISPATCH_OUTAGE_ESCALATION_TICKS
    assert outage_events[0]["threshold"] == DISPATCH_OUTAGE_ESCALATION_TICKS

    # Exactly one critical-level notification (also de-duped).
    criticals = [msg for level, msg in notifications if level == "critical"]
    assert len(criticals) == 1


def test_dispatch_recovery_resets_consecutive_and_clears_outage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_lock: None,
) -> None:
    """After the outage escalates, the first clean ``tick_dispatch``
    resets the consecutive counter and clears the outage flag, while the
    lifetime total is preserved."""
    from claude_task_runner.runner import force_dispatch as fd_mod
    from claude_task_runner.runner import orchestrator as orch_mod

    _no_sleep(monkeypatch)
    monkeypatch.setattr(fd_mod, "tick_consume", lambda **kw: None)

    fail_ticks = DISPATCH_OUTAGE_ESCALATION_TICKS
    calls = {"n": 0}

    def flaky(**kw: object) -> object:
        calls["n"] += 1
        if calls["n"] <= fail_ticks:
            raise RuntimeError("dispatch broke")
        return kw["snapshot"]

    monkeypatch.setattr(orch_mod, "tick_dispatch", flaky)

    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    total_ticks = fail_ticks + 1  # one trailing clean tick
    handle = start_daemon(
        queue_dir=_queue(tmp_path),
        settings=load_settings(None),
        source=FakeUsageSource([_reading(now)] * total_ticks),
        pending_count_fn=lambda: 1,
        in_flight_count_fn=lambda: 0,
        clock=FakeClock(now),
        install_signal_handlers=False,
        max_ticks=total_ticks,
    )

    # Lifetime total persists; consecutive run + outage flag cleared by
    # the trailing clean tick.
    assert handle.tick_failures.dispatch_total == fail_ticks
    assert handle.tick_failures.dispatch_consecutive == 0
    assert handle.tick_failures.dispatch_outage is False
