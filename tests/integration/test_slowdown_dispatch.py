"""SLOWING_DOWN dispatch applies the concurrency the operator is told (ADR-0022).

When an account enters SLOWING_DOWN the supervisor notifies
``slowing dispatch: ... target concurrency=X/Y``. ``X`` is ADR-0022's
linear ramp: the account's ``max_concurrency`` at ``fivehr_slowdown_pct``,
falling to 0 at ``fivehr_stop_pct``. These tests drive the real daemon
tick (:func:`run_one_tick`) and the real orchestrator
(:func:`tick_dispatch`), then count the tasks dispatched through each
account, so they fail whenever dispatch applies a number other than the
one the operator was told.

The configuration mirrors a live two-account queue: ``personal`` allows
5 concurrent tasks, ``work`` allows 1, and the queue-wide
``[concurrency]`` block allows 5.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.loader import load_settings, resolve_accounts
from claude_task_runner.config.schema import Settings
from claude_task_runner.queue.schema import Task
from claude_task_runner.queue.store import (
    queue_runtime_dir,
    task_path_for,
    todo_dir,
    write_task_atomic,
)
from claude_task_runner.runner import orchestrator as orch_mod
from claude_task_runner.runner.in_flight import DispatchSlot
from claude_task_runner.supervisor import persistence as persist_mod
from claude_task_runner.supervisor.actions import Action, Notify
from claude_task_runner.supervisor.daemon import TickContext, run_one_tick
from claude_task_runner.supervisor.states import SupervisorSnapshot, SupervisorState
from claude_task_runner.usage.models import UsageReading, WindowReading

NOW = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
"""Noon UTC: inside the default day band (slowdown 40, stop 60)."""

PENDING = 8
"""More pending tasks than any cap in these tests, so caps decide the count."""


def _write_account_policy(config_dir: Path, max_concurrency: int) -> None:
    config_dir.mkdir(parents=True)
    (config_dir / "runner-account.toml").write_text(
        f"[concurrency]\nmax_concurrency = {max_concurrency}\n", encoding="utf-8"
    )


def _load(tmp_path: Path, body: str) -> Settings:
    toml = tmp_path / "claude_runner.toml"
    toml.write_text('[dispatch_pct]\ntimezone = "UTC"\n\n' + body, encoding="utf-8")
    return load_settings(toml)


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    for i in range(PENDING):
        task = Task.model_validate({"id": f"t{i}", "title": f"Task t{i}", "prompt": "p"})
        write_task_atomic(task, task_path_for(qd, task.id))
    return qd


def _reading(five_hour_pct: int, account: str | None) -> UsageReading:
    return UsageReading(
        captured_at=NOW,
        five_hour=WindowReading(
            utilization_pct=five_hour_pct,
            resets_at_raw="x",
            resets_at=NOW + timedelta(hours=2),
        ),
        seven_day=WindowReading(
            utilization_pct=5,
            resets_at_raw="y",
            resets_at=NOW + timedelta(days=4),
        ),
        account=account,
    )


def _tick(
    snapshot: SupervisorSnapshot,
    settings: Settings,
    reading: UsageReading,
    clock: FakeClock,
) -> tuple[SupervisorSnapshot, list[Action]]:
    """One supervisor tick, built the way ``start_daemon`` builds it."""
    ctx = TickContext(
        settings=settings,
        poll_result=reading,
        pending_count=PENDING,
        in_flight_count=0,
        account_policies={a.name: a.policy for a in resolve_accounts(settings)},
    )
    return run_one_tick(snapshot, ctx, clock)


def _dispatch_counts(
    queue_dir: Path, settings: Settings, snapshot: SupervisorSnapshot, clock: FakeClock
) -> dict[str, int]:
    """Run one real ``tick_dispatch`` and count dispatched tasks per account.

    The dispatcher is stubbed, and every dispatch thread is joined while
    the stub is still in place, so no ``claude`` process can start.
    """
    slots: dict[str, DispatchSlot] = {}

    def _no_op_dispatch(**_kwargs: Any) -> None:
        return None

    with patch.object(orch_mod.dispatcher_mod, "dispatch", side_effect=_no_op_dispatch):
        orch_mod.tick_dispatch(
            queue_dir=queue_dir,
            settings=settings,
            clock=clock,
            snapshot=snapshot,
            in_flight_slots=slots,
        )
        for slot in slots.values():
            slot.thread.join(timeout=5)
    counts: dict[str, int] = {}
    for slot in slots.values():
        counts[slot.account] = counts.get(slot.account, 0) + 1
    return counts


def _slowdown_messages(actions: list[Action]) -> list[str]:
    return [
        a.message
        for a in actions
        if isinstance(a, Notify) and a.message.startswith("slowing dispatch")
    ]


class TestMultiAccountSlowdown:
    """``personal`` is slowing down at 55% 5h; ``work`` is dispatching at 10%.

    The ramp for ``personal`` is ``ceil(5 * (1 - (55 - 40) / (60 - 40)))
    = 2``. ``work`` is not slowing down, so it keeps its cap of 1. The
    order in which the two accounts were captured must not matter.
    """

    @pytest.mark.parametrize(
        "capture_order",
        [("personal", "work"), ("work", "personal")],
        ids=["personal-then-work", "work-then-personal"],
    )
    def test_slowed_account_dispatches_its_ramp_target(
        self, tmp_path: Path, queue_dir: Path, capture_order: tuple[str, str]
    ) -> None:
        _write_account_policy(tmp_path / "personal", max_concurrency=5)
        _write_account_policy(tmp_path / "work", max_concurrency=1)
        settings = _load(
            tmp_path,
            "[concurrency]\nmax_concurrency = 5\ninitial_concurrency = 5\n\n"
            f'[[accounts]]\nname = "personal"\nconfig_dir = "{tmp_path / "personal"}"\n\n'
            f'[[accounts]]\nname = "work"\nconfig_dir = "{tmp_path / "work"}"\n',
        )
        clock = FakeClock(NOW)
        snapshot = persist_mod.initial_snapshot(since=NOW, account_names=["personal", "work"])
        five_hour_pct = {"personal": 55, "work": 10}

        actions: list[Action] = []
        for name in capture_order:
            snapshot, tick_actions = _tick(
                snapshot, settings, _reading(five_hour_pct[name], name), clock
            )
            actions.extend(tick_actions)

        assert snapshot.accounts["personal"].state is SupervisorState.SLOWING_DOWN
        assert snapshot.accounts["work"].state is SupervisorState.DISPATCHING
        assert _slowdown_messages(actions) == [
            "slowing dispatch: 5h=55% in [40, 60) (day); target concurrency=2/5"
        ]

        counts = _dispatch_counts(queue_dir, settings, snapshot, clock)

        assert counts == {"personal": 2, "work": 1}


class TestSingleAccountSlowdown:
    """A queue with no ``[[accounts]]`` block: one ``default`` account.

    Its readings carry no account name, which is what the single-account
    usage source produces. The account and the queue both allow 4.
    """

    @pytest.mark.parametrize(
        ("five_hour_pct", "ramp_target"),
        [(45, 3), (55, 1)],
        ids=["45pct-ramp-3", "55pct-ramp-1"],
    )
    def test_dispatches_the_ramp_target(
        self, tmp_path: Path, queue_dir: Path, five_hour_pct: int, ramp_target: int
    ) -> None:
        _write_account_policy(tmp_path / "claude", max_concurrency=4)
        settings = _load(
            tmp_path,
            "[concurrency]\nmax_concurrency = 4\ninitial_concurrency = 4\n\n"
            f'[claude]\nconfig_dir = "{tmp_path / "claude"}"\n',
        )
        clock = FakeClock(NOW)
        snapshot = persist_mod.initial_snapshot(since=NOW, account_names=["default"])

        snapshot, actions = _tick(snapshot, settings, _reading(five_hour_pct, None), clock)

        assert snapshot.state is SupervisorState.SLOWING_DOWN
        assert _slowdown_messages(actions) == [
            f"slowing dispatch: 5h={five_hour_pct}% in [40, 60) (day); "
            f"target concurrency={ramp_target}/4"
        ]

        counts = _dispatch_counts(queue_dir, settings, snapshot, clock)

        assert counts == {"default": ramp_target}
