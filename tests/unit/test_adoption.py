"""Tests for startup worker adoption + reconcile shielding (ADR-0025).

``supervisor.adoption.adopt_running_workers`` runs at supervisor start,
before the demotion sweep. It re-attaches a monitor thread to each
still-running, file-backed, live-pid, HEALTHY worker and returns the
adopted task ids; those ids are then shielded from
``supervisor.reconcile.reconcile_orphans`` so it doesn't demote a live
worker.

These tests stub ``dispatcher._pid_alive`` (liveness) and
``dispatcher.adopt_worker`` (the monitor body) so no real process or
tail loop runs — the focus is the *selection* logic (who gets adopted)
and the *shielding* contract.

``supervisor.adoption.finalize_exited_workers`` runs before both, for
workers that exited while no supervisor ran. Its tests cover who it
finalizes from the log, who it leaves to the later passes, which account
the run is recorded under, and that it never clobbers a concurrent writer.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.config.schema import AccountSettings, Settings
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
from claude_task_runner.runner.in_flight import DispatchSlot
from claude_task_runner.supervisor.adoption import (
    ExitedWorkerResult,
    adopt_running_workers,
    finalize_exited_workers,
)
from claude_task_runner.supervisor.reconcile import reconcile_orphans
from claude_task_runner.supervisor.states import (
    InFlightRecord,
    SupervisorSnapshot,
    SupervisorState,
)


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
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


_NOW = datetime(2026, 6, 13, 12, 0, tzinfo=UTC)


def _seed_running(
    queue_dir: Path,
    task_id: str,
    *,
    pid: int | None,
    log_path: Path | None,
    started_at: datetime,
    last_heartbeat_at: datetime | None = None,
) -> None:
    # A Task YAML is needed so adoption can load it for adopt_worker.
    write_task_atomic(
        Task(id=task_id, title="t", prompt="p", working_dir=None),
        queue_dir / "todo" / f"{task_id}.yaml",
    )
    state = TaskState(
        task_id=task_id,
        status="running",
        last_started_at=started_at,
        last_heartbeat_at=last_heartbeat_at,
        pid=pid,
        log_path=str(log_path) if log_path is not None else None,
    )
    write_state_atomic(state, state_path_for(queue_dir, task_id))


def _make_log(queue_dir: Path, task_id: str) -> Path:
    log = queue_dir / ".claude_task_runner" / "logs" / task_id / "attempt-1.stream.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"system","subtype":"init","session_id":"s"}\n')
    return log


@pytest.fixture
def stub_worker(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub ``adopt_worker`` so adoption threads don't run a real monitor.

    Returns a list that records the task ids ``adopt_worker`` was called
    for (the monitor thread invokes it). ``_pid_alive`` defaults to True;
    individual tests override it.
    """
    called: list[str] = []
    done = threading.Event()

    def _fake_adopt(*, task: Task, **_kw: object) -> object:
        called.append(task.id)
        done.set()
        return None

    monkeypatch.setattr(dispatcher_mod, "adopt_worker", _fake_adopt)
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: True)
    return called


def test_adopts_healthy_live_filebacked_worker(
    queue_dir: Path, stub_worker: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A running task with a live pid, a present log_path, and a HEALTHY
    heartbeat verdict is adopted: a slot is registered and the monitor
    thread (stubbed) is launched."""
    log = _make_log(queue_dir, "100-healthy")
    # Heartbeat 10s ago, well within the 5-min alert window ⇒ HEALTHY.
    _seed_running(
        queue_dir,
        "100-healthy",
        pid=4321,
        log_path=log,
        started_at=_NOW - timedelta(seconds=120),
        last_heartbeat_at=_NOW - timedelta(seconds=10),
    )

    slots: dict[str, DispatchSlot] = {}
    results = adopt_running_workers(
        queue_dir, settings=_settings(), clock=FakeClock(_NOW), in_flight_slots=slots
    )

    assert [r.task_id for r in results] == ["100-healthy"]
    assert results[0].pid == 4321
    assert "100-healthy" in slots
    # The monitor thread eventually invokes the (stubbed) adopt_worker.
    slots["100-healthy"].thread.join(timeout=2)
    assert "100-healthy" in stub_worker


def test_dead_pid_not_adopted(queue_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A running task whose pid is gone is NOT adopted (left for the
    demotion sweep)."""
    log = _make_log(queue_dir, "101-dead")
    _seed_running(
        queue_dir,
        "101-dead",
        pid=4321,
        log_path=log,
        started_at=_NOW - timedelta(seconds=120),
        last_heartbeat_at=_NOW - timedelta(seconds=10),
    )
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: False)

    slots: dict[str, DispatchSlot] = {}
    results = adopt_running_workers(
        queue_dir, settings=_settings(), clock=FakeClock(_NOW), in_flight_slots=slots
    )
    assert results == []
    assert slots == {}


def test_missing_log_path_not_adopted(queue_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A running task with a live pid but no recorded log_path is NOT
    adopted — there is no stream to re-tail."""
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: True)
    _seed_running(
        queue_dir,
        "102-nolog",
        pid=4321,
        log_path=None,
        started_at=_NOW - timedelta(seconds=120),
        last_heartbeat_at=_NOW - timedelta(seconds=10),
    )
    slots: dict[str, DispatchSlot] = {}
    results = adopt_running_workers(
        queue_dir, settings=_settings(), clock=FakeClock(_NOW), in_flight_slots=slots
    )
    assert results == []
    assert slots == {}


def test_log_path_recorded_but_file_missing_not_adopted(
    queue_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A running task whose recorded log_path points at a file that does
    not exist (lost on disk) is NOT adopted — there is no stream to tail."""
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: True)
    ghost_log = queue_dir / ".claude_task_runner" / "logs" / "105-ghost" / "attempt-1.stream.jsonl"
    _seed_running(
        queue_dir,
        "105-ghost",
        pid=4321,
        log_path=ghost_log,  # path recorded but never created
        started_at=_NOW - timedelta(seconds=120),
        last_heartbeat_at=_NOW - timedelta(seconds=10),
    )
    slots: dict[str, DispatchSlot] = {}
    results = adopt_running_workers(
        queue_dir, settings=_settings(), clock=FakeClock(_NOW), in_flight_slots=slots
    )
    assert results == []
    assert slots == {}


def test_no_started_at_not_adopted(queue_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A running task with no ``last_started_at`` can't be heartbeat-graded
    and is deferred to the demotion sweep rather than adopted blind."""
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: True)
    log = _make_log(queue_dir, "106-nostart")
    _seed_running(
        queue_dir,
        "106-nostart",
        pid=4321,
        log_path=log,
        started_at=None,  # type: ignore[arg-type]
        last_heartbeat_at=None,
    )
    slots: dict[str, DispatchSlot] = {}
    results = adopt_running_workers(
        queue_dir, settings=_settings(), clock=FakeClock(_NOW), in_flight_slots=slots
    )
    assert results == []


def test_unparseable_task_yaml_not_adopted(
    queue_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A healthy live worker whose Task YAML can't be loaded is left for
    the demotion sweep (adopt_worker needs the Task for the output gate)."""
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: True)
    log = _make_log(queue_dir, "107-badtask")
    _seed_running(
        queue_dir,
        "107-badtask",
        pid=4321,
        log_path=log,
        started_at=_NOW - timedelta(seconds=120),
        last_heartbeat_at=_NOW - timedelta(seconds=10),
    )
    # Corrupt the Task YAML so load_task raises.
    (queue_dir / "todo" / "107-badtask.yaml").write_text("{not: valid: yaml: [")

    slots: dict[str, DispatchSlot] = {}
    results = adopt_running_workers(
        queue_dir, settings=_settings(), clock=FakeClock(_NOW), in_flight_slots=slots
    )
    assert results == []
    assert slots == {}


def test_silent_worker_not_adopted(queue_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A running task that is past the alert window (SILENT verdict) is NOT
    adopted even with a live pid + log — it's the reaper's job, and
    adoption must not grab a hung worker."""
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: True)
    log = _make_log(queue_dir, "103-silent")
    # started 1h ago, no heartbeat this attempt ⇒ silence 3600s > 300s alert.
    _seed_running(
        queue_dir,
        "103-silent",
        pid=4321,
        log_path=log,
        started_at=_NOW - timedelta(hours=1),
        last_heartbeat_at=None,
    )
    slots: dict[str, DispatchSlot] = {}
    results = adopt_running_workers(
        queue_dir, settings=_settings(), clock=FakeClock(_NOW), in_flight_slots=slots
    )
    assert results == []


def test_adoption_off_is_noop(queue_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With ``[supervisor].adopt_workers`` false, adoption never runs even
    for a perfectly-adoptable worker."""
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: True)
    log = _make_log(queue_dir, "104-off")
    _seed_running(
        queue_dir,
        "104-off",
        pid=4321,
        log_path=log,
        started_at=_NOW - timedelta(seconds=120),
        last_heartbeat_at=_NOW - timedelta(seconds=10),
    )
    slots: dict[str, DispatchSlot] = {}
    results = adopt_running_workers(
        queue_dir, settings=_settings(adopt=False), clock=FakeClock(_NOW), in_flight_slots=slots
    )
    assert results == []
    assert slots == {}


def _snapshot() -> SupervisorSnapshot:
    return SupervisorSnapshot(state=SupervisorState.IDLE, since=_NOW)


def test_reconcile_orphans_shields_adopted_ids(queue_dir: Path, live_worker_pid: int) -> None:
    """``reconcile_orphans`` must NOT demote a task whose id is in
    ``adopted_ids`` — that task has a live worker + monitor thread."""
    # Two running orphans; one is "adopted", one is not.
    _seed_running(
        queue_dir,
        "200-adopted",
        pid=live_worker_pid,
        log_path=_make_log(queue_dir, "200-adopted"),
        started_at=_NOW,
        last_heartbeat_at=_NOW,
    )
    _seed_running(
        queue_dir,
        "201-plain",
        pid=2,
        log_path=_make_log(queue_dir, "201-plain"),
        started_at=_NOW,
        last_heartbeat_at=_NOW,
    )

    _snap, demoted = reconcile_orphans(queue_dir, _snapshot(), adopted_ids={"200-adopted"})

    # Only the non-adopted orphan was demoted.
    assert demoted == ["201-plain"]
    assert load_state(state_path_for(queue_dir, "200-adopted")).status == "running"
    assert load_state(state_path_for(queue_dir, "201-plain")).status == "failed"


def test_reconcile_orphans_demotes_all_when_no_adopted(
    queue_dir: Path, live_worker_pid: int
) -> None:
    """Default (no adopted_ids): every running orphan is demoted — the
    historical behaviour is preserved bit-for-bit."""
    _seed_running(
        queue_dir,
        "300-a",
        pid=live_worker_pid,
        log_path=_make_log(queue_dir, "300-a"),
        started_at=_NOW,
        last_heartbeat_at=_NOW,
    )
    _snap, demoted = reconcile_orphans(queue_dir, _snapshot())
    assert demoted == ["300-a"]
    assert load_state(state_path_for(queue_dir, "300-a")).status == "failed"


# ---------------------------------------------------------------------------
# finalize_exited_workers: workers that exited while no supervisor ran
# ---------------------------------------------------------------------------

_WORKER_PID = 4321
_EXITED_STARTED = _NOW - timedelta(hours=2)


def _write_worker_log(queue_dir: Path, task_id: str, *, result: bool) -> Path:
    """Write the log a dead worker left: init, one assistant turn and, when
    ``result``, the terminal success result."""
    log = queue_dir / ".claude_task_runner" / "logs" / task_id / "attempt-1.stream.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        '{"type":"system","subtype":"init","session_id":"sess-log"}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}],'
        '"usage":{"input_tokens":60,"output_tokens":40}}}',
    ]
    if result:
        lines.append(
            '{"type":"result","subtype":"success","stop_reason":"end_turn","is_error":false,'
            '"total_cost_usd":0.07,"duration_ms":1234,'
            '"usage":{"input_tokens":120,"output_tokens":80}}'
        )
    log.write_text("".join(line + "\n" for line in lines))
    return log


def _seed_exited(
    queue_dir: Path,
    task_id: str,
    *,
    result: bool = True,
    session_id: str | None = None,
    session_account: str | None = None,
) -> TaskState:
    """Seed a ``running`` first attempt whose worker left a log and exited."""
    write_task_atomic(
        Task(id=task_id, title="t", prompt="p", working_dir=None),
        queue_dir / "todo" / f"{task_id}.yaml",
    )
    state = TaskState(
        task_id=task_id,
        status="running",
        attempts=1,
        last_started_at=_EXITED_STARTED,
        last_heartbeat_at=_NOW - timedelta(hours=1),
        pid=_WORKER_PID,
        log_path=str(_write_worker_log(queue_dir, task_id, result=result)),
        session_id=session_id,
        session_account=session_account,
    )
    write_state_atomic(state, state_path_for(queue_dir, task_id))
    return state


@pytest.fixture
def dead_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every recorded pid probes dead."""
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: False)


def _two_account_settings() -> Settings:
    base = _settings()
    return base.model_copy(
        update={
            "accounts": [
                AccountSettings(name="work", config_dir="~/.claude"),
                AccountSettings(name="personal", config_dir="~/.claude_personal"),
            ]
        }
    )


def test_finalize_exited_records_success_from_log(queue_dir: Path, dead_worker: None) -> None:
    """A running task whose worker is dead and whose log ends in a success
    result is finalized from the log: completed, one RunRecord, the log's
    session id, and pid / log_path cleared."""
    state = _seed_exited(queue_dir, "400-done")

    results = finalize_exited_workers(queue_dir, settings=_settings(), clock=FakeClock(_NOW))

    assert results == [
        ExitedWorkerResult(
            task_id="400-done",
            pid=_WORKER_PID,
            log_path=str(state.log_path),
            status="completed",
            stop_reason="end_turn",
        )
    ]
    reloaded = load_state(state_path_for(queue_dir, "400-done"))
    assert reloaded.status == "completed"
    assert reloaded.stop_reason == "end_turn"
    assert len(reloaded.runs) == 1
    assert reloaded.runs[0].stop_reason == "end_turn"
    assert reloaded.runs[0].pid == _WORKER_PID
    assert reloaded.session_id == "sess-log"
    assert reloaded.pid is None
    assert reloaded.log_path is None


def test_finalize_exited_leaves_crashed_worker(queue_dir: Path, dead_worker: None) -> None:
    """A dead worker whose log has no result event crashed. It is not
    finalized here: the state is left exactly as it was, for the reaper and
    reconcile_orphans."""
    state = _seed_exited(queue_dir, "401-crashed", result=False)

    results = finalize_exited_workers(queue_dir, settings=_settings(), clock=FakeClock(_NOW))

    assert results == []
    assert load_state(state_path_for(queue_dir, "401-crashed")) == state


def test_finalize_exited_leaves_live_worker(
    queue_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker whose pid is alive has not exited, even if its log already
    holds a result event (it may be exiting now). Adoption handles it."""
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: True)
    state = _seed_exited(queue_dir, "402-live")

    results = finalize_exited_workers(queue_dir, settings=_settings(), clock=FakeClock(_NOW))

    assert results == []
    assert load_state(state_path_for(queue_dir, "402-live")) == state


@pytest.mark.parametrize(
    "update",
    [
        pytest.param({"status": "possibly_hung"}, id="not-running"),
        pytest.param({"status": "failed"}, id="already-failed"),
        pytest.param({"pid": None}, id="no-pid"),
        pytest.param({"log_path": None}, id="no-log-path"),
        pytest.param({"log_path": "/nonexistent/attempt-1.stream.jsonl"}, id="log-file-missing"),
    ],
)
def test_finalize_exited_skips_ineligible_state(
    queue_dir: Path, dead_worker: None, update: dict[str, object]
) -> None:
    """Only a ``running`` state with a recorded pid and an existing log is a
    candidate. Anything else is left untouched even though the pid (if
    any) is dead and a finished log exists."""
    seeded = _seed_exited(queue_dir, "403-ineligible")
    state = seeded.model_copy(update=update)
    write_state_atomic(state, state_path_for(queue_dir, "403-ineligible"))

    results = finalize_exited_workers(queue_dir, settings=_settings(), clock=FakeClock(_NOW))

    assert results == []
    assert load_state(state_path_for(queue_dir, "403-ineligible")) == state


def test_finalize_exited_leaves_task_with_unloadable_yaml(
    queue_dir: Path, dead_worker: None
) -> None:
    """The finalize needs the Task for the output gate. A Task YAML that
    can't be loaded leaves the state for reconcile_orphans."""
    state = _seed_exited(queue_dir, "404-badtask")
    (queue_dir / "todo" / "404-badtask.yaml").write_text("{not: valid: yaml: [")

    results = finalize_exited_workers(queue_dir, settings=_settings(), clock=FakeClock(_NOW))

    assert results == []
    assert load_state(state_path_for(queue_dir, "404-badtask")) == state


def test_finalize_exited_skips_unparseable_state_file(queue_dir: Path, dead_worker: None) -> None:
    """An unparseable state file is skipped; the pass carries on to the
    next task rather than aborting the startup sequence."""
    state_path_for(queue_dir, "405-corrupt").write_text("{not: valid: yaml: [")
    _seed_exited(queue_dir, "406-done")

    results = finalize_exited_workers(queue_dir, settings=_settings(), clock=FakeClock(_NOW))

    assert [r.task_id for r in results] == ["406-done"]
    assert state_path_for(queue_dir, "405-corrupt").read_text() == "{not: valid: yaml: ["


def test_finalize_exited_failure_leaves_task_and_continues(
    queue_dir: Path, dead_worker: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finalize that raises for one task (a failed write, say) must not
    stop the supervisor from starting: that task's state is left for the
    later passes and the next task is still recorded."""
    broken = _seed_exited(queue_dir, "410-broken")
    _seed_exited(queue_dir, "411-done")
    real_finalize = dispatcher_mod.finalize_exited_worker

    def _finalize_or_raise(*, task: Task, **kwargs: object) -> object:
        if task.id == "410-broken":
            raise OSError("disk full")
        return real_finalize(task=task, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(dispatcher_mod, "finalize_exited_worker", _finalize_or_raise)

    results = finalize_exited_workers(queue_dir, settings=_settings(), clock=FakeClock(_NOW))

    assert [r.task_id for r in results] == ["411-done"]
    assert load_state(state_path_for(queue_dir, "410-broken")) == broken
    assert load_state(state_path_for(queue_dir, "411-done")).status == "completed"


def test_finalize_exited_off_is_noop(queue_dir: Path, dead_worker: None) -> None:
    """``[supervisor].adopt_workers = false`` turns this pass off with the
    rest of ADR-0025."""
    state = _seed_exited(queue_dir, "407-off")

    results = finalize_exited_workers(
        queue_dir, settings=_settings(adopt=False), clock=FakeClock(_NOW)
    )

    assert results == []
    assert load_state(state_path_for(queue_dir, "407-off")) == state


@pytest.mark.parametrize(
    ("prior_account", "session_account", "expected"),
    [
        pytest.param("personal", None, "personal", id="first-attempt-from-prior-record"),
        pytest.param("personal", "work", "personal", id="prior-record-beats-old-session"),
        pytest.param("default", "work", "work", id="undeclared-prior-record-ignored"),
        pytest.param(None, "work", "work", id="no-record-falls-back-to-session"),
        pytest.param(None, None, None, id="nothing-known"),
    ],
)
def test_finalize_exited_records_dispatch_account(
    queue_dir: Path,
    dead_worker: None,
    prior_account: str | None,
    session_account: str | None,
    expected: str | None,
) -> None:
    """The run is recorded under the account the previous supervisor's
    in-flight record names, when the queue declares that account. Otherwise
    it falls back to the state's session host account."""
    _seed_exited(
        queue_dir,
        "408-account",
        session_id="sess-old" if session_account is not None else None,
        session_account=session_account,
    )
    prior = (
        [InFlightRecord(task_id="408-account", account=prior_account, started_at=_EXITED_STARTED)]
        if prior_account is not None
        else []
    )

    results = finalize_exited_workers(
        queue_dir,
        settings=_two_account_settings(),
        clock=FakeClock(_NOW),
        prior_in_flight=prior,
    )

    assert [r.task_id for r in results] == ["408-account"]
    reloaded = load_state(state_path_for(queue_dir, "408-account"))
    assert reloaded.runs[-1].account == expected
    # The log's session is new, so it is hosted where this run ran.
    assert reloaded.session_id == "sess-log"
    assert reloaded.session_account == expected


def test_finalize_exited_stands_down_for_concurrent_writer(
    queue_dir: Path, dead_worker: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0025 recheck guard: when another writer moves the task off
    ``running`` between this pass's read and its write, the finalize stands
    down, the other writer's state stays on disk, and nothing is reported."""
    seeded = _seed_exited(queue_dir, "409-race")
    other = seeded.model_copy(
        update={"status": "failed", "stop_reason": "operator_reset", "pid": None}
    )
    real_reparse = dispatcher_mod._reparse_stdout_file

    def _reparse_after_other_writer(path: Path) -> object:
        write_state_atomic(other, state_path_for(queue_dir, "409-race"))
        return real_reparse(path)

    monkeypatch.setattr(dispatcher_mod, "_reparse_stdout_file", _reparse_after_other_writer)

    results = finalize_exited_workers(queue_dir, settings=_settings(), clock=FakeClock(_NOW))

    assert results == []
    assert load_state(state_path_for(queue_dir, "409-race")) == other
