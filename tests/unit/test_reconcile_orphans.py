"""Tests for the orphan-task reconciliation that runs at supervisor startup.

Bootstraps: a ``"running"`` TaskState on disk after a supervisor exit is
necessarily orphaned (the new supervisor's in-memory in_flight_slots
is empty at boot). The reconciler demotes those tasks to ``"failed"``
so the orchestrator picks them up and resumes via
``runner.session.plan_next_spawn``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_task_runner.queue.schema import TaskState
from claude_task_runner.queue.store import (
    load_state,
    queue_runtime_dir,
    state_path_for,
    todo_dir,
    write_state_atomic,
)
from claude_task_runner.runner import dispatcher as dispatcher_mod
from claude_task_runner.supervisor.reconcile import (
    LIVE_WORKER_STOP_REASON,
    ORPHAN_STOP_REASON,
    reconcile_orphans,
)
from claude_task_runner.supervisor.states import (
    AccountState,
    InFlightRecord,
    SupervisorSnapshot,
    SupervisorState,
)


def _queue(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


def _seed(qd: Path, task_id: str, *, status: str, session_id: str | None = None) -> TaskState:
    state = TaskState(task_id=task_id, status=status, session_id=session_id)
    write_state_atomic(state, state_path_for(qd, task_id))
    return state


def _empty_snapshot() -> SupervisorSnapshot:
    return SupervisorSnapshot(
        state=SupervisorState.IDLE,
        since=datetime(2026, 5, 22, tzinfo=UTC),
    )


def _snapshot_with_in_flight() -> SupervisorSnapshot:
    """Simulate a snapshot the previous (dead) supervisor wrote with
    an in-flight record. The reconciler should clear this."""
    return SupervisorSnapshot(
        state=SupervisorState.DISPATCHING,
        since=datetime(2026, 5, 22, tzinfo=UTC),
        in_flight_task_ids=["t-running"],
        in_flight=[
            InFlightRecord(
                task_id="t-running",
                account="work",
                started_at=datetime(2026, 5, 22, tzinfo=UTC),
            )
        ],
        accounts={
            "work": AccountState(
                state=SupervisorState.DISPATCHING,
                since=datetime(2026, 5, 22, tzinfo=UTC),
            ),
        },
    )


# ---------------------------------------------------------------------------
# Demotion
# ---------------------------------------------------------------------------


def test_running_task_demoted_to_failed(tmp_path: Path) -> None:
    qd = _queue(tmp_path)
    _seed(qd, "t-running", status="running", session_id="sess-abc")

    snap = _empty_snapshot()
    _new_snap, orphans, _parked = reconcile_orphans(qd, snap)

    assert orphans == ["t-running"]
    # Re-read from disk to confirm the write was persisted.
    from claude_task_runner.queue.store import load_state

    reloaded = load_state(state_path_for(qd, "t-running"))
    assert reloaded.status == "failed"
    assert reloaded.stop_reason == ORPHAN_STOP_REASON
    # session_id MUST be preserved so the next dispatch can resume.
    assert reloaded.session_id == "sess-abc"
    # error stays None — this isn't a real failure.
    assert reloaded.error is None


def test_running_task_with_no_session_id_still_demoted(tmp_path: Path) -> None:
    """A task that died before claude reported a session_id still gets
    demoted; the next dispatch will fall back to FRESH per
    plan_next_spawn's session_id-None check."""
    qd = _queue(tmp_path)
    _seed(qd, "t-no-session", status="running", session_id=None)

    _new_snap, orphans, _parked = reconcile_orphans(qd, _empty_snapshot())
    assert orphans == ["t-no-session"]

    from claude_task_runner.queue.store import load_state

    reloaded = load_state(state_path_for(qd, "t-no-session"))
    assert reloaded.status == "failed"
    assert reloaded.session_id is None


# ---------------------------------------------------------------------------
# Non-targets
# ---------------------------------------------------------------------------


def test_does_not_touch_pending_completed_or_failed(tmp_path: Path) -> None:
    qd = _queue(tmp_path)
    _seed(qd, "t-pending", status="pending")
    _seed(qd, "t-completed", status="completed")
    _seed(qd, "t-already-failed", status="failed", session_id="sess-x")

    _, orphans, _parked = reconcile_orphans(qd, _empty_snapshot())
    assert orphans == []

    from claude_task_runner.queue.store import load_state

    assert load_state(state_path_for(qd, "t-pending")).status == "pending"
    assert load_state(state_path_for(qd, "t-completed")).status == "completed"
    # The previously-failed task is untouched (its session_id stays
    # for the natural retry path; the reconciler only changes records
    # whose status is "running").
    fr = load_state(state_path_for(qd, "t-already-failed"))
    assert fr.status == "failed"
    assert fr.session_id == "sess-x"


def test_does_not_touch_awaiting_sidecar(tmp_path: Path) -> None:
    """An awaiting_sidecar task is parked waiting for the operator's
    response — the supervisor didn't die mid-run, the agent filed
    cleanly. Re-dispatching would re-ask the same sidecar question."""
    qd = _queue(tmp_path)
    _seed(qd, "t-sidecar", status="awaiting_sidecar", session_id="sess-x")

    _, orphans, _parked = reconcile_orphans(qd, _empty_snapshot())
    assert orphans == []

    from claude_task_runner.queue.store import load_state

    assert load_state(state_path_for(qd, "t-sidecar")).status == "awaiting_sidecar"


# ---------------------------------------------------------------------------
# Snapshot cleanup
# ---------------------------------------------------------------------------


def test_clears_stale_snapshot_in_flight_records(tmp_path: Path) -> None:
    """The dead supervisor's last in-flight write is stale; clear it
    so this supervisor's own slot map is the authoritative source."""
    qd = _queue(tmp_path)
    _seed(qd, "t-running", status="running", session_id="sess-abc")

    snap = _snapshot_with_in_flight()
    assert len(snap.in_flight) == 1
    assert len(snap.in_flight_task_ids) == 1

    new_snap, _, _parked = reconcile_orphans(qd, snap)
    assert new_snap.in_flight == []
    assert new_snap.in_flight_task_ids == []
    # Other fields are untouched.
    assert new_snap.state is SupervisorState.DISPATCHING
    assert "work" in new_snap.accounts


def test_unparseable_state_file_skipped(tmp_path: Path) -> None:
    """A malformed state YAML doesn't crash the reconciler; it's
    skipped with a warning and the rest are processed normally."""
    qd = _queue(tmp_path)
    # Good orphan that should be demoted.
    _seed(qd, "t-good", status="running", session_id="sess-good")
    # Bad file.
    bad = qd / ".claude_task_runner" / "state" / "bad.yaml"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("not yaml: ][", encoding="utf-8")

    _, orphans, _parked = reconcile_orphans(qd, _empty_snapshot())
    assert orphans == ["t-good"]


# ---------------------------------------------------------------------------
# Multiple orphans
# ---------------------------------------------------------------------------


def test_multiple_orphans_all_demoted(tmp_path: Path) -> None:
    qd = _queue(tmp_path)
    _seed(qd, "t1", status="running", session_id="s1")
    _seed(qd, "t2", status="running", session_id="s2")
    _seed(qd, "t3", status="running", session_id=None)
    _seed(qd, "t4", status="completed")  # control

    _, orphans, _parked = reconcile_orphans(qd, _empty_snapshot())
    assert sorted(orphans) == ["t1", "t2", "t3"]


# ---------------------------------------------------------------------------
# A worker that still runs is parked, never demoted
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("real_proc")
def test_live_pipe_backed_worker_is_parked_and_a_reused_pid_is_demoted(
    tmp_path: Path, live_worker_pid: int, caplog: pytest.LogCaptureFixture
) -> None:
    """With adoption off a worker has no log, so the probe goes by its start
    time. A worker started when its attempt did is still running: its task
    is parked ``possibly_hung`` with the pid, logged at WARNING. The same
    pid recorded for an attempt that started an hour earlier belongs to a
    process started since, so that task's worker is gone and it is demoted.
    Neither process is signalled."""
    qd = _queue(tmp_path)
    now = datetime.now(UTC)
    for task_id, started_at in [("t-live", now), ("t-reused", now - timedelta(hours=1))]:
        write_state_atomic(
            TaskState(
                task_id=task_id,
                status="running",
                last_started_at=started_at,
                pid=live_worker_pid,
                session_id=f"sess-{task_id}",
            ),
            state_path_for(qd, task_id),
        )

    with caplog.at_level(logging.WARNING, logger="claude_task_runner.supervisor.reconcile"):
        _snap, demoted, _parked = reconcile_orphans(qd, _empty_snapshot())

    assert demoted == ["t-reused"]
    reused = load_state(state_path_for(qd, "t-reused"))
    assert (reused.status, reused.stop_reason) == ("failed", ORPHAN_STOP_REASON)
    live = load_state(state_path_for(qd, "t-live"))
    assert (live.status, live.stop_reason, live.pid, live.session_id) == (
        "possibly_hung",
        LIVE_WORKER_STOP_REASON,
        live_worker_pid,
        "sess-t-live",
    )
    warnings = [(r.name, r.getMessage()) for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings[0] == (
        "claude_task_runner.supervisor.reconcile",
        f"reconcile_orphans: task t-live's worker pid={live_worker_pid} is still running "
        "and was not adopted; parked possibly_hung (stop_reason=worker_still_running), "
        "not failed, so it is not dispatched a second time (log=None)",
    )
    # The probe says why t-reused's pid is not its worker (the offset is the
    # sleep's start against an attempt an hour older, about +3600s).
    assert len(warnings) == 2
    assert warnings[1][0] == "claude_task_runner.runner.dispatcher"
    assert warnings[1][1].startswith(
        f"pid {live_worker_pid} is not the worker of the attempt started at "
        f"{(now - timedelta(hours=1)).isoformat()} (log None): the process started +"
    )


def test_keeps_the_in_flight_records_of_parked_live_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The previous supervisor's in_flight records go, except those of tasks
    parked with a live worker: they are the only record of the account
    each worker runs on, which the per-tick parked-worker pass keeps
    counting. A task parked at this restart keeps its record too."""
    qd = _queue(tmp_path)
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda pid: pid == 4_300_001)
    when = datetime(2026, 10, 16, tzinfo=UTC)
    write_state_atomic(
        dispatcher_mod.park_live_worker(
            TaskState(task_id="t-old-park", status="running", pid=4_300_002),
            why="after an earlier restart",
        ),
        state_path_for(qd, "t-old-park"),
    )
    write_state_atomic(
        TaskState(task_id="t-new-park", status="running", last_started_at=when, pid=4_300_001),
        state_path_for(qd, "t-new-park"),
    )
    write_state_atomic(
        TaskState(task_id="t-dead", status="running", last_started_at=when, pid=4_300_003),
        state_path_for(qd, "t-dead"),
    )
    records = [
        InFlightRecord(task_id=tid, account=acct, started_at=when)
        for tid, acct in [("t-dead", "work"), ("t-new-park", "personal"), ("t-old-park", "work")]
    ]
    snap = _empty_snapshot().model_copy(
        update={"in_flight": records, "in_flight_task_ids": [r.task_id for r in records]}
    )

    new_snap, demoted, parked = reconcile_orphans(qd, snap)

    assert demoted == ["t-dead"]
    assert [p.task_id for p in parked] == ["t-new-park"]
    assert [(r.task_id, r.account) for r in new_snap.in_flight] == [
        ("t-new-park", "personal"),
        ("t-old-park", "work"),
    ]
    assert new_snap.in_flight_task_ids == ["t-new-park", "t-old-park"]
