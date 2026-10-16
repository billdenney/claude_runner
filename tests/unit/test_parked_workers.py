"""Tests for workers parked ``possibly_hung`` while they still run.

``runner.dispatcher.park_live_worker`` parks a task whose worker runs with
no monitor thread in this supervisor. The startup exited-worker finalize
records the run of a parked worker that has exited, from its log.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

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
from claude_task_runner.runner.dispatcher import park_live_worker
from claude_task_runner.runner.in_flight import DispatchSlot
from claude_task_runner.runner.orchestrator import tick_dispatch
from claude_task_runner.supervisor.adoption import (
    ExitedWorkerResult,
    finalize_exited_workers,
)
from claude_task_runner.supervisor.states import (
    AccountState,
    InFlightRecord,
    SupervisorSnapshot,
    SupervisorState,
)

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


def _task(queue_dir: Path, task_id: str) -> None:
    write_task_atomic(
        Task(id=task_id, title="t", prompt="p", working_dir=None), task_path_for(queue_dir, task_id)
    )


def _park(
    queue_dir: Path,
    task_id: str = "t-parked",
    *,
    lines: tuple[str, ...] = (_INIT,),
    session_account: str | None = None,
) -> TaskState:
    """A task parked while its worker, ``_PID``, ran, writing ``lines``."""
    _task(queue_dir, task_id)
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


def _snapshot(accounts: list[str], in_flight: list[InFlightRecord]) -> SupervisorSnapshot:
    return SupervisorSnapshot(
        state=SupervisorState.DISPATCHING,
        since=_NOW,
        accounts={n: AccountState(state=SupervisorState.DISPATCHING, since=_NOW) for n in accounts},
        in_flight=in_flight,
        in_flight_task_ids=[r.task_id for r in in_flight],
    )


def _tick(
    queue_dir: Path,
    settings: Settings,
    snapshot: SupervisorSnapshot,
    accounts: list[ResolvedAccount],
    events: list[tuple[str, dict[str, Any]]] | None = None,
) -> tuple[SupervisorSnapshot, dict[str, DispatchSlot]]:
    slots: dict[str, DispatchSlot] = {}
    with patch("claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch", return_value=None):
        snap = tick_dispatch(
            queue_dir=queue_dir,
            settings=settings,
            clock=FakeClock(_NOW),
            snapshot=snapshot,
            in_flight_slots=slots,
            accounts=accounts,
            event_callback=(lambda k, p: events.append((k, p))) if events is not None else None,
        )
        for slot in slots.values():
            slot.thread.join(timeout=5)
    return snap, slots


def _park_log(queue_dir: Path, task_id: str = "t-parked") -> Path:
    return queue_dir / ".claude_task_runner" / "logs" / task_id / "attempt-1.stream.jsonl"


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
