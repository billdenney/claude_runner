"""Integration: ``start_daemon`` adopts live workers at startup (ADR-0025).

Covers the wiring (not ``adopt_worker``'s own logic — that's in
``tests/unit/test_dispatcher_adopt.py`` and
``tests/integration/test_adoption_e2e.py``):

* a HEALTHY, live-pid, file-backed running task is adopted at startup —
  it stays ``running`` (NOT demoted by ``reconcile_orphans``), the daemon
  emits a ``worker_adopted`` event/notify, and a slot is left in flight;
* a running task that is NOT adoptable (here: a dead pid) is still
  demoted by ``reconcile_orphans`` to ``failed`` — the legacy recovery;
* with ``[supervisor].adopt_workers`` off, nothing is adopted: a dead
  worker's task is demoted (the legacy behaviour), and a live one's is
  parked ``possibly_hung``, so no second worker is dispatched beside it;
* a worker that exited while no supervisor ran, leaving a terminal
  ``result`` event in its log, is recorded from the log whatever its
  heartbeat age; one that left no result event is still demoted;
* against real worker processes and the host's ``/proc`` (the 2026-10-16
  restart incident): a worker in a long tool call, whose agent heartbeat
  is minutes old, is adopted and finalized from its log; one with no sign
  of life at all is parked, not failed, and left running; and a task whose
  pid now belongs to another process is demoted without signalling it.

``adopt_worker`` is stubbed to a no-op so the daemon's adoption monitor
thread doesn't run a real tail loop; ``_pid_alive`` is stubbed per test.
``max_ticks=0`` runs only the startup sequence then exits.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from signal_gate import SignalGate

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
from claude_task_runner.supervisor.reconcile import LIVE_WORKER_STOP_REASON, ORPHAN_STOP_REASON
from claude_task_runner.supervisor.states import InFlightRecord
from claude_task_runner.usage.models import UsageReading, WindowReading
from claude_task_runner.usage.source import FakeUsageSource

from ._file_worker import spawn_file_worker

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
    clock: FakeClock | None = None,
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
        clock=clock if clock is not None else FakeClock(_NOW),
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


@pytest.mark.parametrize(
    ("alive", "status", "stop_reason"),
    [
        (False, "failed", ORPHAN_STOP_REASON),
        (True, "possibly_hung", LIVE_WORKER_STOP_REASON),
    ],
)
def test_daemon_kill_switch_adopts_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alive: bool,
    status: str,
    stop_reason: str,
) -> None:
    """With adoption OFF nothing is adopted. A gone worker's task is demoted
    for re-dispatch, as before adoption existed; a live worker's is parked,
    since a failed task would be dispatched beside the worker."""
    qd = _queue(tmp_path)
    _seed_running_filebacked(qd, "t-off")
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: alive)
    # adopt_worker must never be called when the flag is off.
    monkeypatch.setattr(
        dispatcher_mod,
        "adopt_worker",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("adoption disabled")),
    )

    notifications, events = _run_daemon(qd, _settings(adopt=False), monkeypatch)

    reloaded = load_state(state_path_for(qd, "t-off"))
    assert reloaded.status == status
    assert reloaded.stop_reason == stop_reason
    # A park is reported to the operator; a demotion is not.
    parked_notes = [
        (lvl, msg) for lvl, msg in notifications if "still running and was not adopted" in msg
    ]
    parked_events = [(k, p) for k, p in events if k == "live_worker_parked"]
    if alive:
        assert parked_notes == [
            (
                "warning",
                "task t-off: worker pid=4321 is still running and was not adopted; "
                "parked possibly_hung, not dispatched again",
            )
        ]
        assert parked_events == [
            (
                "live_worker_parked",
                {"task_id": "t-off", "pid": 4321, "log_path": reloaded.log_path},
            )
        ]
    else:
        assert parked_notes == []
        assert parked_events == []


# ---------------------------------------------------------------------------
# A worker that exited while no supervisor ran
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
        # Inside the 300 s alert window, where reconcile_orphans would
        # demote an unrecorded task to failed for a re-dispatch.
        pytest.param(timedelta(seconds=70), id="restart-inside-alert-window"),
        # Past it, where the silent-orphan reaper would park it
        # possibly_hung for the operator.
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


# ---------------------------------------------------------------------------
# Real workers, real /proc (2026-10-16 restart incident)
# ---------------------------------------------------------------------------


def _seed_real_worker(qd: Path, task_id: str, *, pid: int, log: Path, now: datetime) -> None:
    """A running first attempt started 20 minutes ago whose agent heartbeat
    is 9 minutes old: the worker has been in one tool call since."""
    write_task_atomic(
        Task(id=task_id, title="t", prompt="p", working_dir=None),
        qd / "todo" / f"{task_id}.yaml",
    )
    state = TaskState(
        task_id=task_id,
        status="running",
        attempts=1,
        last_started_at=now - timedelta(minutes=20),
        last_heartbeat_at=now - timedelta(minutes=9),
        pid=pid,
        log_path=str(log),
        session_id="sess-real",
    )
    write_state_atomic(state, state_path_for(qd, task_id))


def _wait_for_lines(log: Path, count: int) -> None:
    deadline = time.monotonic() + 10
    while len(log.read_text().splitlines()) < count:
        assert time.monotonic() < deadline, f"{log} never reached {count} lines"
        time.sleep(0.05)


def _adoption_thread(task_id: str) -> threading.Thread:
    (thread,) = [t for t in threading.enumerate() if t.name == f"adopt-{task_id}"]
    return thread


@pytest.mark.slow
@pytest.mark.usefixtures("real_proc")
def test_daemon_adopts_real_worker_in_a_long_tool_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signal_gate: SignalGate
) -> None:
    """The incident: the old supervisor's monitor ticked seconds before the
    restart, the agent heartbeat is 9 minutes old, and the worker is busy
    in a tool call. The new supervisor adopts it, and the adoption monitor
    records its run once it finishes. It is never set failed, so it is
    never dispatched a second time."""
    qd = _queue(tmp_path)
    now = datetime.now(UTC)
    log = qd / ".claude_task_runner" / "logs" / "t-tool" / "attempt-1.stream.jsonl"
    # Long enough that a slow start-up still finds the worker running, so it
    # is adopted rather than recorded as exited.
    pid = spawn_file_worker(log, {"WORKER_SESSION_ID": "sess-real", "WORKER_HOLD_S": "10"})
    signal_gate.allow(pid)
    _wait_for_lines(log, 2)
    _seed_real_worker(qd, "t-tool", pid=pid, log=log, now=now)
    state = load_state(state_path_for(qd, "t-tool"))
    write_state_atomic(
        state.model_copy(update={"dispatcher_alive_at": now - timedelta(seconds=20)}),
        state_path_for(qd, "t-tool"),
    )

    _notifications, events = _run_daemon(qd, _settings(), monkeypatch, clock=FakeClock(now))

    assert ("worker_adopted", {"task_id": "t-tool", "pid": pid, "log_path": str(log)}) in events
    assert load_state(state_path_for(qd, "t-tool")).status == "running"
    thread = _adoption_thread("t-tool")
    thread.join(timeout=40)
    assert not thread.is_alive()
    final = load_state(state_path_for(qd, "t-tool"))
    assert final.status == "completed"
    assert [r.stop_reason for r in final.runs] == ["end_turn"]
    assert final.pid is None


@pytest.mark.slow
@pytest.mark.usefixtures("real_proc")
def test_daemon_parks_real_worker_with_no_sign_of_life(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signal_gate: SignalGate,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Heartbeat, monitor tick and log write all 9 minutes old, but the
    worker runs. The startup reaper parks the task possibly_hung with the
    worker on record (worker_still_running), so it holds its slot and its
    run is recorded once it exits; it is not set failed, and the worker is
    not signalled."""
    qd = _queue(tmp_path)
    now = datetime.now(UTC)
    log = qd / ".claude_task_runner" / "logs" / "t-quiet" / "attempt-1.stream.jsonl"
    pid = spawn_file_worker(log, {"WORKER_SESSION_ID": "sess-real", "WORKER_HOLD_S": "60"})
    signal_gate.allow(pid)
    try:
        _wait_for_lines(log, 2)
        quiet_since = (now - timedelta(minutes=9)).timestamp()
        os.utime(log, (quiet_since, quiet_since))
        _seed_real_worker(qd, "t-quiet", pid=pid, log=log, now=now)
        state = load_state(state_path_for(qd, "t-quiet"))
        write_state_atomic(
            state.model_copy(update={"dispatcher_alive_at": now - timedelta(minutes=9)}),
            state_path_for(qd, "t-quiet"),
        )

        with caplog.at_level(logging.WARNING):
            _notifications, events = _run_daemon(qd, _settings(), monkeypatch, clock=FakeClock(now))

        parked = load_state(state_path_for(qd, "t-quiet"))
        assert (parked.status, parked.stop_reason, parked.pid) == (
            "possibly_hung",
            LIVE_WORKER_STOP_REASON,
            pid,
        )
        assert parked.error == (
            f"worker pid={pid} is still running but showed no sign of life within "
            "heartbeat_silence_alert_s=300 (agent heartbeat 540s ago, monitor tick 540s "
            "ago, worker log write 540s ago); parked so the task is not dispatched again "
            "while it runs"
        )
        assert not any(kind == "worker_adopted" for kind, _payload in events)
        assert (
            "claude_task_runner.supervisor.reconcile_silent",
            f"reaped silent orphan task t-quiet: verdict=silent silence=540s pid={pid} "
            "sigtermed=False new_status=possibly_hung stop_reason=worker_still_running",
        ) in [(r.name, r.getMessage()) for r in caplog.records if r.levelno == logging.WARNING]
        assert dispatcher_mod.probe_worker(pid, log_path=str(log), started_at=None) is (
            dispatcher_mod.WorkerProbe.ALIVE
        )
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)


@pytest.mark.usefixtures("real_proc")
def test_daemon_demotes_task_whose_pid_another_process_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live_worker_pid: int
) -> None:
    """The recorded pid is alive, but it is a process this attempt did not
    start: its stdout is not the attempt's log. The worker is gone, so the
    task is demoted for re-dispatch, and the other process is left alone."""
    qd = _queue(tmp_path)
    now = datetime.now(UTC)
    log = qd / ".claude_task_runner" / "logs" / "t-reused" / "attempt-1.stream.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text('{"type":"system","subtype":"init","session_id":"sess-real"}\n')
    _seed_real_worker(qd, "t-reused", pid=live_worker_pid, log=log, now=now)

    _run_daemon(qd, _settings(), monkeypatch, clock=FakeClock(now))

    demoted = load_state(state_path_for(qd, "t-reused"))
    assert (demoted.status, demoted.stop_reason) == ("failed", ORPHAN_STOP_REASON)
    assert dispatcher_mod._pid_alive(live_worker_pid)


@pytest.mark.parametrize("pid", [2**31, 10**20], ids=["2**31", "10**20"])
def test_daemon_starts_with_an_out_of_range_task_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pid: int
) -> None:
    """No process can have a pid outside the C ``pid_t`` range, and
    ``os.kill`` raises ``OverflowError`` (not ``OSError``) for one. A
    ``running`` state YAML recording such a pid must not crash supervisor
    startup: the corrupt-state quarantine (ADR-0028) takes it, and the
    task reverts to pending."""
    qd = _queue(tmp_path)
    _seed_exited(qd, "t-pid", [_INIT, _ASSISTANT, _SUCCESS], heartbeat_age=timedelta(seconds=70))
    path = state_path_for(qd, "t-pid")
    path.write_text(path.read_text().replace(f"pid: {_EXITED_PID}\n", f"pid: {pid}\n"))

    _notifications, events = _run_daemon(qd, _settings(adopt=True), monkeypatch)

    assert not path.exists()
    assert [
        (p["task_id"], p["preserved_status"]) for k, p in events if k == "corrupt_state_quarantined"
    ] == [("t-pid", None)]


def test_daemon_starts_with_a_worker_log_mtime_past_year_9999(
    far_future_mtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The startup silent-orphan reaper reads each running worker's log
    mtime as a sign of life. One no datetime can hold (past year 9999,
    possible on tmpfs) must not crash startup: it is no sign of life, and
    the task is handled on the others."""
    qd = _queue(far_future_mtime_dir)
    log = _seed_exited(qd, "t-mtime", [_INIT, _ASSISTANT], heartbeat_age=timedelta(seconds=70))
    os.utime(log, (253402300800, 253402300800))
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: False)

    _run_daemon(qd, _settings(adopt=True), monkeypatch)

    reloaded = load_state(state_path_for(qd, "t-mtime"))
    assert (reloaded.status, reloaded.stop_reason) == ("failed", ORPHAN_STOP_REASON)


def test_daemon_records_an_exited_worker_whose_log_mtime_is_past_year_9999(
    far_future_mtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exited-worker finalize dates the run by its log's last write. An
    mtime no datetime can hold dates it at now, like any mtime after now,
    and the finished run is still recorded rather than left for the
    demotion as a failed finalize."""
    qd = _queue(far_future_mtime_dir)
    log = _seed_exited(
        qd, "t-mtime-done", [_INIT, _ASSISTANT, _SUCCESS], heartbeat_age=timedelta(seconds=70)
    )
    os.utime(log, (253402300800, 253402300800))
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: False)

    _run_daemon(qd, _settings(adopt=True), monkeypatch)

    reloaded = load_state(state_path_for(qd, "t-mtime-done"))
    assert reloaded.status == "completed"
    assert [run.finished_at for run in reloaded.runs] == [_NOW]
