"""Extra coverage for scheduler: adaptive rate-limit, worktree teardown
on failure, and promotion edge cases."""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_runner.budget.circuit_breaker import CircuitBreaker
from claude_runner.budget.controller import TokenBudgetController
from claude_runner.config import Settings
from claude_runner.models import TaskState, TaskStatus
from claude_runner.notify.emitter import EventEmitter
from claude_runner.runner.scheduler import Scheduler
from claude_runner.sidecar.schema import (
    InteractionRequest,
    InteractionResponse,
    Option,
    Question,
    RequestState,
)
from claude_runner.sidecar.store import SidecarStore
from claude_runner.state.store import StateStore
from claude_runner.todo.catalog import TodoCatalog


def _make_scheduler(
    tmp_project: Path,
    settings: Settings,
    state_store: StateStore,
    emitter: EventEmitter,
    backend,
    sidecar_store: SidecarStore | None = None,
) -> Scheduler:
    catalog = TodoCatalog(
        tmp_project / "todo",
        state_store=state_store,
        settings=settings,
        time_source=lambda: 0.0,
    )
    return Scheduler(
        settings=settings,
        catalog=catalog,
        backend=backend,
        budget=TokenBudgetController(settings, source=None),
        state_store=state_store,
        emitter=emitter,
        breaker=CircuitBreaker(
            max_consecutive_failures=3,
            failure_rate_threshold=0.5,
            rolling_window=10,
            min_samples=4,
        ),
        sidecar_store=sidecar_store,
    )


def _seed_req(sidecar: SidecarStore, tid: str) -> InteractionRequest:
    req = InteractionRequest(
        task_id=tid,
        sequence=1,
        created_at=datetime(2026, 4, 19, 12, 0, tzinfo=UTC),
        summary="s",
        questions=[Question(id="q", prompt="p", options=[Option(value="A", label="A")])],
        state=RequestState.OPEN,
    )
    sidecar.write_request(req)
    return req


def _make_bare_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    for cmd in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "ci@example.test"],
        ["git", "config", "user.name", "CI"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(repo)],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "fetch", "origin"], cwd=repo, check=True, capture_output=True)
    return repo


def test_snapshot_age_seconds_uses_last_finished_at(
    tmp_project: Path,
    state_store: StateStore,
    emitter: EventEmitter,
    fake_backend,
    make_task,
) -> None:
    settings = Settings(budget_source="static", reporting_interval_s=1, inject_preamble=False)
    sidecar = SidecarStore(state_store.root / "sidecar")
    make_task("t1", id="t1", prompt="x", inject_preamble=False)
    _seed_req(sidecar, "t1")
    st = TaskState(task_id="t1", status=TaskStatus.AWAITING_INPUT)
    st.last_finished_at = datetime.now(tz=UTC) - timedelta(seconds=42)
    state_store.save(st)

    scheduler = _make_scheduler(tmp_project, settings, state_store, emitter, fake_backend, sidecar)
    scheduler._catalog.invalidate()
    scheduler._emit_awaiting_snapshot_if_due()

    payload = json.loads((state_store.root / "status_snapshot.json").read_text())
    row = payload["awaiting"][0]
    assert row["age_seconds"] is not None
    assert 40 < row["age_seconds"] < 80


def test_snapshot_rate_limit_doubles_interval_when_emit_is_slow(
    tmp_project: Path,
    state_store: StateStore,
    emitter: EventEmitter,
    fake_backend,
    make_task,
) -> None:
    settings = Settings(budget_source="static", reporting_interval_s=2, inject_preamble=False)
    sidecar = SidecarStore(state_store.root / "sidecar")
    make_task("t1", id="t1", prompt="x", inject_preamble=False)
    _seed_req(sidecar, "t1")
    state_store.save(TaskState(task_id="t1", status=TaskStatus.AWAITING_INPUT))

    scheduler = _make_scheduler(tmp_project, settings, state_store, emitter, fake_backend, sidecar)
    scheduler._catalog.invalidate()

    # Monkeypatch time.monotonic so the measured emit duration looks long.
    # First call inside _emit_awaiting_snapshot_if_due reads time twice:
    # once for interval check (`now`), once before the emit work (`start`),
    # and once after (`time.monotonic()` for `elapsed`). We need a deterministic
    # sequence that makes elapsed > interval/2.
    values = iter([100.0, 100.0, 110.0])

    def fake_monotonic() -> float:
        return next(values)

    with patch("claude_runner.runner.scheduler.time.monotonic", fake_monotonic):
        scheduler._emit_awaiting_snapshot_if_due()

    # Interval should have doubled (2 → 4) because elapsed=10 > 2/2.
    assert scheduler._reporting_interval_s == 4


def test_run_one_retains_dirty_worktree_as_retained_event(
    tmp_project: Path,
    state_store: StateStore,
    emitter: EventEmitter,
    fake_backend,
    tmp_path: Path,
    make_task,
) -> None:
    """When a task FAILED and its worktree is dirty, teardown raises
    WorktreeError but the scheduler catches it and emits worktree_retained."""
    settings = Settings(budget_source="static", inject_preamble=False)
    repo = _make_bare_repo(tmp_path, "r3")
    wt = tmp_path / "wt-dirty"
    make_task(
        "t1",
        id="t1",
        prompt="go",
        inject_preamble=False,
        git_worktree={
            "repo": str(repo),
            "branch_name": "feat-dirty-retained",
            "branch_from": "origin/main",
            "root": str(wt),
        },
    )
    scheduler = _make_scheduler(tmp_project, settings, state_store, emitter, fake_backend, None)
    spec = next(iter(scheduler._catalog.all_entries())).spec
    prepared = scheduler._prepare_spec_for_dispatch(spec)
    # Dirty the worktree so teardown refuses.
    (wt / "dirty.txt").write_text("left over\n")
    state_store.save(TaskState(task_id="t1", status=TaskStatus.FAILED))
    asyncio.run(scheduler._run_one(prepared))
    # Worktree still exists because teardown refused.
    assert wt.exists()


def test_promote_skips_when_response_is_cancelled(
    tmp_project: Path,
    settings: Settings,
    state_store: StateStore,
    emitter: EventEmitter,
    fake_backend,
    make_task,
) -> None:
    sidecar = SidecarStore(state_store.root / "sidecar")
    make_task("t1", id="t1", prompt="x", inject_preamble=False)
    _seed_req(sidecar, "t1")
    # Write a cancelled (not answered) response.
    InteractionResponse(
        task_id="t1",
        sequence=1,
        responded_at=datetime(2026, 4, 19, 13, 0, tzinfo=UTC),
        answers=[],
        state=RequestState.CANCELLED,
        notes="operator cancelled",
    )
    # Bypass write_response validation by using the lower-level path helper.
    sidecar._atomic_write_json(  # type: ignore[attr-defined]
        sidecar.response_path("t1", 1),
        {
            "schema_version": 1,
            "task_id": "t1",
            "sequence": 1,
            "responded_at": "2026-04-19T13:00:00+00:00",
            "answers": [],
            "state": "cancelled",
            "notes": "operator cancelled",
        },
    )
    state_store.save(TaskState(task_id="t1", status=TaskStatus.AWAITING_INPUT))

    scheduler = _make_scheduler(tmp_project, settings, state_store, emitter, fake_backend, sidecar)
    scheduler._catalog.invalidate()
    scheduler._promote_ready_to_resume()
    # Task should still be AWAITING_INPUT since response state != answered.
    reloaded = state_store.load("t1")
    assert reloaded.status is TaskStatus.AWAITING_INPUT


def test_promote_skips_when_no_response_yet(
    tmp_project: Path,
    settings: Settings,
    state_store: StateStore,
    emitter: EventEmitter,
    fake_backend,
    make_task,
) -> None:
    sidecar = SidecarStore(state_store.root / "sidecar")
    make_task("t1", id="t1", prompt="x", inject_preamble=False)
    _seed_req(sidecar, "t1")
    # No response yet.
    state_store.save(TaskState(task_id="t1", status=TaskStatus.AWAITING_INPUT))

    scheduler = _make_scheduler(tmp_project, settings, state_store, emitter, fake_backend, sidecar)
    scheduler._catalog.invalidate()
    scheduler._promote_ready_to_resume()
    reloaded = state_store.load("t1")
    assert reloaded.status is TaskStatus.AWAITING_INPUT


@pytest.mark.asyncio
async def test_sleep_briefly_waits_on_in_flight_if_present(
    tmp_project: Path,
    settings: Settings,
    state_store: StateStore,
    emitter: EventEmitter,
    fake_backend,
) -> None:
    scheduler = _make_scheduler(tmp_project, settings, state_store, emitter, fake_backend, None)

    async def _fast() -> None:
        await asyncio.sleep(0)

    t = asyncio.create_task(_fast())
    scheduler._in_flight["x"] = t  # type: ignore[assignment]
    await scheduler._sleep_briefly(1)
    await scheduler._drain()


@pytest.mark.asyncio
async def test_sleep_briefly_sleeps_when_no_in_flight(
    tmp_project: Path,
    settings: Settings,
    state_store: StateStore,
    emitter: EventEmitter,
    fake_backend,
) -> None:
    scheduler = _make_scheduler(tmp_project, settings, state_store, emitter, fake_backend, None)
    real_sleep = asyncio.sleep

    async def _instant(_secs: float) -> None:
        await real_sleep(0)

    with patch("claude_runner.runner.scheduler.asyncio.sleep", _instant):
        await scheduler._sleep_briefly(1)
