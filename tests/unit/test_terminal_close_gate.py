"""Tests for the terminal-close auto-gate (ADR-0033).

A run that closes as a clean SKIP or DEFER writes a deliverable, commits
nothing and leaves the worktree clean. ``_finalize_state`` marks that
``completed``, which is not dispatchable -- so on its own it never re-fires.

The leak is the sidecar-resume path: a task that filed a sidecar sits in
``awaiting_sidecar`` and becomes eligible again the moment every request has
a response, which is correct when there is a ruling to act on and pure waste
when the disposition was terminal. The dispatch selector reads only the
``block_dispatch`` register, so a row there is the only thing that holds such
a task down. This module writes one.

The branches that matter, and why each is here:

* Terminal close, no existing row  => a row is written, status stays
  ``completed`` (it is a genuine completion, not a failure).
* Terminal close, row already there => nothing written; an operator's
  curated ruling is never overwritten or duplicated.
* Run that COMMITTED               => no row. The task produced code; gating
  it would strand real work.
* Run that left work uncommitted   => no row, and the existing
  ``uncommitted_work_left`` gate still fires. Gating here would be actively
  harmful: that task MUST be re-dispatched to finish.
* Block-list not configured        => no-op, so queues without the
  convention are unaffected.
* Unwritable register              => swallowed; a register problem must
  never fail a run that genuinely succeeded.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_task_runner.config.schema import DispatchSettings
from claude_task_runner.queue.schema import RunRecord, Task, TaskState, TokenUsage
from claude_task_runner.runner import terminal_gate as terminal_gate_mod
from claude_task_runner.runner.dispatcher import _finalize_state
from claude_task_runner.runner.session import ResumeStrategy, SpawnPlan
from claude_task_runner.runner.stream import StreamSummary

WHEN = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
BLOCK_FILE = "needs_acquisition.jsonl"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    repo = tmp_path / "wt"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("seed\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    q = tmp_path / "queue"
    q.mkdir()
    return q


def _plan() -> SpawnPlan:
    return SpawnPlan(strategy=ResumeStrategy.FRESH, session_id=None, prompt="run", extra_args=[])


def _clean_run() -> RunRecord:
    return RunRecord(
        attempt=1,
        started_at=WHEN,
        finished_at=WHEN,
        stop_reason="end_turn",
        error=None,
        usage=TokenUsage(),
        duration_s=1.0,
    )


def _state() -> TaskState:
    return TaskState(task_id="t1", status="running", attempts=1)


def _finalize(
    worktree: Path,
    queue_dir: Path,
    task: Task,
    pre_sha: str | None,
    settings: DispatchSettings | None = None,
) -> tuple[TaskState, RunRecord]:
    return _finalize_state(
        prior=_state(),
        plan=_plan(),
        run=_clean_run(),
        summary=StreamSummary(),
        cap_violation=None,
        task=task,
        pre_sha=pre_sha,
        queue_dir=queue_dir,
        settings_dispatch=(
            settings if settings is not None else DispatchSettings(dispatch_block_file=BLOCK_FILE)
        ),
    )


def _rows(queue_dir: Path) -> list[dict]:
    path = queue_dir / BLOCK_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _skip_task(worktree: Path) -> Task:
    """A clean SKIP: deliverable on disk, no commit, clean worktree."""
    (worktree / "report.md").write_text("skipped: not a model paper\n")
    return Task(
        id="t1",
        title="t1",
        prompt="p",
        working_dir=worktree,
        deliverable_paths=[Path("report.md")],
    )


class TestTerminalCloseGate:
    def test_writes_a_row_and_keeps_completed(self, worktree: Path, queue_dir: Path) -> None:
        pre = _git(worktree, "rev-parse", "HEAD")
        state, run = _finalize(worktree, queue_dir, _skip_task(worktree), pre)
        # A terminal close is a genuine completion, not a failure.
        assert state.status == "completed"
        assert run.stop_reason == "end_turn"
        rows = _rows(queue_dir)
        assert len(rows) == 1
        assert rows[0]["task"] == "t1"
        assert rows[0]["block_dispatch"] is True
        # Auto-written rows must be distinguishable from curated ones so a
        # wrong heuristic can be audited and reversed in bulk.
        assert rows[0]["status"] == terminal_gate_mod.AUTO_GATE_STATUS
        assert rows[0]["signals"]["auto_gated"] is True
        assert rows[0]["deliverable"] == "report.md"

    def test_does_not_duplicate_or_overwrite_an_existing_row(
        self, worktree: Path, queue_dir: Path
    ) -> None:
        curated = {
            "task": "t1",
            "block_dispatch": True,
            "reason": "operator ruling: permanent skip",
            "status": "CLOSED",
        }
        (queue_dir / BLOCK_FILE).write_text(json.dumps(curated) + "\n")
        pre = _git(worktree, "rev-parse", "HEAD")
        _finalize(worktree, queue_dir, _skip_task(worktree), pre)
        rows = _rows(queue_dir)
        assert rows == [curated], "an operator's curated row must win untouched"

    def test_no_row_when_the_run_committed(self, worktree: Path, queue_dir: Path) -> None:
        # A run that produced code is not a terminal close; gating it would
        # strand real work behind a block the selector honours.
        pre = _git(worktree, "rev-parse", "HEAD")
        (worktree / "model.R").write_text("x <- 1\n")
        _git(worktree, "add", ".")
        _git(worktree, "commit", "-q", "-m", "add model")
        task = Task(
            id="t1",
            title="t1",
            prompt="p",
            working_dir=worktree,
            deliverable_paths=[Path("model.R")],
        )
        state, _ = _finalize(worktree, queue_dir, task, pre)
        assert _rows(queue_dir) == []
        assert state.status in {"completed", "failed"}

    def test_no_row_when_work_was_left_uncommitted(self, worktree: Path, queue_dir: Path) -> None:
        # This task MUST be re-dispatched to finish, so it must not be gated.
        pre = _git(worktree, "rev-parse", "HEAD")
        (worktree / "report.md").write_text("partial\n")
        (worktree / "half_done.R").write_text("x <- 1\n")
        task = Task(
            id="t1",
            title="t1",
            prompt="p",
            working_dir=worktree,
            deliverable_paths=[Path("report.md")],
        )
        state, run = _finalize(worktree, queue_dir, task, pre)
        assert _rows(queue_dir) == []
        assert state.status == "failed"
        assert run.stop_reason == "uncommitted_work_left"

    def test_noop_when_block_file_not_configured(self, worktree: Path, queue_dir: Path) -> None:
        pre = _git(worktree, "rev-parse", "HEAD")
        state, _ = _finalize(
            worktree,
            queue_dir,
            _skip_task(worktree),
            pre,
            settings=DispatchSettings(dispatch_block_file=None),
        )
        assert state.status == "completed"
        assert not (queue_dir / BLOCK_FILE).exists()

    def test_unwritable_register_does_not_fail_the_run(
        self, worktree: Path, queue_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Build the task and snapshot the SHA BEFORE patching: the patch must
        # break only the register append, not the fixture's own file writes.
        task = _skip_task(worktree)
        pre = _git(worktree, "rev-parse", "HEAD")

        def _boom(*_a: object, **_kw: object) -> None:
            raise OSError("read-only file system")

        monkeypatch.setattr(Path, "open", _boom)
        state, _ = _finalize(worktree, queue_dir, task, pre)
        assert state.status == "completed", "a register problem must not fail a good run"


class TestAlreadyGated:
    def test_ignores_rows_that_are_not_task_keyed_blocks(self, queue_dir: Path) -> None:
        # The register carries index rows and target_path re-acquisition rows
        # alongside real blocks; only an explicitly flagged, task-keyed row counts.
        (queue_dir / BLOCK_FILE).write_text(
            "\n".join(
                [
                    "",
                    "not json at all",
                    json.dumps({"task": "t1"}),
                    json.dumps({"task": "t1", "block_dispatch": False}),
                    json.dumps({"block_dispatch": True}),
                    json.dumps({"task": "other", "block_dispatch": True}),
                ]
            )
            + "\n"
        )
        assert not terminal_gate_mod.already_gated(queue_dir, BLOCK_FILE, "t1")
        assert terminal_gate_mod.already_gated(queue_dir, BLOCK_FILE, "other")

    def test_missing_file_reads_as_not_gated(self, queue_dir: Path) -> None:
        assert not terminal_gate_mod.already_gated(queue_dir, BLOCK_FILE, "t1")
