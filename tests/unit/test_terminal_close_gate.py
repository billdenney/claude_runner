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
* Run that left a sidecar question open => no row. It stopped to ask, not
  to close: answering makes it eligible again, which is how the answer
  reaches it. A zero-question notification, or a request whose questions
  are all answered, still gets the row.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_task_runner.config.loader import load_defaults
from claude_task_runner.config.schema import DispatchSettings
from claude_task_runner.queue.schema import (
    RunRecord,
    SidecarAnswer,
    SidecarOption,
    SidecarQuestion,
    SidecarRequest,
    SidecarResponse,
    Task,
    TaskState,
    TokenUsage,
)
from claude_task_runner.queue.sidecar import sidecar_dir_for, write_response
from claude_task_runner.queue.store import (
    state_path_for,
    task_path_for,
    write_state_atomic,
    write_task_atomic,
)
from claude_task_runner.runner import terminal_gate as terminal_gate_mod
from claude_task_runner.runner.dispatcher import _apply_open_sidecar, _finalize_state
from claude_task_runner.runner.orchestrator import _eligible_candidates
from claude_task_runner.runner.session import ResumeStrategy, SpawnPlan
from claude_task_runner.runner.stream import StreamSummary

from ._sidecar_files import write_request

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
    *,
    has_open_sidecar: bool = False,
) -> tuple[TaskState, RunRecord]:
    return _finalize_state(
        prior=_state(),
        plan=_plan(),
        run=_clean_run(),
        summary=StreamSummary(),
        cap_violation=None,
        task=task,
        pre_sha=pre_sha,
        has_open_sidecar=has_open_sidecar,
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


def _request(task_id: str, question_ids: tuple[str, ...]) -> SidecarRequest:
    """A sidecar request asking ``question_ids``; none makes it a notification."""
    return SidecarRequest(
        task_id=task_id,
        sequence=1,
        created_at=WHEN,
        summary="placeholder",
        context="placeholder",
        questions=[
            SidecarQuestion(
                id=qid,
                prompt=f"{qid}?",
                options=[
                    SidecarOption(value="A", label="yes", description=""),
                    SidecarOption(value="B", label="no", description=""),
                ],
                recommended="A",
            )
            for qid in question_ids
        ],
    )


def _answer(task_id: str, answer_ids: tuple[str, ...]) -> SidecarResponse:
    return SidecarResponse(
        task_id=task_id,
        sequence=1,
        responded_at=WHEN,
        answers=[SidecarAnswer(id=aid, value="A") for aid in answer_ids],
    )


def _expected_auto_row(task_id: str = "t1") -> dict[str, object]:
    """Every field of the row the gate writes for ``_skip_task``, except the
    two timestamps, which :func:`_assert_one_auto_row` checks separately."""
    return {
        "task": task_id,
        "block_dispatch": True,
        "needed": (
            "nothing - dispatch blocker only, written automatically when the run "
            "closed on a terminal disposition (ADR-0033)."
        ),
        "reason": (
            "AUTO-GATED on a terminal close: the run produced a deliverable and no "
            "commit, and left a clean worktree -- the shape of a skip or defer. "
            "Without this row, answering the task's sidecar would make it eligible "
            "again and it would re-derive the same disposition at full effort. "
            "stop_reason=end_turn. "
            "Review the deliverable before treating this as a settled ruling; "
            "replace this row with a curated one to record the real disposition."
        ),
        "deliverable": "report.md",
        "signals": {
            "blocker_type": "dispatch_blocker",
            "task": task_id,
            "severity": "closed",
            "blocks_dispatch": True,
            "auto_gated": True,
        },
        "status": "AUTO_GATED",
        "file_kind": "dispatch_blocker",
    }


def _assert_one_auto_row(queue_dir: Path) -> None:
    lines = (queue_dir / BLOCK_FILE).read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    added, detected_at = row.pop("added"), row.pop("detected_at")
    assert row == _expected_auto_row()
    assert detected_at.startswith(added)
    datetime.fromisoformat(detected_at)


class TestOpenSidecarQuestions:
    """A run with a deliverable and no commit, shaped by what its sidecar asks.

    ``has_open_sidecar`` is passed as the dispatcher computes it
    (``list_open_sidecars``), so each case sees what production sees.
    """

    def test_no_sidecar_is_gated(self, worktree: Path, queue_dir: Path) -> None:
        pre = _git(worktree, "rev-parse", "HEAD")
        state, _ = _finalize(worktree, queue_dir, _skip_task(worktree), pre)
        assert state.status == "completed"
        _assert_one_auto_row(queue_dir)

    def test_open_question_is_not_gated(self, worktree: Path, queue_dir: Path) -> None:
        write_request(queue_dir, _request("t1", ("q1", "q2")))
        pre = _git(worktree, "rev-parse", "HEAD")
        state, _ = _finalize(worktree, queue_dir, _skip_task(worktree), pre, has_open_sidecar=True)
        assert state.status == "completed"
        assert not (queue_dir / BLOCK_FILE).exists()

    def test_the_answer_reaches_the_task(self, worktree: Path, queue_dir: Path) -> None:
        """End to end: finalize as the dispatcher does, answer the sidecar,
        and the selector admits the task again."""
        task = _skip_task(worktree)
        write_task_atomic(task, task_path_for(queue_dir, task.id))
        write_request(queue_dir, _request("t1", ("q1",)))
        pre = _git(worktree, "rev-parse", "HEAD")
        state, _ = _finalize(worktree, queue_dir, task, pre, has_open_sidecar=True)
        state = _apply_open_sidecar(state, task=task, pre_sha=pre, settings_failure_classifier=None)
        assert state.status == "awaiting_sidecar"
        write_state_atomic(state, state_path_for(queue_dir, task.id))

        def admitted() -> list[str]:
            return [
                t.id
                for t in _eligible_candidates(
                    queue_dir,
                    {},
                    set(),
                    block_file=BLOCK_FILE,
                    effort_levels=load_defaults()["effort_levels"],
                )
            ]

        assert admitted() == []
        write_response(queue_dir, _answer("t1", ("q1",)))
        assert admitted() == ["t1"]

    def test_partly_answered_request_is_not_gated(self, worktree: Path, queue_dir: Path) -> None:
        """q2 is still outstanding, so the request is open (ADR-0031)."""
        write_request(queue_dir, _request("t1", ("q1", "q2")))
        write_response(queue_dir, _answer("t1", ("q1",)))
        pre = _git(worktree, "rev-parse", "HEAD")
        _finalize(worktree, queue_dir, _skip_task(worktree), pre, has_open_sidecar=True)
        assert not (queue_dir / BLOCK_FILE).exists()

    def test_unreadable_request_is_not_gated(self, worktree: Path, queue_dir: Path) -> None:
        """An undecidable request stays open until repaired, and may ask
        something, so its eventual answer must be able to reach the task."""
        (sidecar_dir_for(queue_dir, "t1") / "request-001.json").write_text("{not json")
        pre = _git(worktree, "rev-parse", "HEAD")
        _finalize(worktree, queue_dir, _skip_task(worktree), pre, has_open_sidecar=True)
        assert not (queue_dir / BLOCK_FILE).exists()

    def test_open_notification_is_gated(self, worktree: Path, queue_dir: Path) -> None:
        """Acknowledging a zero-question notification would re-dispatch the
        task; the row is what stops that."""
        write_request(queue_dir, _request("t1", ()))
        pre = _git(worktree, "rev-parse", "HEAD")
        _finalize(worktree, queue_dir, _skip_task(worktree), pre, has_open_sidecar=True)
        _assert_one_auto_row(queue_dir)

    def test_fully_answered_request_is_gated(self, worktree: Path, queue_dir: Path) -> None:
        """A rerun after the operator answered (e.g. "skip") that closes
        terminally without asking again is a real terminal close."""
        write_request(queue_dir, _request("t1", ("q1",)))
        write_response(queue_dir, _answer("t1", ("q1",)))
        pre = _git(worktree, "rev-parse", "HEAD")
        _finalize(worktree, queue_dir, _skip_task(worktree), pre)
        _assert_one_auto_row(queue_dir)

    def test_another_tasks_open_question_does_not_matter(
        self, worktree: Path, queue_dir: Path
    ) -> None:
        write_request(queue_dir, _request("other", ("q1",)))
        pre = _git(worktree, "rev-parse", "HEAD")
        _finalize(worktree, queue_dir, _skip_task(worktree), pre)
        _assert_one_auto_row(queue_dir)

    def test_committed_run_is_not_gated(self, worktree: Path, queue_dir: Path) -> None:
        """Unchanged by the sidecar rule: a commit means no terminal close,
        even when the open sidecar is a notification the gate would act on."""
        write_request(queue_dir, _request("t1", ()))
        task = _skip_task(worktree)
        pre = _git(worktree, "rev-parse", "HEAD")
        (worktree / "model.R").write_text("x <- 1\n")
        _git(worktree, "add", ".")
        _git(worktree, "commit", "-q", "-m", "add model")
        state, _ = _finalize(worktree, queue_dir, task, pre, has_open_sidecar=True)
        assert state.status == "completed"
        assert not (queue_dir / BLOCK_FILE).exists()


class TestAwaitsAnswers:
    def test_no_sidecar_directory(self, queue_dir: Path) -> None:
        assert not terminal_gate_mod.awaits_answers(queue_dir, "t1")

    def test_open_question_answered_question_and_notification(self, queue_dir: Path) -> None:
        write_request(queue_dir, _request("asks", ("q1",)))
        write_request(queue_dir, _request("answered", ("q1",)))
        write_response(queue_dir, _answer("answered", ("q1",)))
        write_request(queue_dir, _request("notifies", ()))
        assert terminal_gate_mod.awaits_answers(queue_dir, "asks")
        assert not terminal_gate_mod.awaits_answers(queue_dir, "answered")
        assert not terminal_gate_mod.awaits_answers(queue_dir, "notifies")
        assert not terminal_gate_mod.awaits_answers(queue_dir, "absent")
