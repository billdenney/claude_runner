"""All-accounts-exhausted dispatch scenario (audit coverage gap).

When every configured account is already at its per-account
``max_concurrency`` and a task is still pending, ``choose_account``
returns ``DispatchChoice(account=None, ...)`` for that task. The
orchestrator must then make NO dispatch attempt, raise no error, and
leave the task pending for a future tick (after an in-flight task
finishes and is reaped) — i.e. no busy-spin and no crash.

This drives the real ``runner.orchestrator.tick_dispatch`` path (not
just the pure ``choose_account`` policy) with two accounts pinned at
``max_concurrency=1``, both already in-flight, and one pending task.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
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
from claude_task_runner.runner.account_dispatch import choose_account
from claude_task_runner.runner.in_flight import DispatchSlot
from claude_task_runner.runner.orchestrator import tick_dispatch
from claude_task_runner.supervisor.states import (
    AccountState,
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


def _settings(*, names: list[str], target: int) -> Any:
    """Minimal Settings shape. ``target`` feeds ``_target_concurrency``
    (set high enough that the queue-wide gate does NOT short-circuit, so
    the per-account capacity decision in ``choose_account`` is the thing
    under test). Per-account caps come from the resolved accounts."""
    return SimpleNamespace(
        concurrency=SimpleNamespace(initial_concurrency=target, max_concurrency=target),
        task_caps=SimpleNamespace(),
        session=SimpleNamespace(),
        hooks=SimpleNamespace(),
        failure_classifier=None,
        dispatch=SimpleNamespace(auto_detect_paths_in_prompt=False),
        claude=SimpleNamespace(config_dir=""),
        accounts=[AccountSettings(name=n, config_dir="") for n in names],
    )


def _resolved(name: str, *, cap: int = 1) -> ResolvedAccount:
    return ResolvedAccount(
        name=name,
        config_dir="",
        policy=AccountPolicy(concurrency=AccountConcurrencyPolicy(max_concurrency=cap)),
    )


def _dispatchable(util_5h: int = 0) -> AccountState:
    return AccountState(
        state=SupervisorState.DISPATCHING,
        since=datetime(2026, 5, 21, tzinfo=UTC),
        last_5h_util_pct=util_5h,
        paused=False,
    )


@contextmanager
def _busy_slots(*specs: tuple[str, str]) -> Iterator[dict[str, DispatchSlot]]:
    """Build an ``in_flight_slots`` dict of LIVE busy slots.

    ``tick_dispatch`` reaps any slot whose thread has already finished
    before it computes per-account capacity, so the occupying threads
    must stay alive for the duration of the tick. Each ``spec`` is a
    ``(task_id, account)`` pair. The threads are torn down on exit.
    """
    stop = threading.Event()
    slots: dict[str, DispatchSlot] = {}
    threads: list[threading.Thread] = []
    for task_id, account in specs:
        thread = threading.Thread(target=lambda: stop.wait(timeout=5), daemon=True)
        thread.start()
        threads.append(thread)
        slots[task_id] = DispatchSlot(
            task_id=task_id,
            account=account,
            started_at=datetime(2026, 5, 21, tzinfo=UTC),
            thread=thread,
        )
    try:
        yield slots
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=2)


class TestChooseAccountExhausted:
    """The pure policy returns ``account=None`` when every account is full."""

    def test_both_accounts_at_capacity_returns_none(self) -> None:
        in_flight = [
            InFlightRecord(
                task_id="busy-personal",
                account="personal",
                started_at=datetime(2026, 5, 21, tzinfo=UTC),
            ),
            InFlightRecord(
                task_id="busy-work",
                account="work",
                started_at=datetime(2026, 5, 21, tzinfo=UTC),
            ),
        ]
        choice = choose_account(
            task=Task(id="t3", title="t", prompt="x"),
            accounts={"personal": _resolved("personal", cap=1), "work": _resolved("work", cap=1)},
            account_states={"personal": _dispatchable(), "work": _dispatchable()},
            in_flight=in_flight,
        )
        assert choice.account is None
        assert "no eligible account" in choice.reason
        assert "at capacity" in choice.reason


class TestTickDispatchExhausted:
    """The orchestrator makes no dispatch attempt and does not crash/spin."""

    def test_no_dispatch_when_all_accounts_at_capacity(self, queue_dir: Path) -> None:
        # One genuinely-pending task; two accounts both already in-flight
        # (cap=1 each). The two in-flight task IDs are NOT in todo/ so
        # they cannot themselves be re-selected.
        _make_task(queue_dir, "t3")

        accounts = [_resolved("personal", cap=1), _resolved("work", cap=1)]
        snap = SupervisorSnapshot(
            state=SupervisorState.DISPATCHING,
            since=datetime(2026, 5, 21, tzinfo=UTC),
            accounts={"personal": _dispatchable(util_5h=10), "work": _dispatchable(util_5h=20)},
        )

        with (
            _busy_slots(("busy-personal", "personal"), ("busy-work", "work")) as in_flight,
            patch(
                "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
                return_value=None,
            ) as mock_dispatch,
        ):
            new_snap = tick_dispatch(
                queue_dir=queue_dir,
                # target=3 keeps the queue-wide gate open (available=1),
                # so the per-account capacity check is what blocks t3.
                settings=_settings(names=["personal", "work"], target=3),
                clock=RealClock(),
                snapshot=snap,
                in_flight_slots=in_flight,
                accounts=accounts,
            )

            # No dispatch was attempted for the pending task.
            mock_dispatch.assert_not_called()
            # t3 never entered the in-flight set; only the two pre-existing
            # busy slots remain (both still alive, so not reaped).
            assert "t3" not in in_flight
            assert set(in_flight) == {"busy-personal", "busy-work"}
            # No spin / no error: the call returned a refreshed snapshot
            # that still reflects exactly the two busy slots.
            assert "t3" not in [r.task_id for r in new_snap.in_flight]
            assert {r.task_id for r in new_snap.in_flight} == {"busy-personal", "busy-work"}

    def test_one_freed_slot_dispatches_next_tick(self, queue_dir: Path) -> None:
        """Sanity counter-test: with only ONE account full and the other
        free, the same pending task DOES dispatch — proving the no-op
        above is caused by exhaustion, not by an unrelated gate."""
        _make_task(queue_dir, "t3")

        accounts = [_resolved("personal", cap=1), _resolved("work", cap=1)]
        snap = SupervisorSnapshot(
            state=SupervisorState.DISPATCHING,
            since=datetime(2026, 5, 21, tzinfo=UTC),
            accounts={"personal": _dispatchable(util_5h=10), "work": _dispatchable(util_5h=20)},
        )

        captured: dict[str, object] = {}

        def _record(**kwargs: object) -> None:
            captured.update(kwargs)

        with (
            _busy_slots(("busy-work", "work")) as in_flight,
            patch(
                "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
                side_effect=_record,
            ),
        ):
            tick_dispatch(
                queue_dir=queue_dir,
                settings=_settings(names=["personal", "work"], target=3),
                clock=RealClock(),
                snapshot=snap,
                in_flight_slots=in_flight,
                accounts=accounts,
            )
            # Join only the freshly-dispatched t3 thread; the busy-work
            # thread is torn down by the context manager.
            if "t3" in in_flight:
                in_flight["t3"].thread.join(timeout=2)

            # personal had the free slot, so t3 routed there.
            assert captured["account"] == "personal"
            assert "t3" in in_flight
