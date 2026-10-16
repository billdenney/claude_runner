"""A task whose worker still runs is never dispatched beside it.

Two workers in one worktree corrupt its branch. Several paths can leave a
task dispatchable while its worker runs: a state set ``failed`` with the
pid still on record, a worker whose pid was never written
(``UNTRACKED-PID``), a running task's state lost to the corrupt-state
quarantine. The consumers refuse such a task whatever produced it: the
selector and the pre-spawn backstop in ``_dispatch_one_safely`` park it
with its worker on record, and ``queue force-dispatch`` refuses it unless
given ``--allow-live-worker``. The producers that can do so (startup
reconciliation, the quarantine) park it themselves.

A worker is found by its recorded pid (``probe_recorded_worker``) or, when
no state records it, by its stdout being one of the task's stream logs
(``find_live_log_writer``).
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.loader import load_defaults
from claude_task_runner.config.schema import (
    AccountConcurrencyPolicy,
    AccountPolicy,
    AccountSettings,
    ResolvedAccount,
)
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
from claude_task_runner.runner import dispatcher as dispatcher_mod
from claude_task_runner.runner import force_dispatch as fd_mod
from claude_task_runner.runner import orchestrator as orch_mod
from claude_task_runner.runner.dispatcher import (
    LIVE_WORKER_STOP_REASON,
    find_live_log_writer,
    task_logs_dir,
)
from claude_task_runner.runner.in_flight import DispatchSlot
from claude_task_runner.supervisor.reconcile import ParkedWorker, reconcile_orphans
from claude_task_runner.supervisor.reconcile_corrupt import quarantine_corrupt_state_files
from claude_task_runner.supervisor.states import (
    AccountState,
    SupervisorSnapshot,
    SupervisorState,
)

from ._worker_probe_stubs import fake_proc

_NOW = datetime(2026, 10, 16, 14, 0, tzinfo=UTC)
_PID = 4_400_009
_TASK = "t-live"


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    write_task_atomic(Task(id=_TASK, title="t", prompt="p"), task_path_for(qd, _TASK))
    return qd


def _log(queue_dir: Path, attempt: int = 1) -> Path:
    log = task_logs_dir(queue_dir, _TASK) / f"attempt-{attempt}.stream.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"system","subtype":"init","session_id":"s"}\n')
    return log


@pytest.fixture
def live_worker(queue_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``_PID`` runs with the task's attempt-1 log as its stdout; returns the log."""
    log = _log(queue_dir)
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda pid: pid == _PID)
    monkeypatch.setattr(
        dispatcher_mod,
        "_PROC_ROOT",
        fake_proc(
            tmp_path / "proc",
            _PID,
            started=_NOW - timedelta(minutes=30),
            boot=_NOW - timedelta(days=1),
            stdout=log,
        ),
    )
    return log


def _settings() -> Any:
    return SimpleNamespace(
        concurrency=SimpleNamespace(initial_concurrency=None, max_concurrency=None),
        task_caps=SimpleNamespace(),
        session=SimpleNamespace(),
        effort_levels=load_defaults()["effort_levels"],
        hooks=SimpleNamespace(),
        failure_classifier=None,
        claude=SimpleNamespace(executable="claude", config_dir=""),
        dispatch=SimpleNamespace(auto_detect_paths_in_prompt=False),
        accounts=[AccountSettings(name="default", config_dir="")],
    )


def _tick(queue_dir: Path) -> tuple[list[str], dict[str, DispatchSlot]]:
    """One orchestrator tick with the dispatcher stubbed; returns the task
    ids ``dispatcher.dispatch`` was called for, and the slots."""
    called: list[str] = []
    slots: dict[str, DispatchSlot] = {}
    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
        side_effect=lambda **kw: called.append(kw["task"].id),
    ):
        orch_mod.tick_dispatch(
            queue_dir=queue_dir,
            settings=_settings(),
            clock=FakeClock(_NOW),
            snapshot=SupervisorSnapshot(
                state=SupervisorState.DISPATCHING,
                since=_NOW,
                accounts={"default": AccountState(state=SupervisorState.DISPATCHING, since=_NOW)},
            ),
            in_flight_slots=slots,
            accounts=[
                ResolvedAccount(
                    name="default",
                    config_dir="",
                    linux_user=None,
                    policy=AccountPolicy(concurrency=AccountConcurrencyPolicy(max_concurrency=4)),
                )
            ],
        )
        for slot in slots.values():
            slot.thread.join(timeout=5)
    return called, slots


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def _assert_parked(queue_dir: Path, log: Path) -> TaskState:
    state = load_state(state_path_for(queue_dir, _TASK))
    assert (state.status, state.stop_reason, state.pid, state.log_path) == (
        "possibly_hung",
        LIVE_WORKER_STOP_REASON,
        _PID,
        str(log),
    )
    return state


# ---------------------------------------------------------------------------
# Finding a worker no state records
# ---------------------------------------------------------------------------


def test_finds_the_process_writing_the_tasks_log(queue_dir: Path, live_worker: Path) -> None:
    assert find_live_log_writer(task_logs_dir(queue_dir, _TASK)) == (_PID, str(live_worker))


def test_a_process_writing_another_tasks_log_is_not_this_tasks_worker(
    queue_dir: Path, live_worker: Path
) -> None:
    assert find_live_log_writer(task_logs_dir(queue_dir, "t-other")) is None


@pytest.mark.usefixtures("real_proc")
def test_finds_a_real_worker_by_its_log(queue_dir: Path) -> None:
    log = _log(queue_dir, attempt=2)
    with open(log, "ab") as out:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=out,
            start_new_session=True,
        )
    try:
        assert find_live_log_writer(task_logs_dir(queue_dir, _TASK)) == (child.pid, str(log))
    finally:
        child.kill()
        child.wait(timeout=10)
    assert find_live_log_writer(task_logs_dir(queue_dir, _TASK)) is None


# ---------------------------------------------------------------------------
# The selector and the pre-spawn backstop
# ---------------------------------------------------------------------------


def test_selector_parks_a_failed_task_whose_worker_still_runs(
    queue_dir: Path, live_worker: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A task set failed with its live worker's pid still on record (an
    older runner's restart, or any other producer) is not dispatched."""
    write_state_atomic(
        TaskState(task_id=_TASK, status="failed", pid=_PID, log_path=str(live_worker)),
        state_path_for(queue_dir, _TASK),
    )
    with caplog.at_level(logging.WARNING):
        called, slots = _tick(queue_dir)

    assert called == []
    assert slots == {}
    _assert_parked(queue_dir, live_worker)
    assert (
        f"refusing to dispatch task {_TASK}: its worker pid={_PID} is still running "
        f"(log={live_worker}); parked possibly_hung (stop_reason=worker_still_running) "
        "so it is not dispatched a second time"
    ) in _warnings(caplog)


@pytest.mark.parametrize(
    "state",
    [
        pytest.param(None, id="no-state"),
        pytest.param(TaskState(task_id=_TASK, status="pending"), id="pending"),
        pytest.param(
            TaskState(task_id=_TASK, status="failed", stop_reason="orphaned_by_supervisor_restart"),
            id="failed-without-pid",
        ),
    ],
)
def test_backstop_refuses_a_task_whose_unrecorded_worker_writes_its_log(
    queue_dir: Path, live_worker: Path, state: TaskState | None
) -> None:
    """No state records the worker (its pid was never written, or its state
    was lost), so the selector admits the task. The backstop finds the
    worker by its log, parks the task with it on record, and spawns nothing."""
    if state is not None:
        write_state_atomic(state, state_path_for(queue_dir, _TASK))

    called, _slots = _tick(queue_dir)

    assert called == []
    parked = _assert_parked(queue_dir, live_worker)
    assert parked.worker_start_ticks is not None


def test_backstop_dispatches_with_the_operators_override(
    queue_dir: Path, live_worker: Path
) -> None:
    write_state_atomic(
        TaskState(task_id=_TASK, status="failed", pid=_PID, log_path=str(live_worker)),
        state_path_for(queue_dir, _TASK),
    )
    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch", return_value=None
    ) as dispatch:
        orch_mod._dispatch_one_safely(
            Task(id=_TASK, title="t", prompt="p"),
            queue_dir,
            _settings(),
            FakeClock(_NOW),
            "claude",
            "",
            None,
            "default",
            allow_live_worker=True,
        )
    dispatch.assert_called_once()


def test_backstop_dispatches_when_no_worker_runs(queue_dir: Path, live_worker: Path) -> None:
    """The worker exited: nothing holds the task back."""
    with (
        patch.object(dispatcher_mod, "_pid_alive", lambda _pid: False),
        patch(
            "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch", return_value=None
        ) as dispatch,
    ):
        orch_mod._dispatch_one_safely(
            Task(id=_TASK, title="t", prompt="p"),
            queue_dir,
            _settings(),
            FakeClock(_NOW),
            "claude",
            "",
            None,
            "default",
        )
    dispatch.assert_called_once()


# ---------------------------------------------------------------------------
# Producers: startup reconciliation and the corrupt-state quarantine
# ---------------------------------------------------------------------------


def test_untracked_worker_is_parked_at_restart_and_not_dispatched(
    queue_dir: Path, live_worker: Path
) -> None:
    """``UNTRACKED-PID``: the running state has no pid because writing it
    failed. reconcile_orphans finds the worker by its log and parks the
    task with that pid instead of setting it failed; the next tick
    dispatches nothing."""
    write_state_atomic(
        TaskState(task_id=_TASK, status="running", attempts=1, last_started_at=_NOW),
        state_path_for(queue_dir, _TASK),
    )

    _snap, demoted, parked = reconcile_orphans(
        queue_dir, SupervisorSnapshot(state=SupervisorState.IDLE, since=_NOW)
    )
    called, _slots = _tick(queue_dir)

    assert demoted == []
    assert parked == [ParkedWorker(_TASK, _PID, str(live_worker))]
    assert called == []
    _assert_parked(queue_dir, live_worker)


def _corrupt(queue_dir: Path, head: str) -> None:
    """A running task's state file whose head is readable and whose tail is
    garbage, as a write cut short leaves it."""
    state_path_for(queue_dir, _TASK).write_text(head + "runs:\n- [unterminated\n  ]: {\n")


def test_quarantine_finds_a_worker_by_its_salvaged_pid_alone(
    queue_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another Linux user's worker: its stdout cannot be read, so no search
    of the logs finds it. The pid and start time read from the corrupt
    file's head do, and the task is parked with that worker."""
    log = _log(queue_dir)
    started = _NOW - timedelta(minutes=30)
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda pid: pid == _PID)
    monkeypatch.setattr(
        dispatcher_mod,
        "_PROC_ROOT",
        fake_proc(tmp_path / "proc", _PID, started=started, boot=_NOW - timedelta(days=1)),
    )
    _corrupt(
        queue_dir,
        f"task_id: {_TASK}\nstatus: running\nlast_started_at: '{started.isoformat()}'\n"
        f"pid: {_PID}\nlog_path: {log}\n",
    )

    results = quarantine_corrupt_state_files(queue_dir, now=_NOW)

    assert [(r.task_id, r.preserved_status) for r in results] == [(_TASK, "possibly_hung")]
    _assert_parked(queue_dir, log)


@pytest.mark.parametrize("pid_on_record", [True, False])
def test_quarantine_parks_a_task_whose_worker_still_runs(
    queue_dir: Path, live_worker: Path, pid_on_record: bool
) -> None:
    """The quarantine would revert the task to pending. Its worker still
    runs, found by the pid in the readable head or, without one, by its
    log, so the task is parked with that worker instead, and the next tick
    dispatches nothing."""
    head = f"schema_version: 2\ntask_id: {_TASK}\nstatus: running\nattempts: 1\n"
    if pid_on_record:
        head += f"pid: {_PID}\nlog_path: {live_worker}\n"
    _corrupt(queue_dir, head)

    results = quarantine_corrupt_state_files(queue_dir, now=_NOW)
    called, _slots = _tick(queue_dir)

    assert [(r.task_id, r.preserved_status) for r in results] == [(_TASK, "possibly_hung")]
    assert called == []
    parked = _assert_parked(queue_dir, live_worker)
    assert parked.error == (
        f"worker pid={_PID} is still running and its state file was unreadable; "
        "parked so the task is not dispatched again while it runs"
    )


def test_quarantine_still_reverts_a_task_whose_worker_is_gone(
    queue_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: False)
    _corrupt(queue_dir, f"task_id: {_TASK}\nstatus: running\npid: {_PID}\n")

    results = quarantine_corrupt_state_files(queue_dir, now=_NOW)

    assert [(r.task_id, r.preserved_status) for r in results] == [(_TASK, None)]
    assert not state_path_for(queue_dir, _TASK).exists()


# ---------------------------------------------------------------------------
# queue force-dispatch
# ---------------------------------------------------------------------------


def test_force_dispatch_cli_refuses_a_live_worker(queue_dir: Path, live_worker: Path) -> None:
    from claude_task_runner.cli.queue_cmd import app

    write_state_atomic(
        TaskState(task_id=_TASK, status="failed", pid=_PID, log_path=str(live_worker)),
        state_path_for(queue_dir, _TASK),
    )
    with (
        patch("claude_task_runner.cli.queue_cmd._supervisor_is_alive", return_value=False),
        patch("claude_task_runner.cli.queue_cmd.fd_mod.dispatch_synchronously") as sync,
    ):
        result = CliRunner().invoke(
            app, ["force-dispatch", _TASK, "--queue", str(queue_dir), "--json"]
        )

    assert result.exit_code == 2
    assert json.loads(result.stdout) == {
        "ok": False,
        "error": (
            f"task {_TASK}: its worker pid={_PID} is still running (log={live_worker}); "
            "a second worker would share its worktree. Pass --allow-live-worker to "
            "dispatch anyway"
        ),
        "pid": _PID,
    }
    sync.assert_not_called()


def test_force_dispatch_cli_override_reaches_both_paths(queue_dir: Path, live_worker: Path) -> None:
    from claude_task_runner.cli.queue_cmd import app

    write_state_atomic(
        TaskState(task_id=_TASK, status="failed", pid=_PID, log_path=str(live_worker)),
        state_path_for(queue_dir, _TASK),
    )
    done = TaskState(task_id=_TASK, status="completed", attempts=1, stop_reason="end_turn")
    with (
        patch("claude_task_runner.cli.queue_cmd._supervisor_is_alive", return_value=False),
        patch(
            "claude_task_runner.cli.queue_cmd.fd_mod.dispatch_synchronously", return_value=done
        ) as sync,
    ):
        result = CliRunner().invoke(
            app,
            ["force-dispatch", _TASK, "--queue", str(queue_dir), "--allow-live-worker", "--json"],
        )
    assert result.exit_code == 0, result.stdout
    assert sync.call_args.kwargs["allow_live_worker"] is True

    with patch("claude_task_runner.cli.queue_cmd._supervisor_is_alive", return_value=True):
        result = CliRunner().invoke(
            app,
            [
                "force-dispatch",
                _TASK,
                "--queue",
                str(queue_dir),
                "--allow-live-worker",
                "--wait-seconds",
                "0",
            ],
        )
    assert result.exit_code == 0, result.stdout
    assert [r.allow_live_worker for r in fd_mod.list_requests(queue_dir)] == [True]


@pytest.mark.parametrize("allow", [False, True])
def test_supervisor_honours_a_force_request_only_with_the_override(
    queue_dir: Path, live_worker: Path, allow: bool
) -> None:
    write_state_atomic(
        TaskState(task_id=_TASK, status="failed", pid=_PID, log_path=str(live_worker)),
        state_path_for(queue_dir, _TASK),
    )
    fd_mod.write_request(queue_dir, _TASK, allow_live_worker=allow)
    slots: dict[str, DispatchSlot] = {}
    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch", return_value=None
    ) as dispatch:
        n = fd_mod.tick_consume(
            queue_dir=queue_dir, settings=_settings(), clock=FakeClock(_NOW), in_flight_slots=slots
        )
        for slot in slots.values():
            slot.thread.join(timeout=5)

    assert not fd_mod.request_path(queue_dir, _TASK).exists()
    assert n == (1 if allow else 0)
    assert dispatch.call_count == (1 if allow else 0)


def test_synchronous_force_dispatch_refuses_a_live_worker(
    queue_dir: Path, live_worker: Path
) -> None:
    write_state_atomic(
        TaskState(task_id=_TASK, status="failed", pid=_PID, log_path=str(live_worker)),
        state_path_for(queue_dir, _TASK),
    )
    with pytest.raises(fd_mod.ForceDispatchError, match=f"its worker pid={_PID} is still running"):
        fd_mod.dispatch_synchronously(
            task_id=_TASK, queue_dir=queue_dir, settings=_settings(), clock=FakeClock(_NOW)
        )
