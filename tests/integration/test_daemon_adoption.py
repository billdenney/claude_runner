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
  worker is demoted (kill-switch restores legacy behaviour);
* a worker that exited while no supervisor ran, leaving a terminal
  ``result`` event in its log, is recorded from the log (the 2026-09-26
  restart incident) whatever its heartbeat age; one that left no result
  event is still demoted, as before.

``adopt_worker`` is stubbed to a no-op so the daemon's adoption monitor
thread doesn't run a real tail loop; ``_pid_alive`` is stubbed per test.
``max_ticks=0`` runs only the startup sequence then exits.
"""

from __future__ import annotations

import threading
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
from claude_task_runner.supervisor import persistence as persist_mod
from claude_task_runner.supervisor.daemon import start_daemon
from claude_task_runner.supervisor.reconcile import ORPHAN_STOP_REASON
from claude_task_runner.supervisor.states import InFlightRecord
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


# ---------------------------------------------------------------------------
# A worker that exited while no supervisor ran (2026-09-26 restart incident)
# ---------------------------------------------------------------------------

_EXITED_PID = 4101117
_INIT = '{"type":"system","subtype":"init","session_id":"sess-incident"}'
_ASSISTANT = (
    '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}],'
    '"usage":{"input_tokens":60,"output_tokens":40}}}'
)
_SUCCESS = (
    '{"type":"result","subtype":"success","stop_reason":"end_turn","is_error":false,'
    '"num_turns":69,"total_cost_usd":1.25,"duration_ms":1234,'
    '"usage":{"input_tokens":120,"output_tokens":80}}'
)
_ERROR = (
    '{"type":"result","subtype":"error_during_execution","stop_reason":"error_during_execution",'
    '"is_error":true,"total_cost_usd":0.5,"duration_ms":1234,'
    '"usage":{"input_tokens":120,"output_tokens":80}}'
)


def _seed_exited(qd: Path, task_id: str, lines: list[str], *, heartbeat_age: timedelta) -> Path:
    """A first attempt whose worker wrote ``lines`` and exited after the old
    supervisor stopped: ``running`` on disk, a dead pid and no session id
    (a run records its session only when it finalizes). The old
    supervisor's snapshot still lists the task in flight on ``default``."""
    write_task_atomic(
        Task(id=task_id, title="t", prompt="p", working_dir=None),
        qd / "todo" / f"{task_id}.yaml",
    )
    log = qd / ".claude_task_runner" / "logs" / task_id / "attempt-1.stream.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(line + "\n" for line in lines))
    started = _NOW - timedelta(hours=2)
    state = TaskState(
        task_id=task_id,
        status="running",
        attempts=1,
        last_started_at=started,
        last_heartbeat_at=_NOW - heartbeat_age,
        pid=_EXITED_PID,
        log_path=str(log),
    )
    write_state_atomic(state, state_path_for(qd, task_id))
    snapshot = persist_mod.initial_snapshot(since=started, account_names=["default"])
    persist_mod.write_atomic(
        snapshot.model_copy(
            update={
                "in_flight": [
                    InFlightRecord(task_id=task_id, account="default", started_at=started)
                ],
                "in_flight_task_ids": [task_id],
            }
        ),
        persist_mod.supervisor_state_path(qd),
    )
    return log


@pytest.mark.parametrize(
    "heartbeat_age",
    [
        # The incident: the new supervisor started ~70 s after the old one
        # stopped, inside the 300 s alert window, so reconcile_orphans
        # demoted the finished task to failed for a re-dispatch.
        pytest.param(timedelta(seconds=70), id="restart-inside-alert-window"),
        # A longer gap: the silent-orphan reaper would park the finished
        # task possibly_hung for the operator instead.
        pytest.param(timedelta(hours=1), id="restart-past-alert-window"),
    ],
)
def test_daemon_records_worker_that_exited_during_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, heartbeat_age: timedelta
) -> None:
    qd = _queue(tmp_path)
    log = _seed_exited(qd, "t-exited", [_INIT, _ASSISTANT, _SUCCESS], heartbeat_age=heartbeat_age)
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(
        dispatcher_mod,
        "adopt_worker",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("an exited worker is not adopted")),
    )

    notifications, events = _run_daemon(qd, _settings(adopt=True), monkeypatch)

    reloaded = load_state(state_path_for(qd, "t-exited"))
    assert reloaded.status == "completed"
    assert reloaded.stop_reason == "end_turn"
    assert reloaded.error is None
    assert len(reloaded.runs) == 1
    run = reloaded.runs[0]
    assert run.attempt == 1
    assert run.stop_reason == "end_turn"
    assert run.error is None
    assert run.cost_usd == pytest.approx(1.25)
    assert run.account == "default"
    assert run.pid == _EXITED_PID
    assert reloaded.session_id == "sess-incident"
    assert reloaded.session_account == "default"
    assert reloaded.pid is None
    assert reloaded.log_path is None
    assert [(kind, payload) for kind, payload in events if kind == "exited_worker_finalized"] == [
        (
            "exited_worker_finalized",
            {
                "task_id": "t-exited",
                "pid": _EXITED_PID,
                "log_path": str(log),
                "status": "completed",
                "stop_reason": "end_turn",
            },
        )
    ]
    assert not [kind for kind, _ in events if kind in ("silent_orphan_reaped", "worker_adopted")]
    assert (
        "info",
        f"recorded exited worker for task t-exited from its log (pid={_EXITED_PID}): "
        "status=completed stop_reason=end_turn",
    ) in notifications


def test_daemon_records_exited_worker_error_result_as_failed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error result is a real, finished failure: it gets a RunRecord and
    the result's stop_reason, not the orphan demotion's."""
    qd = _queue(tmp_path)
    _seed_exited(qd, "t-error", [_INIT, _ASSISTANT, _ERROR], heartbeat_age=timedelta(seconds=70))
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: False)

    _notifications, events = _run_daemon(qd, _settings(adopt=True), monkeypatch)

    reloaded = load_state(state_path_for(qd, "t-error"))
    assert reloaded.status == "failed"
    assert reloaded.stop_reason == "error_during_execution"
    assert [r.stop_reason for r in reloaded.runs] == ["error_during_execution"]
    assert reloaded.session_id == "sess-incident"
    assert [p["status"] for kind, p in events if kind == "exited_worker_finalized"] == ["failed"]


def test_daemon_still_demotes_exited_worker_without_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead worker whose log stops without a result event crashed. It is
    demoted exactly as before the fix: failed with the orphan stop_reason,
    no RunRecord, the (unset) session id untouched, nothing reported as
    recorded."""
    qd = _queue(tmp_path)
    _seed_exited(qd, "t-crashed", [_INIT, _ASSISTANT], heartbeat_age=timedelta(seconds=70))
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: False)

    _notifications, events = _run_daemon(qd, _settings(adopt=True), monkeypatch)

    reloaded = load_state(state_path_for(qd, "t-crashed"))
    assert reloaded.status == "failed"
    assert reloaded.stop_reason == ORPHAN_STOP_REASON
    assert reloaded.error is None
    assert reloaded.runs == []
    assert reloaded.session_id is None
    assert not [kind for kind, _ in events if kind == "exited_worker_finalized"]


def test_daemon_kill_switch_leaves_exited_worker_to_legacy_demotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With adoption OFF the exited-worker pass is off too: even a finished
    log gets the legacy demote-on-restart."""
    qd = _queue(tmp_path)
    _seed_exited(qd, "t-off", [_INIT, _ASSISTANT, _SUCCESS], heartbeat_age=timedelta(seconds=70))
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: False)

    _notifications, events = _run_daemon(qd, _settings(adopt=False), monkeypatch)

    reloaded = load_state(state_path_for(qd, "t-off"))
    assert reloaded.status == "failed"
    assert reloaded.stop_reason == ORPHAN_STOP_REASON
    assert reloaded.runs == []
    assert not [kind for kind, _ in events if kind == "exited_worker_finalized"]


def test_daemon_gives_both_finalize_paths_the_dispatch_and_hook_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Startup hands the queue's ``[dispatch]`` and ``[hooks]`` settings to the
    exited-worker finalize and to the adoption monitor. Both finalize through
    the owned path's gates, which need them: the terminal-close gate reads
    the block file, and the post-dispatch hook its command."""
    qd = _queue(tmp_path)
    _seed_running_filebacked(qd, "t-live")
    _seed_exited(qd, "t-exited", [_INIT, _ASSISTANT, _SUCCESS], heartbeat_age=timedelta(seconds=70))
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda pid: pid != _EXITED_PID)
    calls: dict[str, dict[str, object]] = {}
    adopted = threading.Event()

    def _record_exited(**kwargs: object) -> None:
        calls["exited"] = kwargs

    def _record_adopted(**kwargs: object) -> None:
        calls["adopted"] = kwargs
        adopted.set()

    monkeypatch.setattr(dispatcher_mod, "finalize_exited_worker", _record_exited)
    monkeypatch.setattr(dispatcher_mod, "adopt_worker", _record_adopted)
    settings = _settings(adopt=True)

    _run_daemon(qd, settings, monkeypatch)

    assert adopted.wait(timeout=5), "the adoption monitor never called adopt_worker"
    assert sorted(calls) == ["adopted", "exited"]
    for path in ("exited", "adopted"):
        assert calls[path]["settings_dispatch"] is settings.dispatch, path
        assert calls[path]["settings_hooks"] is settings.hooks, path
