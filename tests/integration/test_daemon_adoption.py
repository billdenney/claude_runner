"""Integration: ``start_daemon`` adopts live workers at startup (ADR-0025).

Covers the wiring (not ``adopt_worker``'s own logic — that's in
``tests/unit/test_dispatcher_adopt.py`` and
``tests/integration/test_adoption_e2e.py``):

* a HEALTHY, live-pid, file-backed running task is adopted at startup —
  it stays ``running`` (NOT demoted by ``reconcile_orphans``), the daemon
  emits a ``worker_adopted`` event/notify, and a slot is left in flight;
* a running task that is NOT adoptable (here: a dead pid) is still
  demoted by ``reconcile_orphans`` to ``failed`` — the legacy recovery;
* with ``[supervisor].adopt_workers`` off, even a perfectly-adoptable
  worker is demoted (kill-switch restores legacy behaviour).

``adopt_worker`` is stubbed to a no-op so the daemon's adoption monitor
thread doesn't run a real tail loop; ``_pid_alive`` is stubbed per test.
``max_ticks=0`` runs only the startup sequence then exits.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.config.schema import Settings
from claude_task_runner.queue.schema import Task, TaskState
from claude_task_runner.queue.store import (
    load_state,
    queue_runtime_dir,
    state_path_for,
    todo_dir,
    write_state_atomic,
    write_task_atomic,
)
from claude_task_runner.runner import dispatcher as dispatcher_mod
from claude_task_runner.supervisor.daemon import start_daemon
from claude_task_runner.supervisor.reconcile import ORPHAN_STOP_REASON
from claude_task_runner.usage.models import UsageReading, WindowReading
from claude_task_runner.usage.source import FakeUsageSource

_NOW = datetime(2026, 6, 13, 12, 0, tzinfo=UTC)


def _queue(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


def _settings(*, adopt: bool = True) -> Settings:
    base = load_settings(None)
    return base.model_copy(
        update={"supervisor": base.supervisor.model_copy(update={"adopt_workers": adopt})}
    )


def _reading() -> UsageReading:
    return UsageReading(
        captured_at=_NOW,
        five_hour=WindowReading(
            utilization_pct=10, resets_at_raw="x", resets_at=_NOW + timedelta(hours=5)
        ),
        seven_day=WindowReading(
            utilization_pct=10, resets_at_raw="x", resets_at=_NOW + timedelta(days=7)
        ),
    )


def _seed_running_filebacked(qd: Path, task_id: str) -> None:
    write_task_atomic(
        Task(id=task_id, title="t", prompt="p", working_dir=None),
        qd / "todo" / f"{task_id}.yaml",
    )
    log = qd / ".claude_task_runner" / "logs" / task_id / "attempt-1.stream.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"system","subtype":"init","session_id":"s"}\n')
    state = TaskState(
        task_id=task_id,
        status="running",
        last_started_at=_NOW - timedelta(seconds=120),
        last_heartbeat_at=_NOW - timedelta(seconds=10),  # HEALTHY
        pid=4321,
        log_path=str(log),
        session_id="sess-x",
    )
    write_state_atomic(state, state_path_for(qd, task_id))


def _run_daemon(
    qd: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[tuple[str, str]], list[tuple[str, dict[str, object]]]]:
    test_lock = qd.parent / "test_global.lock"
    monkeypatch.setattr("claude_task_runner.supervisor.pidfile.global_lock_path", lambda: test_lock)
    notifications: list[tuple[str, str]] = []
    events: list[tuple[str, dict[str, object]]] = []
    start_daemon(
        queue_dir=qd,
        settings=settings,
        source=FakeUsageSource([_reading()]),
        pending_count_fn=lambda: 0,
        in_flight_count_fn=lambda: 0,
        clock=FakeClock(_NOW),
        notify_callback=lambda level, msg: notifications.append((level, msg)),
        event_callback=lambda kind, payload: events.append((kind, payload)),
        install_signal_handlers=False,
        max_ticks=0,
    )
    return notifications, events


def test_daemon_adopts_healthy_worker_and_shields_from_demotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qd = _queue(tmp_path)
    _seed_running_filebacked(qd, "t-live")

    # Stub the monitor body + liveness so no real tail runs.
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(dispatcher_mod, "adopt_worker", lambda **_kw: None)

    notifications, events = _run_daemon(qd, _settings(adopt=True), monkeypatch)

    # Adopted: state stays running (NOT demoted to failed by reconcile).
    reloaded = load_state(state_path_for(qd, "t-live"))
    assert reloaded.status == "running"
    assert reloaded.stop_reason != ORPHAN_STOP_REASON
    # The daemon surfaced the adoption.
    assert any(
        kind == "worker_adopted" and payload.get("task_id") == "t-live" for kind, payload in events
    )
    assert any("adopted running worker for task t-live" in msg for _lvl, msg in notifications)


def test_daemon_demotes_dead_pid_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A running task whose pid is gone is NOT adoptable; reconcile_orphans
    demotes it to failed for session-resume re-dispatch (legacy path)."""
    qd = _queue(tmp_path)
    _seed_running_filebacked(qd, "t-dead")
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(dispatcher_mod, "adopt_worker", lambda **_kw: None)

    _run_daemon(qd, _settings(adopt=True), monkeypatch)

    reloaded = load_state(state_path_for(qd, "t-dead"))
    assert reloaded.status == "failed"
    assert reloaded.stop_reason == ORPHAN_STOP_REASON


def test_daemon_kill_switch_demotes_even_adoptable_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With adoption OFF, a perfectly-adoptable worker is still demoted —
    the kill-switch restores the legacy demote-on-restart behaviour."""
    qd = _queue(tmp_path)
    _seed_running_filebacked(qd, "t-off")
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: True)
    # adopt_worker must never be called when the flag is off.
    monkeypatch.setattr(
        dispatcher_mod,
        "adopt_worker",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("adoption disabled")),
    )

    _run_daemon(qd, _settings(adopt=False), monkeypatch)

    reloaded = load_state(state_path_for(qd, "t-off"))
    assert reloaded.status == "failed"
    assert reloaded.stop_reason == ORPHAN_STOP_REASON
