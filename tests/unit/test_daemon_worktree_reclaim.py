"""The supervisor's opt-in periodic worktree reclaim (ADR-0034).

* :func:`worktree_reclaim_due` -- when a pass runs (pure).
* :func:`run_worktree_reclaim` -- how its results reach the operator.
* :func:`start_daemon` wiring -- off by default, first pass on the first
  tick, then every ``interval_s``; the live in-flight slot set is passed so a
  task whose post-dispatch hook is still running is kept; a pass that raises
  is counted and does not take the loop down.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.config.schema import Settings, WorktreeReclaimSettings
from claude_task_runner.supervisor import daemon as daemon_mod
from claude_task_runner.supervisor.daemon import (
    DaemonHandle,
    run_worktree_reclaim,
    start_daemon,
    worktree_reclaim_due,
)
from claude_task_runner.usage.models import UsageReading, WindowReading
from claude_task_runner.usage.source import FakeUsageSource
from claude_task_runner.worktree import reclaim as reclaim_mod
from claude_task_runner.worktree.reclaim import (
    KeepReason,
    Outcome,
    ReclaimReport,
    ReclaimResult,
)

from ._git_world import World

T0 = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# worktree_reclaim_due
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("periodic", "last_run_offset_s", "draining", "due"),
    [
        (False, None, False, False),  # opt-in: off by default
        (False, 99_999, False, False),
        (True, None, False, True),  # first tick
        (True, None, True, False),  # never while draining
        (True, 3599, False, False),
        (True, 3600, False, True),  # exactly one interval
        (True, 7200, True, False),
    ],
)
def test_worktree_reclaim_due(
    periodic: bool, last_run_offset_s: float | None, draining: bool, due: bool
) -> None:
    settings = WorktreeReclaimSettings(periodic=periodic, interval_s=3600)
    last = None if last_run_offset_s is None else T0 - timedelta(seconds=last_run_offset_s)
    assert worktree_reclaim_due(settings, last_run_at=last, now=T0, draining=draining) is due


# ---------------------------------------------------------------------------
# run_worktree_reclaim
# ---------------------------------------------------------------------------


def _result(task_id: str, outcome: Outcome, **kw: Any) -> ReclaimResult:
    return ReclaimResult(task_id=task_id, working_dir=f"/wt/{task_id}", outcome=outcome, **kw)


def test_run_worktree_reclaim_surfaces_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = ReclaimReport(
        applied=True,
        remote="origin",
        parent_branch="main",
        tasks_scanned=6,
        results=(
            _result("t-gone", Outcome.RECLAIMED, branch_deleted=True),
            _result(
                "t-branch", Outcome.RECLAIMED, branch_deleted=False, detail="git branch -d kept"
            ),
            _result("t-fail", Outcome.FAILED, detail="git worktree remove: boom"),
            _result("t-probe", Outcome.KEPT, reason=KeepReason.GIT_ERROR, detail="git status: x"),
            _result("t-net", Outcome.KEPT, reason=KeepReason.FETCH_FAILED, detail="fetch down"),
            _result("t-run", Outcome.KEPT, reason=KeepReason.STATUS, detail="status=running"),
        ),
        errors=("fetch down",),
    )
    calls: list[dict[str, Any]] = []

    def fake(queue_dir: Path, settings: WorktreeReclaimSettings, **kw: Any) -> ReclaimReport:
        calls.append({"queue_dir": queue_dir, "settings": settings, **kw})
        return report

    monkeypatch.setattr(reclaim_mod, "reclaim_worktrees", fake)
    settings = WorktreeReclaimSettings(periodic=True, max_per_pass=7)
    notes: list[tuple[str, str]] = []
    events: list[tuple[str, dict[str, object]]] = []

    returned = run_worktree_reclaim(
        queue_dir=tmp_path,
        settings=settings,
        in_flight_task_ids={"t-live"},
        notify_callback=lambda level, msg: notes.append((level, msg)),
        event_callback=lambda kind, payload: events.append((kind, payload)),
    )

    assert returned is report
    assert calls == [
        {
            "queue_dir": tmp_path,
            "settings": settings,
            "apply": True,
            "in_flight_task_ids": {"t-live"},
            "limit": 7,
        }
    ]
    # One notification per repository error (not one per affected worktree),
    # plus one per failed removal or probe.
    assert notes == [
        ("warning", "worktree reclaim: fetch down"),
        ("warning", "worktree reclaim failed for task t-fail: git worktree remove: boom"),
        ("warning", "worktree reclaim failed for task t-probe: git status: x"),
    ]
    assert [(kind, payload["task_id"]) for kind, payload in events] == [
        ("worktree_reclaimed", "t-gone"),
        ("worktree_reclaimed", "t-branch"),
        ("worktree_reclaim_failed", "t-fail"),
        ("worktree_reclaim_failed", "t-probe"),
    ]


def test_run_worktree_reclaim_without_callbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = ReclaimReport(
        applied=True,
        remote="origin",
        parent_branch="main",
        tasks_scanned=1,
        results=(_result("t-fail", Outcome.FAILED, detail="boom"),),
        errors=("fetch down",),
    )
    monkeypatch.setattr(reclaim_mod, "reclaim_worktrees", lambda *a, **kw: report)
    assert (
        run_worktree_reclaim(
            queue_dir=tmp_path, settings=WorktreeReclaimSettings(), in_flight_task_ids=set()
        )
        is report
    )


# ---------------------------------------------------------------------------
# start_daemon wiring
# ---------------------------------------------------------------------------


def _reading() -> UsageReading:
    return UsageReading(
        captured_at=T0,
        five_hour=WindowReading(
            utilization_pct=10, resets_at_raw="x", resets_at=T0 + timedelta(hours=5)
        ),
        seven_day=WindowReading(
            utilization_pct=10, resets_at_raw="x", resets_at=T0 + timedelta(days=7)
        ),
    )


def _settings(**reclaim: Any) -> Settings:
    base = load_settings(None)
    return base.model_copy(update={"worktree_reclaim": WorktreeReclaimSettings(**reclaim)})


def _run_daemon(
    queue_dir: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ticks: int,
    tick_s: float = 0.0,
    slots: dict[str, object] | None = None,
    **kw: Any,
) -> DaemonHandle:
    """Run ``ticks`` supervisor ticks with dispatch stubbed out; each inter-tick
    sleep advances a FakeClock by ``tick_s``."""
    from claude_task_runner.runner import force_dispatch as fd_mod
    from claude_task_runner.runner import orchestrator as orch_mod
    from claude_task_runner.supervisor import reconcile_silent as rs_mod

    clock = FakeClock(T0)

    def tick_dispatch(**kwargs: Any) -> Any:
        kwargs["in_flight_slots"].update(slots or {})
        return kwargs["snapshot"]

    monkeypatch.setattr(orch_mod, "tick_dispatch", tick_dispatch)
    monkeypatch.setattr(fd_mod, "tick_consume", lambda **kw: None)
    monkeypatch.setattr(rs_mod, "reap_silent_orphans_tick", lambda *a, **k: [])
    monkeypatch.setattr(daemon_mod, "sleep_for_next_poll", lambda **k: clock.advance(tick_s))
    monkeypatch.setattr(
        "claude_task_runner.supervisor.pidfile.global_lock_path",
        lambda: queue_dir.parent / "test_global.lock",
    )
    return start_daemon(
        queue_dir=queue_dir,
        settings=settings,
        source=FakeUsageSource([_reading()] * ticks),
        pending_count_fn=lambda: 0,
        in_flight_count_fn=lambda: 0,
        clock=clock,
        install_signal_handlers=False,
        max_ticks=ticks,
        **kw,
    )


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World.create(tmp_path, monkeypatch)


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, queue_dir: Path, settings: WorktreeReclaimSettings, **kw: Any) -> Any:
        self.calls.append(kw)
        return ReclaimReport(
            applied=True, remote="origin", parent_branch="main", tasks_scanned=0, results=()
        )


def test_off_by_default(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(reclaim_mod, "reclaim_worktrees", recorder)
    _run_daemon(world.queue, load_settings(None), monkeypatch, ticks=3, tick_s=7200)
    assert recorder.calls == []


def test_first_tick_then_every_interval(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(reclaim_mod, "reclaim_worktrees", recorder)
    settings = _settings(periodic=True, interval_s=3600, max_per_pass=5)
    # Ticks at t = 0, 1800, 3600, 5400, 7200 s: passes at 0, 3600 and 7200.
    _run_daemon(world.queue, settings, monkeypatch, ticks=5, tick_s=1800)
    assert recorder.calls == [
        {"apply": True, "in_flight_task_ids": set(), "limit": 5},
        {"apply": True, "in_flight_task_ids": set(), "limit": 5},
        {"apply": True, "in_flight_task_ids": set(), "limit": 5},
    ]


def test_live_slots_are_passed_as_in_flight(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(reclaim_mod, "reclaim_worktrees", recorder)
    _run_daemon(
        world.queue,
        _settings(periodic=True),
        monkeypatch,
        ticks=1,
        slots={"t-hook": object()},
    )
    assert recorder.calls[0]["in_flight_task_ids"] == {"t-hook"}


def test_a_failing_pass_is_counted_and_waits_an_interval(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[None] = []

    def boom(*args: Any, **kwargs: Any) -> ReclaimReport:
        attempts.append(None)
        raise RuntimeError("simulated")

    monkeypatch.setattr(reclaim_mod, "reclaim_worktrees", boom)
    settings = _settings(periodic=True, interval_s=3600)
    handle = _run_daemon(world.queue, settings, monkeypatch, ticks=3, tick_s=60)
    # Three ticks one minute apart: one attempt, not one per tick.
    assert len(attempts) == 1
    assert handle.tick_failures.worktree_reclaim_total == 1


def test_end_to_end_reclaim_from_the_supervisor(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone = world.add_task("t-merged")
    busy = world.add_task("t-hook")
    events: list[tuple[str, dict[str, object]]] = []

    _run_daemon(
        world.queue,
        _settings(periodic=True),
        monkeypatch,
        ticks=1,
        slots={"t-hook": object()},
        event_callback=lambda kind, payload: events.append((kind, payload)),
    )

    assert not gone.exists()
    assert not world.branch_exists("claude/t-merged")
    assert (busy / ".git").is_file(), "an in-flight task's worktree must survive"
    reclaimed = [payload for kind, payload in events if kind == "worktree_reclaimed"]
    assert [p["task_id"] for p in reclaimed] == ["t-merged"]
    assert reclaimed[0]["branch_deleted"] is True
