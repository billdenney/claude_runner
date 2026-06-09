"""Integration: ``start_daemon`` runs the silent reaper at startup.

Covers the wiring (not the reaper's own logic — that's in
:mod:`tests.unit.test_reconcile_silent`):

* The reaper runs before ``reconcile_orphans``, so a silent in-flight
  task lands at ``possibly_hung`` (the reaper's outcome) rather than
  ``failed`` with the broad-demotion ``stop_reason``.
* ``notify_callback`` and ``event_callback`` receive one entry per
  reaped task so operators see the action in real time, not just on
  log inspection.

Uses ``max_ticks=0`` so the daemon runs its startup pass and exits
without entering the tick loop — keeps the test fast and avoids
needing a believable :class:`UsageSource` for any actual ticks.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.queue.schema import TaskState
from claude_task_runner.queue.store import (
    load_state,
    queue_runtime_dir,
    state_path_for,
    todo_dir,
    write_state_atomic,
)
from claude_task_runner.supervisor.daemon import start_daemon
from claude_task_runner.supervisor.reconcile import ORPHAN_STOP_REASON
from claude_task_runner.supervisor.reconcile_silent import SILENT_STOP_REASON
from claude_task_runner.usage.models import UsageReading, WindowReading
from claude_task_runner.usage.source import FakeUsageSource


def _queue(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


def _reading() -> UsageReading:
    return UsageReading(
        captured_at=datetime(2026, 6, 9, 12, 0, tzinfo=UTC),
        five_hour=WindowReading(
            utilization_pct=10,
            resets_at_raw="x",
            resets_at=datetime(2026, 6, 9, 17, 0, tzinfo=UTC),
        ),
        seven_day=WindowReading(
            utilization_pct=10,
            resets_at_raw="x",
            resets_at=datetime(2026, 6, 16, 12, 0, tzinfo=UTC),
        ),
    )


def test_start_daemon_runs_silent_reaper_before_reconcile_orphans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qd = _queue(tmp_path)

    # Redirect the host-wide lock to the tmp dir so the test isn't
    # blocked by a real supervisor.
    test_lock = tmp_path / "test_global.lock"
    monkeypatch.setattr(
        "claude_task_runner.supervisor.pidfile.global_lock_path",
        lambda: test_lock,
    )

    now = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)
    # Silent-running task: dispatch started 10 minutes ago, alert
    # window is 300s (default). Should be flipped to possibly_hung
    # by the reaper. If the reaper didn't run (or ran AFTER
    # reconcile_orphans), this would end up at "failed" instead.
    silent_state = TaskState(
        task_id="t-silent",
        status="running",
        last_started_at=now - timedelta(seconds=600),
        pid=99999,
        session_id="sess-silent",
    )
    write_state_atomic(silent_state, state_path_for(qd, "t-silent"))

    settings = load_settings(None)
    source = FakeUsageSource([_reading()])
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
        max_ticks=0,
    )

    reloaded = load_state(state_path_for(qd, "t-silent"))
    # If the reaper ran FIRST: possibly_hung with SILENT_STOP_REASON.
    # If reconcile_orphans ran first (the regression we're guarding
    # against): "failed" with ORPHAN_STOP_REASON.
    assert reloaded.status == "possibly_hung"
    assert reloaded.stop_reason == SILENT_STOP_REASON
    assert reloaded.stop_reason != ORPHAN_STOP_REASON
    # pid cleared on demotion.
    assert reloaded.pid is None

    # The daemon surfaced the reap via both callbacks.
    assert any("silent-orphan task t-silent" in msg for _level, msg in notifications)
    assert any(
        kind == "silent_orphan_reaped" and payload.get("task_id") == "t-silent"
        for kind, payload in events
    )
