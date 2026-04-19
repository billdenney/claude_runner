"""Tests for Scheduler._prepare_spec_for_dispatch, _promote_ready_to_resume,
and _emit_awaiting_snapshot_if_due — the sidecar + worktree plumbing.

These exercise the scheduler's per-task preparation path without having to
drive the full run loop. The pattern follows ``test_scheduler_internals.py``.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from claude_runner.budget.circuit_breaker import CircuitBreaker
from claude_runner.budget.controller import TokenBudgetController
from claude_runner.config import Settings
from claude_runner.models import TaskState, TaskStatus
from claude_runner.notify.emitter import EventEmitter
from claude_runner.runner.scheduler import Scheduler
from claude_runner.sidecar.schema import (
    Answer,
    InteractionRequest,
    InteractionResponse,
    Option,
    Question,
    RequestState,
)
from claude_runner.sidecar.store import SidecarStore
from claude_runner.state.store import StateStore
from claude_runner.todo.catalog import TodoCatalog


def _make_bare_repo(tmp_path: Path, name: str = "repo") -> Path:
    """Create a fresh local git repo that treats itself as origin."""
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


def _seed_request(
    sidecar: SidecarStore, *, task_id: str = "t1", seq: int = 1
) -> InteractionRequest:
    req = InteractionRequest(
        task_id=task_id,
        sequence=seq,
        created_at=datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC),
        summary="pick one",
        questions=[
            Question(
                id="decision",
                prompt="Which?",
                options=[Option(value="A", label="A"), Option(value="B", label="B")],
                recommended="A",
            )
        ],
        state=RequestState.OPEN,
    )
    sidecar.write_request(req)
    return req


def _seed_response(
    sidecar: SidecarStore,
    req: InteractionRequest,
    *,
    value: str = "A",
    notes: str | None = None,
) -> None:
    resp = InteractionResponse(
        task_id=req.task_id,
        sequence=req.sequence,
        responded_at=datetime(2026, 4, 19, 13, 0, 0, tzinfo=UTC),
        answers=[Answer(id=q.id, value=value) for q in req.questions],
        notes=notes,
        state=RequestState.ANSWERED,
    )
    sidecar.write_response(resp, request=req)


def test_prepare_spec_injects_preamble_into_prompt(
    tmp_project: Path,
    settings: Settings,
    state_store: StateStore,
    emitter: EventEmitter,
    fake_backend,
    make_task,
) -> None:
    """With inject_preamble on, the dispatched spec prompt gets the preamble
    block prepended (task_id + gh-read-only + sidecar rules)."""
    sidecar = SidecarStore(state_store.root / "sidecar")
    make_task("t1", id="t1", prompt="ORIGINAL PROMPT")
    scheduler = _make_scheduler(tmp_project, settings, state_store, emitter, fake_backend, sidecar)
    entries = list(scheduler._catalog.all_entries())
    assert entries, "task should be discovered"
    spec = entries[0].spec
    prepared = scheduler._prepare_spec_for_dispatch(spec)
    assert "ORIGINAL PROMPT" in prepared.prompt
    assert "CLAUDE_RUNNER_TASK_ID=t1" in prepared.prompt
    # Original prompt is appended after the preamble.
    assert prepared.prompt.rstrip().endswith("ORIGINAL PROMPT")


def test_prepare_spec_skips_preamble_when_task_opts_out(
    tmp_project: Path,
    settings: Settings,
    state_store: StateStore,
    emitter: EventEmitter,
    fake_backend,
    make_task,
) -> None:
    sidecar = SidecarStore(state_store.root / "sidecar")
    make_task("t1", id="t1", prompt="RAW", inject_preamble=False)
    scheduler = _make_scheduler(tmp_project, settings, state_store, emitter, fake_backend, sidecar)
    spec = next(iter(scheduler._catalog.all_entries())).spec
    prepared = scheduler._prepare_spec_for_dispatch(spec)
    assert prepared.prompt == "RAW"


def test_prepare_spec_sets_up_worktree_and_redirects_working_dir(
    tmp_project: Path,
    settings: Settings,
    state_store: StateStore,
    emitter: EventEmitter,
    fake_backend,
    tmp_path: Path,
    make_task,
) -> None:
    repo = _make_bare_repo(tmp_path, "r1")
    wt_root = tmp_path / "wts"
    make_task(
        "t1",
        id="t1",
        prompt="go",
        inject_preamble=False,
        git_worktree={
            "repo": str(repo),
            "branch_name": "feat-abc",
            "branch_from": "origin/main",
            "root": str(wt_root / "t1"),
        },
    )
    scheduler = _make_scheduler(tmp_project, settings, state_store, emitter, fake_backend, None)
    spec = next(iter(scheduler._catalog.all_entries())).spec
    prepared = scheduler._prepare_spec_for_dispatch(spec)
    assert prepared.working_dir == wt_root / "t1"
    assert (wt_root / "t1" / "README.md").exists()


def test_prepare_spec_resume_prepends_operator_answers(
    tmp_project: Path,
    settings: Settings,
    state_store: StateStore,
    emitter: EventEmitter,
    fake_backend,
    make_task,
) -> None:
    sidecar = SidecarStore(state_store.root / "sidecar")
    make_task("t1", id="t1", prompt="BRIEF", inject_preamble=False)
    req = _seed_request(sidecar)
    _seed_response(sidecar, req, value="B", notes="go with B")
    # Force task into READY_TO_RESUME.
    state_store.save(TaskState(task_id="t1", status=TaskStatus.READY_TO_RESUME))

    scheduler = _make_scheduler(tmp_project, settings, state_store, emitter, fake_backend, sidecar)
    spec = next(iter(scheduler._catalog.all_entries())).spec
    prepared = scheduler._prepare_spec_for_dispatch(spec)
    assert "operator" in prepared.prompt.lower()
    assert "'B'" in prepared.prompt or ": B" in prepared.prompt
    assert "go with B" in prepared.prompt
    assert "BRIEF" in prepared.prompt


def test_promote_ready_to_resume_flips_status_and_emits(
    tmp_project: Path,
    settings: Settings,
    state_store: StateStore,
    emitter: EventEmitter,
    fake_backend,
    make_task,
) -> None:
    sidecar = SidecarStore(state_store.root / "sidecar")
    make_task("t1", id="t1", prompt="x", inject_preamble=False)
    req = _seed_request(sidecar)
    _seed_response(sidecar, req, value="A")
    # Task is AWAITING_INPUT (with a matching answered response already on disk).
    state_store.save(TaskState(task_id="t1", status=TaskStatus.AWAITING_INPUT))

    scheduler = _make_scheduler(tmp_project, settings, state_store, emitter, fake_backend, sidecar)
    # Invalidate so catalog picks up the new state.
    scheduler._catalog.invalidate()
    scheduler._promote_ready_to_resume()

    reloaded = state_store.load("t1")
    assert reloaded.status is TaskStatus.READY_TO_RESUME


def test_promote_is_noop_when_sidecar_is_none(
    tmp_project: Path,
    settings: Settings,
    state_store: StateStore,
    emitter: EventEmitter,
    fake_backend,
) -> None:
    scheduler = _make_scheduler(tmp_project, settings, state_store, emitter, fake_backend, None)
    # Should return without raising.
    scheduler._promote_ready_to_resume()


def test_emit_awaiting_snapshot_writes_file_and_respects_interval(
    tmp_project: Path,
    state_store: StateStore,
    emitter: EventEmitter,
    fake_backend,
    make_task,
) -> None:
    settings = Settings(
        budget_source="static",
        reporting_interval_s=1,
        report_max_per_tick=1,
        inject_preamble=False,
    )
    sidecar = SidecarStore(state_store.root / "sidecar")
    for tid in ("a", "b", "c"):
        make_task(tid, id=tid, prompt=f"p-{tid}", inject_preamble=False)
        _seed_request(sidecar, task_id=tid, seq=1)
        state_store.save(TaskState(task_id=tid, status=TaskStatus.AWAITING_INPUT))

    scheduler = _make_scheduler(tmp_project, settings, state_store, emitter, fake_backend, sidecar)
    scheduler._catalog.invalidate()
    scheduler._emit_awaiting_snapshot_if_due()

    snap_path = state_store.root / "status_snapshot.json"
    assert snap_path.is_file()
    payload = json.loads(snap_path.read_text())
    assert payload["awaiting_total"] == 3
    assert {row["task_id"] for row in payload["awaiting"]} == {"a", "b", "c"}

    # Second call within the interval window should NOT re-emit (snapshot
    # timestamp stays).
    first_ts = payload["generated_at"]
    scheduler._emit_awaiting_snapshot_if_due()
    payload2 = json.loads(snap_path.read_text())
    assert payload2["generated_at"] == first_ts


def test_emit_snapshot_noop_when_sidecar_is_none(
    tmp_project: Path,
    settings: Settings,
    state_store: StateStore,
    emitter: EventEmitter,
    fake_backend,
) -> None:
    scheduler = _make_scheduler(tmp_project, settings, state_store, emitter, fake_backend, None)
    scheduler._emit_awaiting_snapshot_if_due()  # should not raise


def test_run_one_tears_down_worktree_on_completion(
    tmp_project: Path,
    settings: Settings,
    state_store: StateStore,
    emitter: EventEmitter,
    fake_backend,
    tmp_path: Path,
    make_task,
) -> None:
    """After a COMPLETED task, the worktree is removed."""
    import asyncio as _asyncio

    repo = _make_bare_repo(tmp_path, "r2")
    wt = tmp_path / "wt-t1"
    make_task(
        "t1",
        id="t1",
        prompt="go",
        inject_preamble=False,
        git_worktree={
            "repo": str(repo),
            "branch_name": "feat-teardown",
            "branch_from": "origin/main",
            "root": str(wt),
        },
    )
    scheduler = _make_scheduler(tmp_project, settings, state_store, emitter, fake_backend, None)
    spec = next(iter(scheduler._catalog.all_entries())).spec
    prepared = scheduler._prepare_spec_for_dispatch(spec)
    assert wt.exists()
    # Mark COMPLETED so _run_one's teardown branch fires.
    state_store.save(TaskState(task_id="t1", status=TaskStatus.COMPLETED))
    _asyncio.run(scheduler._run_one(prepared))
    assert not wt.exists()
