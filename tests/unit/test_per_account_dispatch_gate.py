"""Tests for the per-account dispatch gate in ``tick_dispatch`` (PR 9).

Pinpoints the behavior the gate was designed for:

* When one account is throttled and another is dispatchable, dispatch
  STILL proceeds (and ``choose_account`` routes to the dispatchable
  one). Before PR 9, the top-level ``snapshot.state`` gate would
  alternate every tick based on the most-recently-captured account,
  halving effective dispatch throughput.
* When EVERY configured account is non-dispatchable, the gate returns
  early without invoking ``choose_account`` (preserves the legacy
  early-exit behavior).
* Operator-paused accounts are excluded from the "any dispatchable"
  computation even if their state would otherwise allow dispatch.
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
from claude_task_runner.queue.schema import Task
from claude_task_runner.queue.store import (
    queue_runtime_dir,
    task_path_for,
    todo_dir,
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


def _make_task(qd: Path, task_id: str) -> Task:
    task = Task.model_validate(
        {"id": task_id, "title": f"Task {task_id}", "prompt": "do the thing"}
    )
    write_task_atomic(task, task_path_for(qd, task_id))
    return task


def _settings(names: list[str], max_c: int = 5) -> Any:
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


def _resolved(name: str, cap: int = 5) -> ResolvedAccount:
    return ResolvedAccount(
        name=name,
        config_dir="",
        policy=AccountPolicy(concurrency=AccountConcurrencyPolicy(max_concurrency=cap)),
    )


def _snapshot(
    account_states: dict[str, AccountState], top_state: SupervisorState
) -> SupervisorSnapshot:
    return SupervisorSnapshot(
        state=top_state,
        since=datetime(2026, 5, 22, tzinfo=UTC),
        accounts=account_states,
    )


def _acct(state: SupervisorState, *, paused: bool = False) -> AccountState:
    return AccountState(
        state=state,
        since=datetime(2026, 5, 22, tzinfo=UTC),
        paused=paused,
    )


def test_dispatch_proceeds_when_at_least_one_account_dispatchable(queue_dir: Path) -> None:
    """One account THROTTLED_WEEKLY, another DISPATCHING → dispatch goes
    to the second account. Before PR 9, the top-level gate would have
    rejected this tick because top-level mirrors the most-recently-
    captured account."""
    _make_task(queue_dir, "t1")
    in_flight: dict[str, DispatchSlot] = {}

    accounts = [_resolved("personal"), _resolved("work")]
    snap = _snapshot(
        {
            "personal": _acct(SupervisorState.THROTTLED_WEEKLY),
            "work": _acct(SupervisorState.DISPATCHING),
        },
        # Top-level mirrors personal (simulating "personal was captured last").
        top_state=SupervisorState.THROTTLED_WEEKLY,
    )

    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
        return_value=None,
    ):
        tick_dispatch(
            queue_dir=queue_dir,
            settings=_settings(["personal", "work"]),
            clock=RealClock(),
            snapshot=snap,
            in_flight_slots=in_flight,
            accounts=accounts,
        )
        for slot in list(in_flight.values()):
            slot.thread.join(timeout=2)

    # t1 was dispatched, and it went to work (the dispatchable account).
    assert "t1" in in_flight
    assert in_flight["t1"].account == "work"


def test_dispatch_proceeds_with_throttled_5h_top_level(queue_dir: Path) -> None:
    """Same as above but with THROTTLED_5H at top level — also rejected
    by the old gate, also fine under the new per-account gate."""
    _make_task(queue_dir, "t1")
    in_flight: dict[str, DispatchSlot] = {}

    accounts = [_resolved("personal"), _resolved("work")]
    snap = _snapshot(
        {
            "personal": _acct(SupervisorState.THROTTLED_5H),
            "work": _acct(SupervisorState.DISPATCHING),
        },
        top_state=SupervisorState.THROTTLED_5H,
    )

    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
        return_value=None,
    ):
        tick_dispatch(
            queue_dir=queue_dir,
            settings=_settings(["personal", "work"]),
            clock=RealClock(),
            snapshot=snap,
            in_flight_slots=in_flight,
            accounts=accounts,
        )
        for slot in list(in_flight.values()):
            slot.thread.join(timeout=2)

    assert in_flight["t1"].account == "work"


def test_dispatch_skipped_when_no_account_dispatchable(queue_dir: Path) -> None:
    """All accounts throttled → no dispatch, no choose_account call."""
    _make_task(queue_dir, "t1")
    in_flight: dict[str, DispatchSlot] = {}

    accounts = [_resolved("personal"), _resolved("work")]
    snap = _snapshot(
        {
            "personal": _acct(SupervisorState.THROTTLED_5H),
            "work": _acct(SupervisorState.PAUSED_WEEKLY),
        },
        top_state=SupervisorState.PAUSED_WEEKLY,
    )

    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
        return_value=None,
    ) as mock_dispatch:
        tick_dispatch(
            queue_dir=queue_dir,
            settings=_settings(["personal", "work"]),
            clock=RealClock(),
            snapshot=snap,
            in_flight_slots=in_flight,
            accounts=accounts,
        )

    assert in_flight == {}
    mock_dispatch.assert_not_called()


def test_paused_account_excluded_from_dispatchable_set(queue_dir: Path) -> None:
    """Operator-paused account doesn't count even if its state is
    DISPATCHING. With only the other account paused-OR-throttled, the
    gate closes."""
    _make_task(queue_dir, "t1")
    in_flight: dict[str, DispatchSlot] = {}

    accounts = [_resolved("personal"), _resolved("work")]
    snap = _snapshot(
        {
            # personal is operator-paused via `account pause personal`.
            "personal": _acct(SupervisorState.DISPATCHING, paused=True),
            "work": _acct(SupervisorState.THROTTLED_WEEKLY),
        },
        top_state=SupervisorState.DISPATCHING,
    )

    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
        return_value=None,
    ) as mock_dispatch:
        tick_dispatch(
            queue_dir=queue_dir,
            settings=_settings(["personal", "work"]),
            clock=RealClock(),
            snapshot=snap,
            in_flight_slots=in_flight,
            accounts=accounts,
        )

    assert in_flight == {}
    mock_dispatch.assert_not_called()


def test_idle_account_counts_as_dispatchable_on_cold_start(queue_dir: Path) -> None:
    """Cold start: snapshot.accounts[*].state == IDLE (seeded by the
    daemon). The gate must let dispatch proceed so choose_account picks
    the first eligible account; otherwise the supervisor would deadlock
    waiting for a /usage capture that itself depends on accounts being
    polled."""
    _make_task(queue_dir, "t1")
    in_flight: dict[str, DispatchSlot] = {}

    accounts = [_resolved("personal"), _resolved("work")]
    snap = _snapshot(
        {
            "personal": _acct(SupervisorState.IDLE),
            "work": _acct(SupervisorState.IDLE),
        },
        top_state=SupervisorState.IDLE,
    )

    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
        return_value=None,
    ):
        tick_dispatch(
            queue_dir=queue_dir,
            settings=_settings(["personal", "work"]),
            clock=RealClock(),
            snapshot=snap,
            in_flight_slots=in_flight,
            accounts=accounts,
        )
        for slot in list(in_flight.values()):
            slot.thread.join(timeout=2)

    # One task dispatched; the picked account is whichever choose_account
    # selected (least utilization → alphabetical tie-break → personal).
    assert "t1" in in_flight
