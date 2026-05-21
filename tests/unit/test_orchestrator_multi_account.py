"""Tests for multi-account dispatch integration in ``tick_dispatch``.

Drives the orchestrator's full dispatch path with hand-built
``ResolvedAccount`` / ``SupervisorSnapshot`` inputs to verify:

* ``choose_account`` is consulted per candidate task.
* Per-account ``max_concurrency`` from the resolved policy is honoured
  (the queue-wide ``[concurrency].max_concurrency`` no longer caps).
* A task pinned to a paused account is skipped, not silently re-routed.
* The dispatcher is called with the chosen account's ``config_dir`` /
  ``linux_user`` / ``account``.
* The returned snapshot carries the refreshed ``in_flight`` /
  ``in_flight_task_ids`` lists.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from claude_task_runner.clock import RealClock
from claude_task_runner.config.schema import (
    AccountConcurrencyPolicy,
    AccountPolicy,
    AccountSettings,
    ResolvedAccount,
)
from claude_task_runner.queue.schema import Task, TaskState
from claude_task_runner.queue.store import (
    queue_runtime_dir,
    state_path_for,
    task_path_for,
    todo_dir,
    write_state_atomic,
    write_task_atomic,
)
from claude_task_runner.runner.in_flight import DispatchSlot
from claude_task_runner.runner.orchestrator import tick_dispatch
from claude_task_runner.supervisor.states import (
    AccountState,
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


def _make_task(qd: Path, task_id: str, **overrides: Any) -> Task:
    payload: dict[str, Any] = {
        "id": task_id,
        "title": f"Task {task_id}",
        "prompt": "do the thing",
    }
    payload.update(overrides)
    task = Task.model_validate(payload)
    write_task_atomic(task, task_path_for(qd, task_id))
    return task


def _seed_state(qd: Path, task_id: str, status: str) -> TaskState:
    state = TaskState(task_id=task_id, status=status)
    write_state_atomic(state, state_path_for(qd, task_id))
    return state


def _settings(*, names: list[str], max_c: int = 5) -> Any:
    """Minimal Settings shape. tick_dispatch's per-account cap comes
    from the resolved accounts (passed explicitly below); the queue's
    ``max_concurrency`` only feeds ``_target_concurrency``."""
    return SimpleNamespace(
        concurrency=SimpleNamespace(initial_concurrency=max_c, max_concurrency=max_c),
        task_caps=SimpleNamespace(),
        session=SimpleNamespace(),
        hooks=SimpleNamespace(),
        failure_classifier=None,
        dispatch=SimpleNamespace(auto_detect_paths_in_prompt=False),
        claude=SimpleNamespace(config_dir=""),
        accounts=[AccountSettings(name=n, config_dir="") for n in names],
    )


def _resolved(
    name: str, *, cap: int = 1, linux_user: str | None = None, config_dir: str = ""
) -> ResolvedAccount:
    return ResolvedAccount(
        name=name,
        config_dir=config_dir,
        linux_user=linux_user,
        policy=AccountPolicy(concurrency=AccountConcurrencyPolicy(max_concurrency=cap)),
    )


def _snapshot(account_states: dict[str, AccountState]) -> SupervisorSnapshot:
    return SupervisorSnapshot(
        state=SupervisorState.DISPATCHING,
        since=datetime(2026, 5, 21, tzinfo=UTC),
        accounts=account_states,
    )


def _dispatchable(util_5h: int = 0, paused: bool = False) -> AccountState:
    return AccountState(
        state=SupervisorState.DISPATCHING,
        since=datetime(2026, 5, 21, tzinfo=UTC),
        last_5h_util_pct=util_5h,
        paused=paused,
    )


def test_dispatch_routes_to_least_utilized_account(queue_dir: Path) -> None:
    """Two eligible accounts; the lower-5h-util one wins."""
    _make_task(queue_dir, "t1")
    in_flight: dict[str, DispatchSlot] = {}

    accounts = [_resolved("personal", cap=5), _resolved("work", cap=5)]
    snap = _snapshot(
        {
            "personal": _dispatchable(util_5h=80),
            "work": _dispatchable(util_5h=10),
        }
    )

    captured = {}

    def _record_dispatch(**kwargs: object) -> None:
        captured.update(kwargs)

    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
        side_effect=_record_dispatch,
    ):
        new_snap = tick_dispatch(
            queue_dir=queue_dir,
            settings=_settings(names=["personal", "work"], max_c=5),
            clock=RealClock(),
            snapshot=snap,
            in_flight_slots=in_flight,
            accounts=accounts,
        )
        for slot in list(in_flight.values()):
            slot.thread.join(timeout=2)

    assert "t1" in in_flight
    assert in_flight["t1"].account == "work"
    # tick_dispatch returns the snapshot with the live in_flight list.
    assert [r.task_id for r in new_snap.in_flight] == ["t1"]
    assert new_snap.in_flight[0].account == "work"
    assert captured["account"] == "work"


def test_dispatch_honours_per_account_cap(queue_dir: Path) -> None:
    """Per-account cap=1 means the second candidate is skipped this tick."""
    _make_task(queue_dir, "t1")
    _make_task(queue_dir, "t2")
    in_flight: dict[str, DispatchSlot] = {}

    accounts = [_resolved("solo", cap=1)]
    snap = _snapshot({"solo": _dispatchable()})

    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
        return_value=None,
    ):
        tick_dispatch(
            queue_dir=queue_dir,
            settings=_settings(names=["solo"], max_c=5),
            clock=RealClock(),
            snapshot=snap,
            in_flight_slots=in_flight,
            accounts=accounts,
        )
        for slot in list(in_flight.values()):
            slot.thread.join(timeout=2)

    # Only one of the two tasks landed; the other waits for the next
    # tick (or for the in-flight one to finish + reap).
    assert len(in_flight) == 1


def test_dispatch_skips_pinned_paused_account(queue_dir: Path) -> None:
    """task.account points at a paused account → task is not dispatched."""
    _make_task(queue_dir, "t1", account="work")
    in_flight: dict[str, DispatchSlot] = {}

    accounts = [_resolved("personal", cap=5), _resolved("work", cap=5)]
    snap = _snapshot(
        {
            "personal": _dispatchable(),
            "work": _dispatchable(paused=True),
        }
    )

    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
        return_value=None,
    ) as mock_dispatch:
        tick_dispatch(
            queue_dir=queue_dir,
            settings=_settings(names=["personal", "work"], max_c=5),
            clock=RealClock(),
            snapshot=snap,
            in_flight_slots=in_flight,
            accounts=accounts,
        )

    assert "t1" not in in_flight
    mock_dispatch.assert_not_called()


def test_dispatch_passes_config_dir_linux_user(queue_dir: Path, tmp_path: Path) -> None:
    """The chosen account's ``config_dir`` and ``linux_user`` reach ``dispatch``."""
    _make_task(queue_dir, "t1")
    in_flight: dict[str, DispatchSlot] = {}

    cfg = tmp_path / "claude_work"
    cfg.mkdir()
    accounts = [
        _resolved("work", cap=5, config_dir=str(cfg), linux_user="bill-work"),
    ]
    snap = _snapshot({"work": _dispatchable()})

    captured = {}

    def _record(**kwargs: object) -> None:
        captured.update(kwargs)

    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
        side_effect=_record,
    ):
        tick_dispatch(
            queue_dir=queue_dir,
            settings=_settings(names=["work"], max_c=5),
            clock=RealClock(),
            snapshot=snap,
            in_flight_slots=in_flight,
            accounts=accounts,
        )
        for slot in list(in_flight.values()):
            slot.thread.join(timeout=2)

    assert captured["claude_config_dir"] == str(cfg)
    assert captured["linux_user"] == "bill-work"
    assert captured["account"] == "work"


def test_dispatch_returns_snapshot_with_refreshed_in_flight(queue_dir: Path) -> None:
    """The returned snapshot mirrors live attribution; legacy
    in_flight_task_ids stays in sync."""
    _make_task(queue_dir, "t1")
    in_flight: dict[str, DispatchSlot] = {}

    snap = _snapshot({"personal": _dispatchable()})

    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
        return_value=None,
    ):
        new_snap = tick_dispatch(
            queue_dir=queue_dir,
            settings=_settings(names=["personal"], max_c=5),
            clock=RealClock(),
            snapshot=snap,
            in_flight_slots=in_flight,
            accounts=[_resolved("personal", cap=5)],
        )
        for slot in list(in_flight.values()):
            slot.thread.join(timeout=2)

    assert [r.task_id for r in new_snap.in_flight] == ["t1"]
    assert new_snap.in_flight_task_ids == ["t1"]
    assert new_snap.in_flight[0].account == "personal"


def test_dispatch_skips_when_no_eligible_account(queue_dir: Path) -> None:
    """Every account paused → task stays pending; no thread spawned."""
    _make_task(queue_dir, "t1")
    in_flight: dict[str, DispatchSlot] = {}

    accounts = [_resolved("personal", cap=5), _resolved("work", cap=5)]
    snap = _snapshot(
        {
            "personal": _dispatchable(paused=True),
            "work": _dispatchable(paused=True),
        }
    )

    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
        return_value=None,
    ) as mock_dispatch:
        new_snap = tick_dispatch(
            queue_dir=queue_dir,
            settings=_settings(names=["personal", "work"], max_c=5),
            clock=RealClock(),
            snapshot=snap,
            in_flight_slots=in_flight,
            accounts=accounts,
        )

    assert in_flight == {}
    assert new_snap.in_flight == []
    mock_dispatch.assert_not_called()


def test_dispatch_routes_around_capacity_filled_account(queue_dir: Path) -> None:
    """When one account is at capacity (per-account cap), router picks
    the other eligible account."""
    _make_task(queue_dir, "t1")
    _make_task(queue_dir, "t2")
    in_flight: dict[str, DispatchSlot] = {}

    accounts = [_resolved("personal", cap=1), _resolved("work", cap=1)]
    snap = _snapshot(
        {
            "personal": _dispatchable(util_5h=10),
            "work": _dispatchable(util_5h=20),
        }
    )

    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
        return_value=None,
    ):
        tick_dispatch(
            queue_dir=queue_dir,
            settings=_settings(names=["personal", "work"], max_c=5),
            clock=RealClock(),
            snapshot=snap,
            in_flight_slots=in_flight,
            accounts=accounts,
        )
        for slot in list(in_flight.values()):
            slot.thread.join(timeout=2)

    # Both tasks dispatched: one to each account (each capped at 1).
    accts = sorted(slot.account for slot in in_flight.values())
    assert accts == ["personal", "work"]
