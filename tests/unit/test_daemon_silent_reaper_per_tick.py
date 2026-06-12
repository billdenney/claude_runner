"""Integration: ``start_daemon`` wires the per-tick silent reaper.

Companion to ``test_daemon_silent_reaper`` (the startup pass). The
per-tick pass covers the silent-but-alive case the startup pass cannot
see: the supervisor stayed alive but the dispatcher's loop is wedged
on a stdout read because the subprocess emits no events. The
2026-06-12 ``frompeople-680-yu_2017`` zombie sat ~29h alive at 0.8%
CPU with this exact failure mode; PR #55 only handled the restart
case.

These tests run the daemon for a single tick with the per-tick reaper
function monkeypatched to return a known result, and assert:

* The daemon invokes the reaper on every tick when
  ``steady_state_reap_interval_ticks=1`` (the default).
* The interval knob throttles the call frequency.
* The per-tick reaper's results flow to ``notify_callback`` and
  ``event_callback`` so operators see the action in real time.
* The pass is skipped during drain mode (the operator's intent is
  "finish what's running and exit"; reaping mid-drain would race the
  drain-complete check).
"""

from __future__ import annotations

import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.queue.store import queue_runtime_dir, todo_dir
from claude_task_runner.runner.heartbeat import HeartbeatVerdict
from claude_task_runner.supervisor import reconcile_silent as rs_mod
from claude_task_runner.supervisor.daemon import start_daemon
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


def _stub_orchestrator(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub orchestrator + force-dispatch so the daemon tick loop runs
    cleanly without trying to spawn real claude subprocesses, and
    short-circuit the inter-tick sleep so the test finishes quickly."""
    from claude_task_runner.runner import force_dispatch as fd_mod
    from claude_task_runner.runner import orchestrator as orch_mod
    from claude_task_runner.supervisor import daemon as daemon_mod

    def stub_tick_dispatch(**kwargs):
        return kwargs["snapshot"]

    monkeypatch.setattr(orch_mod, "tick_dispatch", stub_tick_dispatch)
    monkeypatch.setattr(fd_mod, "tick_consume", lambda **kw: None)
    # Default poll_interval_s is 60s — without overriding the sleep,
    # each tick would block the test for a minute.
    monkeypatch.setattr(daemon_mod, "sleep_for_next_poll", lambda **kw: None)


def test_per_tick_reap_runs_every_tick_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``steady_state_reap_interval_ticks=1`` (default), the
    daemon calls ``reap_silent_orphans_tick`` exactly once per tick."""
    qd = _queue(tmp_path)
    test_lock = tmp_path / "test_global.lock"
    monkeypatch.setattr(
        "claude_task_runner.supervisor.pidfile.global_lock_path",
        lambda: test_lock,
    )
    _stub_orchestrator(monkeypatch)

    call_count = {"n": 0}

    def fake_reap(queue_dir, in_flight_task_ids, *, settings, clock, sigterm_fn=None):
        call_count["n"] += 1
        return []

    monkeypatch.setattr(rs_mod, "reap_silent_orphans_tick", fake_reap)

    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    settings = load_settings(None)
    source = FakeUsageSource([_reading(now), _reading(now), _reading(now)])

    start_daemon(
        queue_dir=qd,
        settings=settings,
        source=source,
        pending_count_fn=lambda: 0,
        in_flight_count_fn=lambda: 0,
        clock=FakeClock(now),
        install_signal_handlers=False,
        max_ticks=3,
    )

    assert call_count["n"] == 3


def test_per_tick_reap_results_surface_to_callbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-empty :class:`ReapResult` list flows to both
    ``notify_callback`` and ``event_callback`` with the steady-state
    event type and a warning level — distinct from the startup pass
    so operators can filter on the audit trail."""
    qd = _queue(tmp_path)
    test_lock = tmp_path / "test_global.lock"
    monkeypatch.setattr(
        "claude_task_runner.supervisor.pidfile.global_lock_path",
        lambda: test_lock,
    )
    _stub_orchestrator(monkeypatch)

    fake_result = rs_mod.ReapResult(
        task_id="t-steady-x",
        verdict=HeartbeatVerdict.KILL,
        silence_s=1234.0,
        pid=9999,
        sigtermed=True,
    )

    def fake_reap(queue_dir, in_flight_task_ids, *, settings, clock, sigterm_fn=None):
        return [fake_result]

    monkeypatch.setattr(rs_mod, "reap_silent_orphans_tick", fake_reap)

    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    settings = load_settings(None)
    source = FakeUsageSource([_reading(now)])
    notifications: list[tuple[str, str]] = []
    events: list[tuple[str, dict[str, object]]] = []

    start_daemon(
        queue_dir=qd,
        settings=settings,
        source=source,
        pending_count_fn=lambda: 0,
        in_flight_count_fn=lambda: 0,
        clock=FakeClock(now),
        notify_callback=lambda level, msg: notifications.append((level, msg)),
        event_callback=lambda kind, payload: events.append((kind, payload)),
        install_signal_handlers=False,
        max_ticks=1,
    )

    # Notification carries the steady-state suffix so operators can
    # visually distinguish from the startup-pass notifications.
    steady_notifs = [
        msg for level, msg in notifications if "steady-state" in msg and "t-steady-x" in msg
    ]
    assert len(steady_notifs) == 1
    assert any(level == "warning" for level, _ in notifications)

    # The event name is the steady-state variant.
    steady_events = [
        payload for kind, payload in events if kind == "silent_orphan_reaped_steady_state"
    ]
    assert len(steady_events) == 1
    assert steady_events[0]["task_id"] == "t-steady-x"
    assert steady_events[0]["pid"] == 9999
    assert steady_events[0]["sigtermed"] is True


def test_per_tick_reap_interval_throttles_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``steady_state_reap_interval_ticks=4`` the reaper runs on
    tick 0, 4, 8, ... — useful when very-long-running tasks make the
    per-tick scan worth dialing back."""
    qd = _queue(tmp_path)
    test_lock = tmp_path / "test_global.lock"
    monkeypatch.setattr(
        "claude_task_runner.supervisor.pidfile.global_lock_path",
        lambda: test_lock,
    )
    _stub_orchestrator(monkeypatch)

    call_count = {"n": 0}

    def fake_reap(queue_dir, in_flight_task_ids, *, settings, clock, sigterm_fn=None):
        call_count["n"] += 1
        return []

    monkeypatch.setattr(rs_mod, "reap_silent_orphans_tick", fake_reap)

    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    base = load_settings(None)
    # Bump the interval knob to every-4th tick.
    new_caps = base.task_caps.model_copy(update={"steady_state_reap_interval_ticks": 4})
    settings = base.model_copy(update={"task_caps": new_caps})
    source = FakeUsageSource([_reading(now)] * 8)

    start_daemon(
        queue_dir=qd,
        settings=settings,
        source=source,
        pending_count_fn=lambda: 0,
        in_flight_count_fn=lambda: 0,
        clock=FakeClock(now),
        install_signal_handlers=False,
        max_ticks=8,
    )

    # Ticks 0, 4 fire the reaper. Ticks 1-3 and 5-7 skip it.
    assert call_count["n"] == 2


def test_per_tick_reap_skipped_during_drain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """During drain (SIGUSR1) the per-tick reaper must not fire — the
    operator's intent is "finish what's running"; reaping a still-
    alive in-flight task during drain would terminate the thread the
    drain is waiting for and race the drain-complete check."""
    qd = _queue(tmp_path)
    test_lock = tmp_path / "test_global.lock"
    monkeypatch.setattr(
        "claude_task_runner.supervisor.pidfile.global_lock_path",
        lambda: test_lock,
    )
    _stub_orchestrator(monkeypatch)

    call_count = {"n": 0}

    def fake_reap(queue_dir, in_flight_task_ids, *, settings, clock, sigterm_fn=None):
        call_count["n"] += 1
        return []

    monkeypatch.setattr(rs_mod, "reap_silent_orphans_tick", fake_reap)

    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    settings = load_settings(None)
    source = FakeUsageSource([_reading(now)] * 5)

    # Pre-install the drain flag by raising SIGUSR1 to ourselves
    # immediately after start_daemon installs the signal handler. We
    # can't do that cleanly without threads; instead, monkeypatch
    # signal.signal so SIGUSR1's handler is invoked synchronously
    # right after registration.
    original_signal = signal.signal

    def signal_then_raise(signum, handler):
        original_signal(signum, handler)
        if signum == signal.SIGUSR1:
            # Invoke the handler in-process so the drain flag flips
            # before the loop starts ticking.
            handler(signum, None)

    monkeypatch.setattr(signal, "signal", signal_then_raise)

    start_daemon(
        queue_dir=qd,
        settings=settings,
        source=source,
        pending_count_fn=lambda: 0,
        # When draining + in_flight=0, the daemon exits cleanly after
        # the first tick. The per-tick reaper is gated on
        # `not draining`, so even on that single tick it should not
        # have fired.
        in_flight_count_fn=lambda: 0,
        clock=FakeClock(now),
        install_signal_handlers=True,
        max_ticks=5,
    )

    assert call_count["n"] == 0
