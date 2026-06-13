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
from claude_task_runner.runner.in_flight import DispatchSlot
from claude_task_runner.supervisor.adoption import adopt_running_workers
from claude_task_runner.supervisor.reconcile import reconcile_orphans
from claude_task_runner.supervisor.states import SupervisorSnapshot, SupervisorState


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


def test_reconcile_orphans_shields_adopted_ids(queue_dir: Path) -> None:
    """``reconcile_orphans`` must NOT demote a task whose id is in
    ``adopted_ids`` — that task has a live worker + monitor thread."""
    # Two running orphans; one is "adopted", one is not.
    _seed_running(
        queue_dir,
        "200-adopted",
        pid=1,
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


def test_reconcile_orphans_demotes_all_when_no_adopted(queue_dir: Path) -> None:
    """Default (no adopted_ids): every running orphan is demoted — the
    historical behaviour is preserved bit-for-bit."""
    _seed_running(
        queue_dir,
        "300-a",
        pid=1,
        log_path=_make_log(queue_dir, "300-a"),
        started_at=_NOW,
        last_heartbeat_at=_NOW,
    )
    _snap, demoted = reconcile_orphans(queue_dir, _snapshot())
    assert demoted == ["300-a"]
    assert load_state(state_path_for(queue_dir, "300-a")).status == "failed"
