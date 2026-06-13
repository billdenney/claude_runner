"""ADR-0024 regression: ``--over-limit`` is a *throttle* bypass, NOT a
*correctness* bypass — session affinity must still be honoured.

A Claude Code session is namespaced by ``CLAUDE_CONFIG_DIR``. A task
with an active ``session_id`` can only resume on the account that
created it; resuming under a different config dir fails fast with
``No conversation found with session ID`` and burns an attempt against
the per-task circuit breaker. Force-dispatch (and its ``--over-limit``
flag) deliberately bypasses the 5h/weekly throttle gates, but it must
NOT cross-account-resume.

These tests pin the affined account against an *otherwise more eligible*
non-affined account (more headroom, dispatchable state) and assert the
affined account is still chosen — exercising all three force-dispatch
entry points: the pure ``choose_account`` policy, the supervised
``tick_consume`` over-limit path, and the synchronous in-process path
(which always over-limits the throttle).

The companion ``test_force_dispatch.py::TestForceDispatchAffinity`` and
``test_account_dispatch.py::TestSessionAffinity`` cover the decline
branches (orphaned/paused/at-capacity host); this file locks in the
positive "affinity wins even under over-limit" invariant.
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
from claude_task_runner.runner import force_dispatch as fd_mod
from claude_task_runner.runner.account_dispatch import choose_account
from claude_task_runner.runner.in_flight import DispatchSlot
from claude_task_runner.supervisor.states import (
    AccountState,
    SupervisorState,
)


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


# --- builders for the pure choose_account policy -----------------------------


def _state(
    *,
    state: SupervisorState = SupervisorState.DISPATCHING,
    util_5h: int = 0,
    util_weekly: int = 0,
    paused: bool = False,
) -> AccountState:
    return AccountState(
        state=state,
        since=datetime(2026, 5, 21, tzinfo=UTC),
        last_5h_util_pct=util_5h,
        last_weekly_util_pct=util_weekly,
        paused=paused,
    )


def _account(name: str, *, cap: int = 5, config_dir: str = "") -> ResolvedAccount:
    return ResolvedAccount(
        name=name,
        config_dir=config_dir,
        policy=AccountPolicy(concurrency=AccountConcurrencyPolicy(max_concurrency=cap)),
    )


# --- builders for the force_dispatch entry points ----------------------------


def _multi_account_settings() -> Any:
    """Two-account settings shape. ``work`` is the session host;
    ``personal`` is first-configured (the fallback) and is also where a
    naive task.account pin would point."""
    return SimpleNamespace(
        concurrency=SimpleNamespace(initial_concurrency=1, max_concurrency=2),
        task_caps=SimpleNamespace(),
        session=SimpleNamespace(),
        hooks=SimpleNamespace(),
        failure_classifier=None,
        claude=SimpleNamespace(executable="claude", config_dir=""),
        dispatch=SimpleNamespace(auto_detect_paths_in_prompt=False),
        accounts=[
            AccountSettings(name="personal", config_dir="/tmp/.claude_personal"),
            AccountSettings(name="work", config_dir="/tmp/.claude_work"),
        ],
    )


def _make_task(qd: Path, task_id: str, **overrides: Any) -> Task:
    payload: dict[str, Any] = {"id": task_id, "title": f"Task {task_id}", "prompt": "x"}
    payload.update(overrides)
    task = Task.model_validate(payload)
    write_task_atomic(task, task_path_for(qd, task_id))
    return task


def _seed_sessioned_state(
    qd: Path,
    task_id: str,
    *,
    session_id: str,
    session_account: str,
    status: str = "failed",
) -> TaskState:
    state = TaskState(
        task_id=task_id,
        status=status,
        session_id=session_id,
        session_account=session_account,
    )
    write_state_atomic(state, state_path_for(qd, task_id))
    return state


class TestChooseAccountOverLimitAffinity:
    """Pure-policy proof: the affined account wins even when the other
    account is throttled/over-limit and would be the only one a throttle
    bypass could otherwise unlock — affinity is evaluated first and is
    independent of the *other* account's state."""

    def test_affined_wins_over_more_eligible_account(self) -> None:
        choice = choose_account(
            task=Task(id="t1", title="t", prompt="x"),
            accounts={
                "personal": _account("personal"),
                "work": _account("work"),
            },
            account_states={
                # personal: pristine, tons of headroom, would win on util.
                "personal": _state(util_5h=2, util_weekly=2),
                # work: the session host, but heavily utilised.
                "work": _state(util_5h=92, util_weekly=70),
            },
            in_flight=[],
            affined_account="work",
        )
        assert choice.account == "work"
        assert "affined" in choice.reason

    def test_affined_wins_even_when_only_other_account_is_dispatchable(self) -> None:
        """``--over-limit`` exists to push a throttled host through. The
        affined (throttled) host is still chosen — NOT the freely
        dispatchable non-affined account."""
        choice = choose_account(
            task=Task(id="t1", title="t", prompt="x"),
            accounts={"personal": _account("personal"), "work": _account("work")},
            account_states={
                "personal": _state(state=SupervisorState.DISPATCHING, util_5h=5),
                # Host is SLOWING_DOWN (still dispatchable) but at the edge.
                "work": _state(state=SupervisorState.SLOWING_DOWN, util_5h=95),
            },
            in_flight=[],
            affined_account="work",
        )
        assert choice.account == "work"


class TestTickConsumeOverLimitAffinity:
    """Supervised force path with ``allow_over_limit=True``."""

    def test_over_limit_routes_to_affined_not_more_eligible(self, queue_dir: Path) -> None:
        # task.account pins to 'personal' (first-configured, the naive
        # "more eligible" pick), but the live session is hosted on 'work'.
        _make_task(queue_dir, "t1", account="personal")
        _seed_sessioned_state(queue_dir, "t1", session_id="sess-1", session_account="work")
        fd_mod.write_request(queue_dir, "t1", allow_over_limit=True)
        settings = _multi_account_settings()
        in_flight: dict[str, DispatchSlot] = {}

        captured: dict[str, str] = {}

        def fake_spawn(**kwargs: object) -> None:
            captured["account"] = str(kwargs["account"])
            captured["config_dir"] = str(kwargs["claude_config_dir"])

        with patch.object(fd_mod, "_spawn_dispatch_thread", side_effect=fake_spawn):
            n = fd_mod.tick_consume(
                queue_dir=queue_dir,
                settings=settings,
                clock=RealClock(),
                in_flight_slots=in_flight,
            )

        assert n == 1
        # Affinity beats BOTH the task.account pin and first-configured.
        assert captured["account"] == "work"
        assert captured["config_dir"] == "/tmp/.claude_work"


class TestDispatchSynchronouslyOverLimitAffinity:
    """Synchronous in-process force path. This path always over-limits
    the throttle (no throttle gate at all); the affinity check is the
    only account constraint and must still bind."""

    def test_routes_to_affined_not_pinned(self, queue_dir: Path) -> None:
        _make_task(queue_dir, "t1", account="personal")
        _seed_sessioned_state(queue_dir, "t1", session_id="sess-1", session_account="work")
        settings = _multi_account_settings()

        captured: dict[str, str] = {}

        def fake_dispatch(**kwargs: object) -> Any:
            captured["account"] = str(kwargs["account"])
            captured["config_dir"] = str(kwargs["claude_config_dir"])
            return SimpleNamespace(new_state=TaskState(task_id="t1", status="completed"))

        with (
            patch.object(fd_mod.dispatcher_mod, "dispatch", side_effect=fake_dispatch),
            patch.object(fd_mod, "plan_next_spawn", return_value=SimpleNamespace()),
        ):
            out = fd_mod.dispatch_synchronously(
                task_id="t1",
                queue_dir=queue_dir,
                settings=settings,
                clock=RealClock(),
            )

        assert out.status == "completed"
        assert captured["account"] == "work"
        assert captured["config_dir"] == "/tmp/.claude_work"
