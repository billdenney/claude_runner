"""Tests for the output-evidence gate added in ADR-0020.

The gate flips a would-be ``completed`` status to ``failed`` with
stop_reason ``end_turn_no_output`` when a clean-exit run produced no
observable artifact (no new commit on the worktree branch, no open
sidecar, no declared deliverable on disk). The gate is skipped when
``task.working_dir is None`` so research/analysis tasks that
intentionally run without a worktree keep the legacy behavior.

The non-trivial branches exercised here:

* Task with a fresh commit on the branch ⇒ ``completed``.
* Task with a declared deliverable path that exists ⇒ ``completed``.
* Task with no commit, no sidecar, no deliverable ⇒ ``failed`` with
  ``stop_reason="end_turn_no_output"``.
* Task with an open sidecar ⇒ the existing ``awaiting_sidecar``
  override still wins (no regression).
* Task with ``working_dir is None`` ⇒ legacy ``completed`` path
  (gate skipped entirely).
* Pre-dispatch SHA snapshot against a non-git cwd returns ``None``
  cleanly (gate degrades to sidecar/deliverable evidence; dispatch
  itself does not fail).
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_task_runner.queue.schema import (
    RunRecord,
    Task,
    TaskState,
    TokenUsage,
)
from claude_task_runner.runner.dispatcher import (
    OutputEvidence,
    _finalize_state,
    _new_commit_since,
    _snapshot_pre_dispatch_sha,
    _verify_output_evidence,
)
from claude_task_runner.runner.session import ResumeStrategy, SpawnPlan
from claude_task_runner.runner.stream import StreamSummary

WHEN = datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def fresh_plan() -> SpawnPlan:
    return SpawnPlan(
        strategy=ResumeStrategy.FRESH,
        session_id=None,
        prompt="run",
        extra_args=[],
    )


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def git_worktree(tmp_path: Path) -> Path:
    """A minimal initialized git repo with one commit on ``main``."""
    repo = tmp_path / "wt"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("seed\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _clean_run() -> RunRecord:
    """A RunRecord that mirrors the live ``end_turn`` no-output incident:
    clean exit, ``stop_reason="end_turn"``, no error. The gate's only
    trigger condition."""
    return RunRecord(
        attempt=1,
        started_at=WHEN,
        finished_at=WHEN,
        stop_reason="end_turn",
        error=None,
        usage=TokenUsage(),
        duration_s=1.0,
    )


def _task(*, working_dir: Path | None, deliverable_paths: list[Path] | None = None) -> Task:
    return Task(
        id="t1",
        title="t1",
        prompt="do the thing",
        working_dir=working_dir,
        deliverable_paths=deliverable_paths or [],
    )


# --- _snapshot_pre_dispatch_sha ----------------------------------------


class TestSnapshotPreDispatchSha:
    def test_returns_head_sha_for_valid_repo(self, git_worktree: Path) -> None:
        expected = _git(git_worktree, "rev-parse", "HEAD")
        assert _snapshot_pre_dispatch_sha(git_worktree) == expected

    def test_returns_none_when_working_dir_is_none(self) -> None:
        assert _snapshot_pre_dispatch_sha(None) is None

    def test_returns_none_for_non_git_directory(self, tmp_path: Path) -> None:
        # `git rev-parse HEAD` exits non-zero outside a repo. The helper
        # must swallow the failure and return None so dispatch is not
        # blocked when a task's working_dir isn't a git checkout.
        non_repo = tmp_path / "plain"
        non_repo.mkdir()
        assert _snapshot_pre_dispatch_sha(non_repo) is None

    def test_returns_none_when_git_missing(
        self, git_worktree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("git binary missing")

        monkeypatch.setattr("claude_task_runner.runner.dispatcher.subprocess.run", _raise)
        assert _snapshot_pre_dispatch_sha(git_worktree) is None


# --- _new_commit_since --------------------------------------------------


class TestNewCommitSince:
    def test_false_when_head_unchanged(self, git_worktree: Path) -> None:
        sha = _git(git_worktree, "rev-parse", "HEAD")
        assert _new_commit_since(git_worktree, sha) is False

    def test_true_after_new_commit(self, git_worktree: Path) -> None:
        sha = _git(git_worktree, "rev-parse", "HEAD")
        (git_worktree / "out.txt").write_text("work product\n")
        _git(git_worktree, "add", ".")
        _git(git_worktree, "commit", "-q", "-m", "add output")
        assert _new_commit_since(git_worktree, sha) is True

    def test_false_for_bogus_sha(self, git_worktree: Path) -> None:
        # An unknown SHA makes `git rev-list` exit non-zero; the helper
        # treats that as "no new commit observed."
        assert _new_commit_since(git_worktree, "0" * 40) is False

    def test_subprocess_error_returns_false(
        self, git_worktree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Helper degrades to "no commit" rather than raising so a flaky
        # git invocation doesn't take a successful run down with it.
        def _raise(*_a: object, **_kw: object) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("git binary missing")

        monkeypatch.setattr("claude_task_runner.runner.dispatcher.subprocess.run", _raise)
        assert _new_commit_since(git_worktree, "abc") is False

    def test_unparseable_count_returns_false(
        self, git_worktree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If a future git version returns text we can't int(), don't
        # mistakenly call it a success — treat as "no commit."
        fake = subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout="not-a-number\n", stderr=""
        )
        monkeypatch.setattr(
            "claude_task_runner.runner.dispatcher.subprocess.run",
            lambda *_a, **_kw: fake,
        )
        assert _new_commit_since(git_worktree, "abc") is False


# --- _verify_output_evidence -------------------------------------------


class TestVerifyOutputEvidence:
    def test_no_evidence(self, git_worktree: Path) -> None:
        sha = _git(git_worktree, "rev-parse", "HEAD")
        ev = _verify_output_evidence(
            task=_task(working_dir=git_worktree),
            pre_sha=sha,
            has_open_sidecar=False,
        )
        assert ev == OutputEvidence(has_commit=False, has_sidecar=False, has_deliverable=False)
        assert ev.any is False
        assert "no new commit" in ev.missed_gates()
        assert "no open sidecar" in ev.missed_gates()
        assert "no declared deliverable" in ev.missed_gates()

    def test_missed_gates_omits_satisfied_checks(self) -> None:
        # Only the failing gates appear in the human-readable string;
        # satisfied checks are not listed. Useful when one gate passes
        # but the operator still wants to know which others would
        # otherwise have flagged.
        partial = OutputEvidence(has_commit=True, has_sidecar=False, has_deliverable=True)
        msg = partial.missed_gates()
        assert "no new commit" not in msg
        assert "no open sidecar" in msg
        assert "no declared deliverable" not in msg

    def test_commit_satisfies_gate(self, git_worktree: Path) -> None:
        sha = _git(git_worktree, "rev-parse", "HEAD")
        (git_worktree / "out.R").write_text("# stub\n")
        _git(git_worktree, "add", ".")
        _git(git_worktree, "commit", "-q", "-m", "stub")
        ev = _verify_output_evidence(
            task=_task(working_dir=git_worktree),
            pre_sha=sha,
            has_open_sidecar=False,
        )
        assert ev.has_commit is True
        assert ev.any is True

    def test_sidecar_satisfies_gate(self, git_worktree: Path) -> None:
        sha = _git(git_worktree, "rev-parse", "HEAD")
        ev = _verify_output_evidence(
            task=_task(working_dir=git_worktree),
            pre_sha=sha,
            has_open_sidecar=True,
        )
        assert ev.has_sidecar is True
        assert ev.any is True

    def test_deliverable_relative_path_satisfies_gate(self, git_worktree: Path) -> None:
        sha = _git(git_worktree, "rev-parse", "HEAD")
        (git_worktree / "inst").mkdir()
        (git_worktree / "inst" / "out.R").write_text("written\n")
        ev = _verify_output_evidence(
            task=_task(
                working_dir=git_worktree,
                deliverable_paths=[Path("inst/out.R")],
            ),
            pre_sha=sha,
            has_open_sidecar=False,
        )
        assert ev.has_deliverable is True
        assert ev.any is True

    def test_deliverable_missing_path(self, git_worktree: Path) -> None:
        ev = _verify_output_evidence(
            task=_task(
                working_dir=git_worktree,
                deliverable_paths=[Path("inst/never.R")],
            ),
            pre_sha=None,
            has_open_sidecar=False,
        )
        assert ev.has_deliverable is False

    def test_deliverable_absolute_path(self, tmp_path: Path, git_worktree: Path) -> None:
        deliverable = tmp_path / "external_output.csv"
        deliverable.write_text("col1\n1\n")
        ev = _verify_output_evidence(
            task=_task(
                working_dir=git_worktree,
                deliverable_paths=[deliverable],
            ),
            pre_sha=None,
            has_open_sidecar=False,
        )
        assert ev.has_deliverable is True


# --- _finalize_state gating -------------------------------------------


class TestFinalizeStateOutputGate:
    """End-to-end checks for the gate inside ``_finalize_state``.

    Direct calls (not through ``dispatch()``) because the gate is a
    pure transform on (state, run, evidence)."""

    def test_completes_when_commit_present(self, git_worktree: Path, fresh_plan: SpawnPlan) -> None:
        sha = _git(git_worktree, "rev-parse", "HEAD")
        (git_worktree / "f.R").write_text("ok\n")
        _git(git_worktree, "add", ".")
        _git(git_worktree, "commit", "-q", "-m", "work")
        prior = TaskState(task_id="t1")
        new_state, new_run = _finalize_state(
            prior=prior,
            plan=fresh_plan,
            run=_clean_run(),
            summary=StreamSummary(),
            cap_violation=None,
            task=_task(working_dir=git_worktree),
            pre_sha=sha,
            has_open_sidecar=False,
        )
        assert new_state.status == "completed"
        assert new_run.stop_reason == "end_turn"
        assert new_run.error is None

    def test_sidecar_path_unaffected_by_gate(
        self, git_worktree: Path, fresh_plan: SpawnPlan
    ) -> None:
        # When a sidecar is open, the gate sees evidence and lets the
        # run reach ``completed`` — the *separate* awaiting_sidecar
        # override (still in ``dispatch()``) is what changes the status
        # afterward. The gate must not pre-flip this to ``failed``.
        sha = _git(git_worktree, "rev-parse", "HEAD")
        prior = TaskState(task_id="t1")
        new_state, new_run = _finalize_state(
            prior=prior,
            plan=fresh_plan,
            run=_clean_run(),
            summary=StreamSummary(),
            cap_violation=None,
            task=_task(working_dir=git_worktree),
            pre_sha=sha,
            has_open_sidecar=True,
        )
        assert new_state.status == "completed"
        assert new_run.stop_reason == "end_turn"

    def test_declared_deliverable_satisfies_gate(
        self, git_worktree: Path, fresh_plan: SpawnPlan
    ) -> None:
        sha = _git(git_worktree, "rev-parse", "HEAD")
        (git_worktree / "report.md").write_text("done\n")
        prior = TaskState(task_id="t1")
        new_state, new_run = _finalize_state(
            prior=prior,
            plan=fresh_plan,
            run=_clean_run(),
            summary=StreamSummary(),
            cap_violation=None,
            task=_task(
                working_dir=git_worktree,
                deliverable_paths=[Path("report.md")],
            ),
            pre_sha=sha,
            has_open_sidecar=False,
        )
        assert new_state.status == "completed"
        assert new_run.error is None

    def test_no_evidence_flips_to_failed(self, git_worktree: Path, fresh_plan: SpawnPlan) -> None:
        sha = _git(git_worktree, "rev-parse", "HEAD")
        prior = TaskState(task_id="t1")
        new_state, new_run = _finalize_state(
            prior=prior,
            plan=fresh_plan,
            run=_clean_run(),
            summary=StreamSummary(),
            cap_violation=None,
            task=_task(working_dir=git_worktree),
            pre_sha=sha,
            has_open_sidecar=False,
        )
        assert new_state.status == "failed"
        assert new_run.stop_reason == "end_turn_no_output"
        assert new_run.error is not None
        assert "no new commit" in new_run.error
        assert "no open sidecar" in new_run.error
        assert "no declared deliverable" in new_run.error
        # State's stop_reason / error mirror the amended run record so
        # the persisted YAML carries the same signal the run history does.
        assert new_state.stop_reason == "end_turn_no_output"
        assert new_state.error is not None
        # The amended RunRecord is what ends up in `runs`.
        assert new_state.runs[-1].stop_reason == "end_turn_no_output"

    def test_working_dir_none_skips_gate(self, fresh_plan: SpawnPlan) -> None:
        # Legacy non-worktree-task path: working_dir is None means the
        # runner has no anchor for a commit check. The task is treated
        # as completed regardless of artifact evidence — the ADR
        # explicitly preserves this behavior.
        prior = TaskState(task_id="t1")
        new_state, new_run = _finalize_state(
            prior=prior,
            plan=fresh_plan,
            run=_clean_run(),
            summary=StreamSummary(),
            cap_violation=None,
            task=_task(working_dir=None),
            pre_sha=None,
            has_open_sidecar=False,
        )
        assert new_state.status == "completed"
        assert new_run.stop_reason == "end_turn"

    def test_missing_pre_sha_still_checks_sidecar_and_deliverable(
        self, git_worktree: Path, fresh_plan: SpawnPlan
    ) -> None:
        # Pre-dispatch SHA capture can fail (e.g. cwd not a git repo).
        # The gate must degrade to sidecar / deliverable checks, not
        # spuriously pass *or* fail.
        (git_worktree / "report.md").write_text("done\n")
        prior = TaskState(task_id="t1")
        new_state, _new_run = _finalize_state(
            prior=prior,
            plan=fresh_plan,
            run=_clean_run(),
            summary=StreamSummary(),
            cap_violation=None,
            task=_task(
                working_dir=git_worktree,
                deliverable_paths=[Path("report.md")],
            ),
            pre_sha=None,
            has_open_sidecar=False,
        )
        assert new_state.status == "completed"

    def test_missing_pre_sha_no_sidecar_no_deliverable_fails(
        self, git_worktree: Path, fresh_plan: SpawnPlan
    ) -> None:
        prior = TaskState(task_id="t1")
        new_state, new_run = _finalize_state(
            prior=prior,
            plan=fresh_plan,
            run=_clean_run(),
            summary=StreamSummary(),
            cap_violation=None,
            task=_task(working_dir=git_worktree),
            pre_sha=None,
            has_open_sidecar=False,
        )
        assert new_state.status == "failed"
        assert new_run.stop_reason == "end_turn_no_output"

    def test_gate_does_not_re_run_on_already_failed(
        self, git_worktree: Path, fresh_plan: SpawnPlan
    ) -> None:
        # If the run came back with an error or a non-completing
        # stop_reason, the status is already ``failed`` and the gate
        # must not touch the RunRecord. (Regression guard: an earlier
        # draft re-classified ``stop_sequence`` as
        # ``end_turn_no_output`` because the gate ran unconditionally.)
        prior = TaskState(task_id="t1")
        failing_run = RunRecord(
            attempt=1,
            started_at=WHEN,
            finished_at=WHEN,
            stop_reason="stop_sequence",
            error=None,
            usage=TokenUsage(),
            duration_s=1.0,
        )
        new_state, new_run = _finalize_state(
            prior=prior,
            plan=fresh_plan,
            run=failing_run,
            summary=StreamSummary(),
            cap_violation=None,
            task=_task(working_dir=git_worktree),
            pre_sha=None,
            has_open_sidecar=False,
        )
        assert new_state.status == "failed"
        assert new_run.stop_reason == "stop_sequence"
