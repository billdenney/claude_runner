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
  are all answered, gets the row.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_task_runner.config.loader import load_defaults
from claude_task_runner.config.schema import DispatchSettings
from claude_task_runner.queue.schema import RunRecord, Task, TaskState, TokenUsage
from claude_task_runner.queue.sidecar import list_open_sidecars, sidecar_dir_for, write_response
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

from ._sidecar_files import make_request, make_response, write_request

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
    # ``has_open_sidecar`` is computed exactly as the dispatcher's finalize
    # call sites compute it.
    return _finalize_state(
        prior=_state(),
        plan=_plan(),
        run=_clean_run(),
        summary=StreamSummary(),
        cap_violation=None,
        task=task,
        pre_sha=pre_sha,
        has_open_sidecar=any(tid == task.id for tid, _seq, _path in list_open_sidecars(queue_dir)),
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

    def test_a_line_that_is_not_utf8_is_skipped(self, queue_dir: Path) -> None:
        (queue_dir / BLOCK_FILE).write_bytes(
            b"\xff{}\n" + json.dumps({"task": "t1", "block_dispatch": True}).encode() + b"\n"
        )
        assert terminal_gate_mod.already_gated(queue_dir, BLOCK_FILE, "t1")

    def test_a_path_no_file_can_have_reads_as_not_gated(self, queue_dir: Path) -> None:
        assert not terminal_gate_mod.already_gated(queue_dir, "needs\x00acquisition.jsonl", "t1")

    def test_a_path_no_file_can_have_is_not_appended_to(self, queue_dir: Path) -> None:
        """The append fails as an unwritable register does: the run stands,
        and the task stays ungated."""
        assert not terminal_gate_mod.ensure_terminal_gate(
            queue_dir=queue_dir,
            block_file="needs\x00acquisition.jsonl",
            task_id="t1",
            stop_reason="end_turn",
            deliverable=None,
        )


def _expected_auto_row() -> dict[str, object]:
    """Every field of the row the gate writes for ``_skip_task``, except the
    two timestamps, which :func:`_assert_one_auto_row` checks separately."""
    return {
        "task": "t1",
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
            "task": "t1",
            "severity": "closed",
            "blocks_dispatch": True,
            "auto_gated": True,
        },
        "status": "AUTO_GATED",
        "file_kind": "dispatch_blocker",
    }


def _assert_one_auto_row(queue_dir: Path) -> None:
    (row,) = _rows(queue_dir)
    added, detected_at = row.pop("added"), row.pop("detected_at")
    assert row == _expected_auto_row()
    assert detected_at.startswith(added)
    datetime.fromisoformat(detected_at)


def _sidecar_file(queue_dir: Path, name: str) -> Path:
    return sidecar_dir_for(queue_dir, "t1") / name


class TestOpenSidecarQuestions:
    """A run with a deliverable and no commit, shaped by what its sidecar asks."""

    def test_no_sidecar_is_gated(self, worktree: Path, queue_dir: Path) -> None:
        pre = _git(worktree, "rev-parse", "HEAD")
        state, _ = _finalize(worktree, queue_dir, _skip_task(worktree), pre)
        assert state.status == "completed"
        _assert_one_auto_row(queue_dir)

    def test_open_question_is_not_gated(self, worktree: Path, queue_dir: Path) -> None:
        write_request(queue_dir, make_request("t1", question_ids=("q1", "q2")))
        pre = _git(worktree, "rev-parse", "HEAD")
        state, _ = _finalize(worktree, queue_dir, _skip_task(worktree), pre)
        assert state.status == "completed"
        assert not (queue_dir / BLOCK_FILE).exists()

    def test_the_answer_reaches_the_task(self, worktree: Path, queue_dir: Path) -> None:
        """End to end: finalize as the dispatcher does, answer the sidecar,
        and the selector admits the task again."""
        task = _skip_task(worktree)
        write_task_atomic(task, task_path_for(queue_dir, task.id))
        write_request(queue_dir, make_request("t1"))
        pre = _git(worktree, "rev-parse", "HEAD")
        state, _ = _finalize(worktree, queue_dir, task, pre)
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
        write_response(queue_dir, make_response("t1"))
        assert admitted() == ["t1"]

    def test_partly_answered_request_is_not_gated(self, worktree: Path, queue_dir: Path) -> None:
        """q2 is still outstanding, so the request is open (ADR-0031)."""
        write_request(queue_dir, make_request("t1", question_ids=("q1", "q2")))
        write_response(queue_dir, make_response("t1", answer_ids=("q1",)))
        pre = _git(worktree, "rev-parse", "HEAD")
        _finalize(worktree, queue_dir, _skip_task(worktree), pre)
        assert not (queue_dir / BLOCK_FILE).exists()

    @pytest.mark.parametrize(
        ("request_text", "response_text"),
        [
            pytest.param("{not json", None, id="request-not-json"),
            pytest.param(
                json.dumps({"task_id": "t1", "questions": [{"prompt": "which?"}]}),
                None,
                id="question-without-id",
            ),
            pytest.param(
                make_request("t1").model_dump_json(),
                json.dumps({"task_id": "t1", "sequence": 1}),
                id="response-without-answers",
            ),
        ],
    )
    def test_unreadable_sidecar_is_not_gated(
        self, worktree: Path, queue_dir: Path, request_text: str, response_text: str | None
    ) -> None:
        """A request or response that cannot be read well enough to tell
        what is answered stays open until repaired, and may be asking, so
        its eventual answer must be able to reach the task."""
        _sidecar_file(queue_dir, "request-001.json").write_text(request_text)
        if response_text is not None:
            _sidecar_file(queue_dir, "response-001.json").write_text(response_text)
        (item,) = terminal_gate_mod.unanswered_asks(queue_dir, "t1")
        assert item.error is not None
        pre = _git(worktree, "rev-parse", "HEAD")
        _finalize(worktree, queue_dir, _skip_task(worktree), pre)
        assert not (queue_dir / BLOCK_FILE).exists()

    def test_open_notification_is_gated(self, worktree: Path, queue_dir: Path) -> None:
        """Acknowledging a zero-question notification would re-dispatch the
        task; the row is what stops that."""
        write_request(queue_dir, make_request("t1", question_ids=()))
        pre = _git(worktree, "rev-parse", "HEAD")
        _finalize(worktree, queue_dir, _skip_task(worktree), pre)
        _assert_one_auto_row(queue_dir)

    def test_fully_answered_request_is_gated(self, worktree: Path, queue_dir: Path) -> None:
        """A rerun after the operator answered (e.g. "skip") that closes
        terminally without asking again is a real terminal close."""
        write_request(queue_dir, make_request("t1"))
        write_response(queue_dir, make_response("t1"))
        pre = _git(worktree, "rev-parse", "HEAD")
        _finalize(worktree, queue_dir, _skip_task(worktree), pre)
        _assert_one_auto_row(queue_dir)

    def test_another_tasks_open_question_does_not_matter(
        self, worktree: Path, queue_dir: Path
    ) -> None:
        write_request(queue_dir, make_request("other"))
        pre = _git(worktree, "rev-parse", "HEAD")
        _finalize(worktree, queue_dir, _skip_task(worktree), pre)
        _assert_one_auto_row(queue_dir)

    def test_committed_run_is_not_gated(self, worktree: Path, queue_dir: Path) -> None:
        """Unchanged by the sidecar rule: a commit means no terminal close,
        even when the open sidecar is a notification the gate would act on."""
        write_request(queue_dir, make_request("t1", question_ids=()))
        task = _skip_task(worktree)
        pre = _git(worktree, "rev-parse", "HEAD")
        (worktree / "model.R").write_text("x <- 1\n")
        _git(worktree, "add", ".")
        _git(worktree, "commit", "-q", "-m", "add model")
        state, _ = _finalize(worktree, queue_dir, task, pre)
        assert state.status == "completed"
        assert not (queue_dir / BLOCK_FILE).exists()


class TestUnansweredAsks:
    def test_no_sidecar_directory(self, queue_dir: Path) -> None:
        assert terminal_gate_mod.unanswered_asks(queue_dir, "t1") == []

    def test_open_question_answered_question_and_notification(self, queue_dir: Path) -> None:
        write_request(queue_dir, make_request("asks", question_ids=("q1", "q2")))
        write_response(queue_dir, make_response("asks", answer_ids=("q1",)))
        write_request(queue_dir, make_request("answered"))
        write_response(queue_dir, make_response("answered"))
        write_request(queue_dir, make_request("notifies", question_ids=()))

        (item,) = terminal_gate_mod.unanswered_asks(queue_dir, "asks")
        assert (item.task_id, item.sequence, item.outstanding) == ("asks", 1, ("q2",))
        assert terminal_gate_mod.unanswered_asks(queue_dir, "answered") == []
        assert terminal_gate_mod.unanswered_asks(queue_dir, "notifies") == []
        assert terminal_gate_mod.unanswered_asks(queue_dir, "absent") == []

    def test_skip_is_logged_with_the_request(
        self, worktree: Path, queue_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A stub logger: structlog's routing depends on whether an earlier
        # test configured logging, so capturing its output is order-dependent.
        lines: list[str] = []
        monkeypatch.setattr(
            terminal_gate_mod,
            "logger",
            SimpleNamespace(info=lambda msg, *args: lines.append(msg % args)),
        )
        write_request(queue_dir, make_request("t1", question_ids=("q1", "q2")))
        _sidecar_file(queue_dir, "request-002.json").write_text("{not json")
        pre = _git(worktree, "rev-parse", "HEAD")
        _finalize(worktree, queue_dir, _skip_task(worktree), pre)
        (line,) = lines
        assert line.startswith(
            "not auto-gating task t1: its open sidecar still asks the operator: "
            "request-001 asks q1, q2; request-002 unreadable ("
        )
