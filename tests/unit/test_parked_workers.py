"""Tests for workers parked ``possibly_hung`` while they still run.

``runner.dispatcher.park_live_worker`` parks a task whose worker runs with
no monitor thread in this supervisor. Each tick,
``supervisor.adoption.track_parked_workers``:

* counts each parked worker that still runs against the account it runs
  on, so ``dispatch_headroom`` and ``choose_account`` see it, and so do the
  persisted ``in_flight`` list and ``account list``;
* records the run of each parked worker that has exited, from its log.

The startup exited-worker finalize records them too.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from claude_task_runner.cli import account_cmd
from claude_task_runner.clock import FakeClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.config.schema import (
    AccountConcurrencyPolicy,
    AccountPolicy,
    AccountSettings,
    ResolvedAccount,
    Settings,
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
from claude_task_runner.runner.dispatcher import LIVE_WORKER_STOP_REASON, park_live_worker
from claude_task_runner.runner.in_flight import DispatchSlot
from claude_task_runner.runner.orchestrator import tick_dispatch
from claude_task_runner.supervisor import daemon as daemon_mod
from claude_task_runner.supervisor import persistence as persist_mod
from claude_task_runner.supervisor import reconcile_silent
from claude_task_runner.supervisor.adoption import (
    UNATTRIBUTED_ACCOUNT,
    ExitedWorkerResult,
    ParkedWorkers,
    ParkedWorkerTracker,
    finalize_exited_workers,
)
from claude_task_runner.supervisor.daemon import start_daemon
from claude_task_runner.supervisor.states import (
    AccountState,
    InFlightRecord,
    SupervisorSnapshot,
    SupervisorState,
)
from claude_task_runner.usage.models import UsageReading, WindowReading
from claude_task_runner.usage.source import FakeUsageSource

from ._worker_probe_stubs import fake_proc

_NOW = datetime(2026, 10, 16, 14, 0, tzinfo=UTC)
_PID = 4_200_007
_STARTED = _NOW - timedelta(minutes=40)
_INIT = '{"type":"system","subtype":"init","session_id":"sess-parked"}'
_SUCCESS = (
    '{"type":"result","subtype":"success","stop_reason":"end_turn","is_error":false,'
    '"total_cost_usd":0.5,"usage":{"input_tokens":10,"output_tokens":5}}'
)


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


@pytest.fixture
def worker(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """Whether ``_PID`` runs; flip ``["alive"]`` to end the worker."""
    flag = {"alive": True}
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda pid: pid == _PID and flag["alive"])
    return flag


def _settings(names: list[str]) -> Settings:
    base = load_settings(None)
    return base.model_copy(
        update={"accounts": [AccountSettings(name=n, config_dir=f"/tmp/{n}") for n in names]}
    )


def _resolved(name: str, cap: int) -> ResolvedAccount:
    return ResolvedAccount(
        name=name,
        config_dir=f"/tmp/{name}",
        linux_user=None,
        policy=AccountPolicy(concurrency=AccountConcurrencyPolicy(max_concurrency=cap)),
    )


def _task(queue_dir: Path, task_id: str, cap_s: float | None = None) -> None:
    write_task_atomic(
        Task(id=task_id, title="t", prompt="p", working_dir=None, max_duration_s_override=cap_s),
        task_path_for(queue_dir, task_id),
    )


def _park(
    queue_dir: Path,
    task_id: str = "t-parked",
    *,
    lines: tuple[str, ...] = (_INIT,),
    session_account: str | None = None,
    cap_s: float | None = None,
) -> TaskState:
    """A task parked while its worker, ``_PID``, ran, writing ``lines``.
    ``cap_s`` is the task's duration cap (the queue's 4 h by default)."""
    _task(queue_dir, task_id, cap_s)
    log = queue_dir / ".claude_task_runner" / "logs" / task_id / "attempt-1.stream.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(line + "\n" for line in lines))
    os.utime(log, (_STARTED.timestamp(), _STARTED.timestamp()))
    running = TaskState(
        task_id=task_id,
        status="running",
        attempts=1,
        last_started_at=_STARTED,
        pid=_PID,
        log_path=str(log),
        session_id="sess-parked" if session_account else None,
        session_account=session_account,
    )
    parked = park_live_worker(running, why="and was not adopted by this supervisor")
    write_state_atomic(parked, state_path_for(queue_dir, task_id))
    return parked


def _snapshot(
    accounts: list[str], in_flight: list[InFlightRecord], util: dict[str, int] | None = None
) -> SupervisorSnapshot:
    """``util`` is each account's 5 h utilization; choose_account prefers the
    least used account with room."""
    return SupervisorSnapshot(
        state=SupervisorState.DISPATCHING,
        since=_NOW,
        accounts={
            n: AccountState(
                state=SupervisorState.DISPATCHING,
                since=_NOW,
                last_5h_util_pct=(util or {}).get(n, 0),
            )
            for n in accounts
        },
        in_flight=in_flight,
        in_flight_task_ids=[r.task_id for r in in_flight],
    )


def _tick(
    queue_dir: Path,
    settings: Settings,
    snapshot: SupervisorSnapshot,
    accounts: list[ResolvedAccount],
    events: list[tuple[str, dict[str, Any]]] | None = None,
    *,
    tracker: ParkedWorkerTracker | None = None,
    slots: dict[str, DispatchSlot] | None = None,
    notes: list[tuple[str, str]] | None = None,
) -> tuple[SupervisorSnapshot, dict[str, DispatchSlot]]:
    """One tick with the dispatcher stubbed; joins every slot's thread."""
    slots = {} if slots is None else slots
    with patch("claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch", return_value=None):
        snap = tick_dispatch(
            queue_dir=queue_dir,
            settings=settings,
            clock=FakeClock(_NOW),
            snapshot=snapshot,
            in_flight_slots=slots,
            accounts=accounts,
            event_callback=(lambda k, p: events.append((k, p))) if events is not None else None,
            notify_callback=(lambda lvl, m: notes.append((lvl, m))) if notes is not None else None,
            parked_tracker=tracker,
        )
        for slot in slots.values():
            slot.thread.join(timeout=10)
    return snap, slots


def _track(
    queue_dir: Path,
    names: list[str],
    prior: list[InFlightRecord] | None = None,
    tracker: ParkedWorkerTracker | None = None,
    owned: set[str] | None = None,
) -> ParkedWorkers:
    return (tracker or ParkedWorkerTracker()).tick(
        queue_dir,
        settings=_settings(names),
        clock=FakeClock(_NOW),
        prior_in_flight=prior or [],
        owned_ids=owned or set(),
    )


# ---------------------------------------------------------------------------
# S1: a parked live worker holds its account's slot
# ---------------------------------------------------------------------------


def test_parked_live_worker_holds_its_accounts_slot(
    queue_dir: Path, worker: dict[str, bool]
) -> None:
    """``work`` and ``personal`` take one task each, and ``work`` is the
    less used, so ``choose_account`` would pick it. The worker parked on
    ``work`` (as the previous tick's record says) holds work's slot, so of
    two pending tasks only one starts, on personal; the persisted list shows
    both."""
    _park(queue_dir)
    _task(queue_dir, "t-a")
    _task(queue_dir, "t-b")
    names = ["work", "personal"]
    prior = [InFlightRecord(task_id="t-parked", account="work", started_at=_STARTED)]

    snap, slots = _tick(
        queue_dir,
        _settings(names),
        _snapshot(names, prior, util={"work": 10, "personal": 80}),
        [_resolved("work", 1), _resolved("personal", 1)],
    )

    assert [(tid, s.account) for tid, s in slots.items()] == [("t-a", "personal")]
    assert [(r.task_id, r.account) for r in snap.in_flight] == [
        ("t-a", "personal"),
        ("t-parked", "work"),
    ]
    assert snap.in_flight_task_ids == ["t-a", "t-parked"]
    assert load_state(state_path_for(queue_dir, "t-parked")).status == "possibly_hung"


def test_parked_worker_releases_its_slot_and_is_recorded_when_it_exits(
    queue_dir: Path, worker: dict[str, bool]
) -> None:
    """The worker finished. The tick records its run from the log
    (completed, one RunRecord) on a thread that holds work's slot, as a
    dispatch thread holds it through its post-dispatch hook; the next tick
    reaps that thread, reports the run, and gives the slot to a pending
    task."""
    _park(queue_dir, lines=(_INIT, _SUCCESS))
    _task(queue_dir, "t-a")
    names = ["work"]
    prior = [InFlightRecord(task_id="t-parked", account="work", started_at=_STARTED)]
    worker["alive"] = False
    events: list[tuple[str, dict[str, Any]]] = []
    tracker = ParkedWorkerTracker()

    snap, slots = _tick(
        queue_dir,
        _settings(names),
        _snapshot(names, prior),
        [_resolved("work", 1)],
        events,
        tracker=tracker,
    )
    assert [(tid, s.account) for tid, s in slots.items()] == [("t-parked", "work")]
    assert [(r.task_id, r.account) for r in snap.in_flight] == [("t-parked", "work")]
    assert events == []

    snap, slots = _tick(
        queue_dir,
        _settings(names),
        snap,
        [_resolved("work", 1)],
        events,
        tracker=tracker,
        slots=slots,
    )

    done = load_state(state_path_for(queue_dir, "t-parked"))
    assert (done.status, done.stop_reason, done.pid, done.log_path) == (
        "completed",
        "end_turn",
        None,
        None,
    )
    assert [(r.stop_reason, r.pid, r.account) for r in done.runs] == [("end_turn", _PID, "work")]
    assert list(slots) == ["t-a"]
    assert [(r.task_id, r.account) for r in snap.in_flight] == [("t-a", "work")]
    assert events == [
        (
            "exited_worker_finalized",
            {
                "task_id": "t-parked",
                "pid": _PID,
                "log_path": str(_park_log(queue_dir)),
                "status": "completed",
                "stop_reason": "end_turn",
            },
        )
    ]


def _park_log(queue_dir: Path, task_id: str = "t-parked") -> Path:
    return queue_dir / ".claude_task_runner" / "logs" / task_id / "attempt-1.stream.jsonl"


def test_parked_worker_that_crashed_is_recorded_as_failed(
    queue_dir: Path, worker: dict[str, bool]
) -> None:
    """No result event: the worker crashed. Nothing else handles a parked
    task, so the crash is recorded and the task fails, to be dispatched
    again with its session."""
    _park(queue_dir, lines=(_INIT,))
    worker["alive"] = False
    tracker = ParkedWorkerTracker()

    parked = _track(queue_dir, ["work"], tracker=tracker)
    assert parked.in_flight == []
    assert [(j.state.task_id, j.account, j.cap_violation) for j in parked.to_finalize] == [
        ("t-parked", "work", None)
    ]
    tracker.finalize(
        parked.to_finalize[0], queue_dir, settings=_settings(["work"]), clock=FakeClock(_NOW)
    )

    assert [
        (f.task_id, f.status, f.stop_reason)
        for f in _track(queue_dir, ["work"], tracker=tracker).finished
    ] == [("t-parked", "failed", "process_exit_nonzero")]
    state = load_state(state_path_for(queue_dir, "t-parked"))
    assert (state.status, state.pid, len(state.runs)) == ("failed", None, 1)


@pytest.mark.parametrize(
    ("names", "prior", "session_account", "expected"),
    [
        pytest.param(["work", "personal"], "personal", None, "personal", id="prior-record"),
        pytest.param(["work", "personal"], None, "work", "work", id="session-host"),
        pytest.param(["solo"], None, None, "solo", id="sole-account"),
        pytest.param(["work", "personal"], None, None, UNATTRIBUTED_ACCOUNT, id="unknown"),
        pytest.param(["work", "personal"], "retired", None, UNATTRIBUTED_ACCOUNT, id="undeclared"),
    ],
)
def test_parked_worker_account_comes_from_adoptions_sources(
    queue_dir: Path,
    worker: dict[str, bool],
    names: list[str],
    prior: str | None,
    session_account: str | None,
    expected: str,
) -> None:
    _park(queue_dir, session_account=session_account)
    records = (
        [InFlightRecord(task_id="t-parked", account=prior, started_at=_STARTED)] if prior else []
    )

    parked = _track(queue_dir, names, records)

    assert parked.in_flight == [
        InFlightRecord(task_id="t-parked", account=expected, started_at=_STARTED)
    ]
    assert (parked.to_finalize, parked.finished) == ([], [])


def test_unattributed_parked_worker_counts_against_the_queue_cap_only(
    queue_dir: Path, worker: dict[str, bool]
) -> None:
    """No record names its account: it takes neither account's slot, but
    an explicit queue cap of 2 counts it, so one of two tasks starts."""
    _park(queue_dir)
    _task(queue_dir, "t-a")
    _task(queue_dir, "t-b")
    names = ["work", "personal"]
    settings = _settings(names)
    settings = settings.model_copy(
        update={"concurrency": settings.concurrency.model_copy(update={"max_concurrency": 2})}
    )

    snap, slots = _tick(
        queue_dir, settings, _snapshot(names, []), [_resolved("work", 1), _resolved("personal", 1)]
    )

    assert list(slots) == ["t-a"]
    assert ("t-parked", UNATTRIBUTED_ACCOUNT) in [(r.task_id, r.account) for r in snap.in_flight]


def test_task_with_a_monitor_thread_is_left_to_it(queue_dir: Path, worker: dict[str, bool]) -> None:
    """A task a reaper parked while its dispatch thread still waits on the
    worker has a slot already, and its thread records the run."""
    _park(queue_dir)
    worker["alive"] = False
    parked = _track(queue_dir, ["work"], owned={"t-parked"})
    assert parked == ParkedWorkers(in_flight=[], to_finalize=[], finished=[])


def test_other_possibly_hung_tasks_are_not_counted(
    queue_dir: Path, worker: dict[str, bool]
) -> None:
    """A SILENT park clears the pid, and names another stop reason."""
    _task(queue_dir, "t-silent")
    write_state_atomic(
        TaskState(
            task_id="t-silent",
            status="possibly_hung",
            stop_reason="silent_on_restart",
            last_started_at=_STARTED,
        ),
        state_path_for(queue_dir, "t-silent"),
    )
    parked = _track(queue_dir, ["work"])
    assert parked.in_flight == []


# ---------------------------------------------------------------------------
# S2: the startup pass records a parked worker that exited
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lines", "status", "stop_reason"),
    [((_INIT, _SUCCESS), "completed", "end_turn"), ((_INIT,), "failed", "process_exit_nonzero")],
)
def test_startup_records_a_parked_worker_that_exited(
    queue_dir: Path,
    worker: dict[str, bool],
    lines: tuple[str, ...],
    status: str,
    stop_reason: str,
) -> None:
    _park(queue_dir, lines=lines)
    worker["alive"] = False

    results = finalize_exited_workers(
        queue_dir, settings=_settings(["work"]), clock=FakeClock(_NOW)
    )

    assert results == [
        ExitedWorkerResult(
            task_id="t-parked",
            pid=_PID,
            log_path=str(_park_log(queue_dir)),
            status=status,
            stop_reason=stop_reason,
        )
    ]
    assert load_state(state_path_for(queue_dir, "t-parked")).status == status


def test_startup_leaves_a_parked_worker_that_still_runs(
    queue_dir: Path, worker: dict[str, bool]
) -> None:
    parked = _park(queue_dir, lines=(_INIT, _SUCCESS))
    assert (
        finalize_exited_workers(queue_dir, settings=_settings(["work"]), clock=FakeClock(_NOW))
        == []
    )
    assert load_state(state_path_for(queue_dir, "t-parked")) == parked


# ---------------------------------------------------------------------------
# Through the daemon: supervisor.json and `account list`
# ---------------------------------------------------------------------------


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


def test_daemon_persists_the_parked_worker_and_account_list_counts_it(
    queue_dir: Path,
    worker: dict[str, bool],
    private_global_lock: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a tick, supervisor.json lists the parked worker in flight on
    the queue's only account, and ``account list`` counts it there."""
    _park(queue_dir)
    settings = load_settings(None)
    monkeypatch.setattr(daemon_mod, "sleep_for_next_poll", lambda **_kw: None)

    start_daemon(
        queue_dir=queue_dir,
        settings=settings,
        source=FakeUsageSource([_reading(), _reading()]),
        pending_count_fn=lambda: 0,
        in_flight_count_fn=lambda: 0,
        clock=FakeClock(_NOW),
        install_signal_handlers=False,
        max_ticks=1,
    )

    snap = persist_mod.load(
        persist_mod.supervisor_state_path(queue_dir, settings.supervisor.state_file)
    )
    assert snap is not None
    assert [(r.task_id, r.account) for r in snap.in_flight] == [("t-parked", "default")]
    row = account_cmd._state_fields("default", snap, frozenset(), cap=1)
    assert row["in_flight_count"] == 1
    assert load_state(state_path_for(queue_dir, "t-parked")).stop_reason == LIVE_WORKER_STOP_REASON


# ---------------------------------------------------------------------------
# S-1: a parked worker is held to its task's duration cap
# ---------------------------------------------------------------------------


def test_parked_worker_past_its_duration_cap_is_stopped_and_recorded(
    queue_dir: Path, worker: dict[str, bool], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Started 40 minutes ago with a 10-minute cap: the pass terminates the
    worker's process group, and its run is recorded as killed by the
    duration cap; it no longer holds a slot."""
    _park(queue_dir, cap_s=600)
    terminated: list[int] = []

    def terminate(pid: int) -> bool:
        terminated.append(pid)
        worker["alive"] = False
        return True

    monkeypatch.setattr(reconcile_silent, "terminate_worker", terminate)
    tracker = ParkedWorkerTracker()

    parked = _track(queue_dir, ["work"], tracker=tracker)

    assert terminated == [_PID]
    assert parked.in_flight == []
    (job,) = parked.to_finalize
    assert job.cap_violation is not None
    assert (job.cap_violation.which, job.cap_violation.observed, job.cap_violation.cap) == (
        "duration",
        2400.0,
        600.0,
    )
    tracker.finalize(job, queue_dir, settings=_settings(["work"]), clock=FakeClock(_NOW))
    state = load_state(state_path_for(queue_dir, "t-parked"))
    assert (state.status, state.pid, [r.killed_by_cap for r in state.runs]) == (
        "failed",
        None,
        ["duration"],
    )


def test_parked_worker_within_its_cap_is_left_running(
    queue_dir: Path, worker: dict[str, bool], monkeypatch: pytest.MonkeyPatch
) -> None:
    _park(queue_dir, cap_s=3600)
    monkeypatch.setattr(
        reconcile_silent,
        "terminate_worker",
        lambda _pid: (_ for _ in ()).throw(AssertionError("terminated within its cap")),
    )
    parked = _track(queue_dir, ["work"])
    assert [r.task_id for r in parked.in_flight] == ["t-parked"]
    assert parked.to_finalize == []


def test_a_capped_worker_that_survives_keeps_its_slot_and_backs_off(
    queue_dir: Path,
    worker: dict[str, bool],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The terminate cannot confirm the kill (a D-state, say): the worker
    keeps its slot, the failure is logged once, and the next attempt waits
    out the retry delay."""
    _park(queue_dir, cap_s=600)
    attempts: list[int] = []
    monkeypatch.setattr(
        reconcile_silent, "terminate_worker", lambda pid: attempts.append(pid) or False
    )
    tracker = ParkedWorkerTracker()

    with caplog.at_level(logging.WARNING, logger="claude_task_runner.supervisor.adoption"):
        first = _track(queue_dir, ["work"], tracker=tracker)
        second = _track(queue_dir, ["work"], tracker=tracker)

    assert attempts == [_PID]
    assert (
        [r.task_id for r in first.in_flight]
        == [r.task_id for r in second.in_flight]
        == ["t-parked"]
    )
    assert first.to_finalize == second.to_finalize == []
    assert [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "could not be recorded" in r.getMessage()
    ] == [
        f"parked task t-parked: its run could not be recorded: worker pid={_PID} survived "
        "its duration-cap terminate; retrying in 60s, and not logging this error again for it"
    ]


# ---------------------------------------------------------------------------
# N-5: a run that cannot be recorded is logged once and retried later
# ---------------------------------------------------------------------------


def test_unrecordable_parked_run_is_logged_once_and_backs_off(
    queue_dir: Path, worker: dict[str, bool], caplog: pytest.LogCaptureFixture
) -> None:
    _park(queue_dir)
    task_path_for(queue_dir, "t-parked").unlink()
    worker["alive"] = False
    tracker = ParkedWorkerTracker()
    settings = _settings(["work"])

    def tick_at(when: datetime) -> ParkedWorkers:
        return tracker.tick(
            queue_dir, settings=settings, clock=FakeClock(when), prior_in_flight=[], owned_ids=()
        )

    with caplog.at_level(logging.WARNING, logger="claude_task_runner.supervisor.adoption"):
        (job,) = tick_at(_NOW).to_finalize
        tracker.finalize(job, queue_dir, settings=settings, clock=FakeClock(_NOW))
        waiting = tick_at(_NOW + timedelta(seconds=59)).to_finalize
        (retry,) = tick_at(_NOW + timedelta(seconds=61)).to_finalize
        tracker.finalize(
            retry, queue_dir, settings=settings, clock=FakeClock(_NOW + timedelta(seconds=61))
        )
        still_waiting = tick_at(_NOW + timedelta(seconds=61 + 119)).to_finalize
        (again,) = tick_at(_NOW + timedelta(seconds=61 + 121)).to_finalize

    assert waiting == still_waiting == []
    assert again.state.task_id == "t-parked"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].startswith(
        "parked task t-parked: its run could not be recorded: its Task YAML could not be loaded"
    )
    assert load_state(state_path_for(queue_dir, "t-parked")).status == "possibly_hung"


# ---------------------------------------------------------------------------
# N-6: recording a parked run runs off the tick thread, holding the slot
# ---------------------------------------------------------------------------


def test_a_slow_post_dispatch_hook_does_not_stall_the_tick(
    queue_dir: Path, worker: dict[str, bool], monkeypatch: pytest.MonkeyPatch
) -> None:
    _park(queue_dir, lines=(_INIT, _SUCCESS))
    worker["alive"] = False
    release = threading.Event()
    entered = threading.Event()

    def slow_hook(*_args: object, **_kwargs: object) -> None:
        entered.set()
        release.wait(timeout=30)

    monkeypatch.setattr(dispatcher_mod, "_run_post_dispatch_hook", slow_hook)
    slots: dict[str, DispatchSlot] = {}
    names = ["work"]
    with patch("claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch", return_value=None):
        tick_dispatch(
            queue_dir=queue_dir,
            settings=_settings(names),
            clock=FakeClock(_NOW),
            snapshot=_snapshot(names, []),
            in_flight_slots=slots,
            accounts=[_resolved("work", 1)],
        )
    try:
        assert entered.wait(timeout=10)
        assert list(slots) == ["t-parked"]
        assert slots["t-parked"].thread.is_alive()
    finally:
        release.set()
        slots["t-parked"].thread.join(timeout=10)
    assert load_state(state_path_for(queue_dir, "t-parked")).status == "completed"


# ---------------------------------------------------------------------------
# N-4, N-10: a park made by the selector or a dispatch thread is reported
# once and counted from the tick it happens in
# ---------------------------------------------------------------------------


def test_selector_park_is_reported_once_and_counted_the_same_tick(
    queue_dir: Path, worker: dict[str, bool]
) -> None:
    """A failed task whose recorded worker still runs takes work's only
    slot the tick the selector parks it, so the pending task waits; the
    park is reported that tick and not again."""
    _task(queue_dir, "t-failed")
    log = queue_dir / ".claude_task_runner" / "logs" / "t-failed" / "attempt-1.stream.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text("")
    write_state_atomic(
        TaskState(
            task_id="t-failed",
            status="failed",
            attempts=1,
            last_started_at=_STARTED,
            pid=_PID,
            log_path=str(log),
        ),
        state_path_for(queue_dir, "t-failed"),
    )
    _task(queue_dir, "t-new")
    names = ["work"]
    events: list[tuple[str, dict[str, Any]]] = []
    notes: list[tuple[str, str]] = []
    tracker = ParkedWorkerTracker()

    snap, slots = _tick(
        queue_dir,
        _settings(names),
        _snapshot(names, []),
        [_resolved("work", 1)],
        events,
        tracker=tracker,
        notes=notes,
    )

    assert slots == {}
    assert [(r.task_id, r.account) for r in snap.in_flight] == [("t-failed", "work")]
    assert events == [
        ("live_worker_parked", {"task_id": "t-failed", "pid": _PID, "log_path": str(log)})
    ]
    assert notes == [
        (
            "warning",
            f"task t-failed: worker pid={_PID} is still running though its task was "
            "dispatchable; parked possibly_hung, not dispatched again",
        )
    ]

    _tick(queue_dir, _settings(names), snap, [_resolved("work", 1)], events, tracker=tracker)
    assert len(events) == 1


def test_backstop_park_is_reported_at_the_next_tick(
    queue_dir: Path, worker: dict[str, bool], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No state records the worker, so the selector admits the task and the
    dispatch thread's backstop parks it; the next tick reports the park."""
    _task(queue_dir, "t-untracked")
    log = queue_dir / ".claude_task_runner" / "logs" / "t-untracked" / "attempt-1.stream.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text("")
    monkeypatch.setattr(
        dispatcher_mod,
        "_PROC_ROOT",
        fake_proc(
            tmp_path / "proc",
            _PID,
            started=_STARTED,
            boot=_NOW - timedelta(days=1),
            stdout=log,
        ),
    )
    names = ["work"]
    events: list[tuple[str, dict[str, Any]]] = []
    tracker = ParkedWorkerTracker()

    snap, slots = _tick(
        queue_dir,
        _settings(names),
        _snapshot(names, []),
        [_resolved("work", 2)],
        events,
        tracker=tracker,
    )
    assert events == []
    snap, slots = _tick(
        queue_dir,
        _settings(names),
        snap,
        [_resolved("work", 2)],
        events,
        tracker=tracker,
        slots=slots,
    )
    _tick(
        queue_dir,
        _settings(names),
        snap,
        [_resolved("work", 2)],
        events,
        tracker=tracker,
        slots=slots,
    )

    assert events == [
        ("live_worker_parked", {"task_id": "t-untracked", "pid": _PID, "log_path": str(log)})
    ]


def test_a_forced_tasks_backstop_park_goes_to_the_tracker(
    queue_dir: Path, worker: dict[str, bool], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A worker starts writing the forced task's log after the request's own
    check found none (that check is stubbed to find none), so the dispatch
    thread's backstop parks the task. The park goes to the tracker, which
    the next tick reports, as for a park in a tick's own dispatch."""
    _task(queue_dir, "t-forced")
    log = queue_dir / ".claude_task_runner" / "logs" / "t-forced" / "attempt-1.stream.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text("")
    monkeypatch.setattr(
        dispatcher_mod,
        "_PROC_ROOT",
        fake_proc(
            tmp_path / "proc",
            _PID,
            started=_STARTED,
            boot=_NOW - timedelta(days=1),
            stdout=log,
        ),
    )
    monkeypatch.setattr(fd_mod, "live_worker_of", lambda *_args: None)
    config_dir = tmp_path / "cfg-work"
    config_dir.mkdir()
    settings = load_settings(None).model_copy(
        update={"accounts": [AccountSettings(name="work", config_dir=str(config_dir))]}
    )
    fd_mod.write_request(queue_dir, "t-forced")
    tracker = ParkedWorkerTracker()
    slots: dict[str, DispatchSlot] = {}

    with patch("claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch") as dispatch:
        n = fd_mod.tick_consume(
            queue_dir=queue_dir,
            settings=settings,
            clock=FakeClock(_NOW),
            in_flight_slots=slots,
            on_park=tracker.note_park,
        )
        for slot in slots.values():
            slot.thread.join(timeout=10)

    assert n == 1
    dispatch.assert_not_called()
    assert [
        (state.task_id, state.status, state.pid, why) for state, why in tracker.drain_parks()
    ] == [("t-forced", "possibly_hung", _PID, "when its task was about to be dispatched again")]
