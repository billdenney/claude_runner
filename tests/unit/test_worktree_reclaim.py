"""Worktree reclamation (ADR-0034) against a real fixture repository.

The contract: a task's worktree goes only when the task is ``completed``, its
branch is merged into ``origin/main`` (as seen after a fetch), and nothing
uncommitted would be lost. Every other case keeps the worktree -- and the
branch -- untouched. See ``_git_world.py`` for the repository layout.
"""

from __future__ import annotations

import contextlib
import fcntl
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, get_args

import pytest

from claude_task_runner.config.schema import WorktreeReclaimSettings
from claude_task_runner.queue.schema import TaskStatus
from claude_task_runner.queue.store import state_path_for, todo_dir
from claude_task_runner.worktree import reclaim as reclaim_mod
from claude_task_runner.worktree.reclaim import (
    KeepReason,
    Outcome,
    ReclaimError,
    ReclaimReport,
    ReclaimResult,
    reclaim_worktrees,
)

from ._git_world import World, git, real

NON_COMPLETED_STATUSES = sorted(set(get_args(TaskStatus)) - {"completed"})
"""Every status the reclaim must never touch, enumerated from the schema so a
newly added status is covered (and kept) automatically."""

LOCK_REL = ".run/setup_worktree.lock"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World.create(tmp_path, monkeypatch)


def run(
    world: World,
    *,
    apply: bool = True,
    settings: WorktreeReclaimSettings | None = None,
    **kwargs: Any,
) -> ReclaimReport:
    return reclaim_worktrees(
        world.queue, settings or WorktreeReclaimSettings(), apply=apply, **kwargs
    )


def only(report: ReclaimReport, task_id: str) -> ReclaimResult:
    matches = [r for r in report.results if r.task_id == task_id]
    assert len(matches) == 1, report.results
    return matches[0]


def assert_untouched(world: World, task_id: str) -> None:
    wt = world.worktree(task_id)
    assert (wt / ".git").is_file(), f"{wt} was removed"
    assert real(wt) in world.registered_worktrees()
    assert world.branch_exists(f"claude/{task_id}")


# ---------------------------------------------------------------------------
# reclaimed
# ---------------------------------------------------------------------------


class TestReclaimed:
    def test_completed_merged_clean_worktree_is_reclaimed(self, world: World) -> None:
        wt = world.add_task("t-merged")
        # The merge landed from the consolidator, so repo's own origin/main is
        # stale: only the reclaim's fetch can reveal that the branch is merged.
        assert not world.is_ancestor("claude/t-merged", "origin/main")

        report = run(world)

        result = only(report, "t-merged")
        assert result.outcome is Outcome.RECLAIMED
        assert result.reason is None
        assert result.forced is False
        assert result.discarded == ()
        assert result.branch_deleted is True
        assert result.branch == "claude/t-merged"
        assert not wt.exists()
        assert real(wt) not in world.registered_worktrees()
        assert not world.branch_exists("claude/t-merged")
        assert world.is_ancestor("origin/main", "origin/main")
        assert report.ok is True
        assert report.errors == ()
        assert report.counts() == {
            "seen": 1,
            "reclaimed": 1,
            "would_reclaim": 0,
            "kept": 0,
            "failed": 0,
            "branch_kept": 0,
            "kept_by_reason": {},
        }

    def test_task_that_committed_nothing_is_reclaimed(self, world: World) -> None:
        """A clean skip: the branch still points at its fork point on main."""
        wt = world.add_task("t-skip", commit=False, merge=False)
        result = only(run(world), "t-skip")
        assert result.outcome is Outcome.RECLAIMED
        assert not wt.exists()
        assert not world.branch_exists("claude/t-skip")

    def test_ignored_build_output_does_not_block(self, world: World) -> None:
        """git's own worktree removal treats ignored files as disposable."""
        wt = world.add_task("t-build")
        (wt / "src").mkdir()
        (wt / "src" / "model.o").write_bytes(b"\x7fELF")
        result = only(run(world), "t-build")
        assert result.outcome is Outcome.RECLAIMED
        assert result.forced is False
        assert not wt.exists()

    def test_deliverable_outside_the_worktree_is_irrelevant(self, world: World) -> None:
        report_path = world.queue / "reports" / "t-report.md"
        report_path.parent.mkdir()
        report_path.write_text("# done\n")
        wt = world.add_task("t-report", deliverable_paths=[report_path])
        result = only(run(world), "t-report")
        assert result.outcome is Outcome.RECLAIMED
        assert not wt.exists()
        assert report_path.exists()

    def test_dry_run_touches_nothing(self, world: World) -> None:
        world.add_task("t-dry")
        report = run(world, apply=False)
        result = only(report, "t-dry")
        assert result.outcome is Outcome.WOULD_RECLAIM
        assert result.branch_deleted is None
        assert report.applied is False
        assert_untouched(world, "t-dry")
        assert report.summary() == (
            "dry run: 1 worktree(s) seen; 1 reclaimable; kept 0 (status), 0 (unmerged), "
            "0 (uncommitted work), 0 (other); 0 failed"
        )


# ---------------------------------------------------------------------------
# the discardable-untracked allow-list
# ---------------------------------------------------------------------------


def _write_problems(wt: Path) -> None:
    problems = wt / "tests" / "testthat" / "_problems"
    problems.mkdir()
    (problems / "test-model-1.md").write_text("failure snapshot\n")


class TestAllowList:
    def test_allow_listed_untracked_paths_are_reclaimed_with_force(self, world: World) -> None:
        wt = world.add_task("t-problems")
        _write_problems(wt)
        result = only(run(world), "t-problems")
        assert result.outcome is Outcome.RECLAIMED
        assert result.forced is True
        assert result.discarded == ("tests/testthat/_problems/",)
        assert result.branch_deleted is True
        assert not wt.exists()

    def test_dry_run_reports_the_force(self, world: World) -> None:
        wt = world.add_task("t-problems")
        _write_problems(wt)
        result = only(run(world, apply=False), "t-problems")
        assert result.outcome is Outcome.WOULD_RECLAIM
        assert result.forced is True
        assert result.discarded == ("tests/testthat/_problems/",)
        assert_untouched(world, "t-problems")

    def test_untracked_file_outside_the_allow_list_keeps_it(self, world: World) -> None:
        wt = world.add_task("t-notes")
        (wt / "notes.txt").write_text("half-finished thought\n")
        result = only(run(world), "t-notes")
        assert (result.outcome, result.reason) == (Outcome.KEPT, KeepReason.DIRTY)
        assert result.detail == "uncommitted: ?? notes.txt"
        assert_untouched(world, "t-notes")

    def test_allow_listed_plus_other_untracked_keeps_it(self, world: World) -> None:
        wt = world.add_task("t-mixed")
        _write_problems(wt)
        (wt / "models" / "Draft_2024.R").write_text("draft\n")
        result = only(run(world), "t-mixed")
        assert (result.outcome, result.reason) == (Outcome.KEPT, KeepReason.DIRTY)
        assert result.detail == "uncommitted: ?? models/Draft_2024.R"
        assert (wt / "tests" / "testthat" / "_problems" / "test-model-1.md").exists()

    def test_modified_tracked_file_under_the_prefix_keeps_it(self, world: World) -> None:
        """The allow-list covers UNTRACKED paths only."""
        wt = world.add_task("t-tracked", extra_files=("tests/testthat/_problems/keep.md",))
        (wt / "tests" / "testthat" / "_problems" / "keep.md").write_text("edited\n")
        result = only(run(world), "t-tracked")
        assert (result.outcome, result.reason) == (Outcome.KEPT, KeepReason.DIRTY)
        assert result.detail == "uncommitted:  M tests/testthat/_problems/keep.md"

    def test_empty_allow_list_requires_a_clean_status(self, world: World) -> None:
        wt = world.add_task("t-strict")
        _write_problems(wt)
        settings = WorktreeReclaimSettings(discardable_untracked=[])
        result = only(run(world, settings=settings), "t-strict")
        assert (result.outcome, result.reason) == (Outcome.KEPT, KeepReason.DIRTY)
        assert result.detail == "uncommitted: ?? tests/testthat/_problems/"

    def test_wholly_untracked_parent_is_not_matched_by_a_deeper_entry(self, world: World) -> None:
        """git collapses an all-untracked tree to its top directory; that is
        not the allow-listed path, so the worktree is kept rather than guessed."""
        wt = world.add_task("t-deep")
        (wt / "build" / "cache").mkdir(parents=True)
        (wt / "build" / "cache" / "blob").write_text("x\n")
        settings = WorktreeReclaimSettings(discardable_untracked=["build/cache/"])
        result = only(run(world, settings=settings), "t-deep")
        assert (result.outcome, result.reason) == (Outcome.KEPT, KeepReason.DIRTY)
        assert result.detail == "uncommitted: ?? build/"

    def test_file_entry_matches_exactly(self, world: World) -> None:
        wt = world.add_task("t-file")
        (wt / "Rplots.pdf").write_text("%PDF\n")
        settings = WorktreeReclaimSettings(discardable_untracked=["Rplots.pdf"])
        result = only(run(world, settings=settings), "t-file")
        assert result.outcome is Outcome.RECLAIMED
        assert result.discarded == ("Rplots.pdf",)


# ---------------------------------------------------------------------------
# kept
# ---------------------------------------------------------------------------


class TestKept:
    def test_named_statuses_are_in_the_enumeration(self) -> None:
        assert {"awaiting_sidecar", "running", "failed", "deferred"} <= set(NON_COMPLETED_STATUSES)

    @pytest.mark.parametrize("status", NON_COMPLETED_STATUSES)
    def test_every_other_status_keeps_its_worktree(self, world: World, status: str) -> None:
        world.add_task("t-status", status=status)  # merged + clean: status alone fails
        result = only(run(world), "t-status")
        assert (result.outcome, result.reason) == (Outcome.KEPT, KeepReason.STATUS)
        assert result.detail == f"status={status}"
        assert_untouched(world, "t-status")

    def test_missing_state_file_keeps_it(self, world: World) -> None:
        world.add_task("t-nostate", status=None)
        result = only(run(world), "t-nostate")
        assert (result.reason, result.detail) == (KeepReason.STATUS, "no state file")
        assert_untouched(world, "t-nostate")

    def test_unreadable_state_file_keeps_it(self, world: World) -> None:
        world.add_task("t-corrupt")
        state_path_for(world.queue, "t-corrupt").write_text("status: [completed\n")
        result = only(run(world), "t-corrupt")
        assert result.reason is KeepReason.STATUS
        assert result.detail.startswith("state unreadable: ")
        assert_untouched(world, "t-corrupt")

    def test_pushed_but_unmerged_branch_is_kept(self, world: World) -> None:
        world.add_task("t-open", merge=False)
        result = only(run(world), "t-open")
        assert (result.outcome, result.reason) == (Outcome.KEPT, KeepReason.UNMERGED)
        assert result.detail == "claude/t-open is not an ancestor of origin/main"
        assert_untouched(world, "t-open")

    def test_commit_made_after_the_merge_is_kept(self, world: World) -> None:
        wt = world.add_task("t-followup")
        world.commit(wt, "models/followup.R")
        result = only(run(world), "t-followup")
        assert result.reason is KeepReason.UNMERGED
        assert_untouched(world, "t-followup")

    def test_merge_into_local_main_only_is_kept(self, world: World) -> None:
        world.add_task("t-local", merge=False)
        git(world.repo, "merge", "-q", "--no-ff", "-m", "local only", "claude/t-local")
        assert world.is_ancestor("claude/t-local", "main")
        result = only(run(world), "t-local")
        assert result.reason is KeepReason.UNMERGED
        assert_untouched(world, "t-local")

    def test_modified_tracked_file_is_kept(self, world: World) -> None:
        wt = world.add_task("t-edit")
        (wt / "models" / "t-edit.R").write_text("# edited, not committed\n")
        result = only(run(world), "t-edit")
        assert (result.reason, result.detail) == (
            KeepReason.DIRTY,
            "uncommitted:  M models/t-edit.R",
        )
        assert_untouched(world, "t-edit")

    def test_staged_change_is_kept(self, world: World) -> None:
        wt = world.add_task("t-staged")
        (wt / "models" / "Staged_2025.R").write_text("staged\n")
        git(wt, "add", "models/Staged_2025.R")
        result = only(run(world), "t-staged")
        assert (result.reason, result.detail) == (
            KeepReason.DIRTY,
            "uncommitted: A  models/Staged_2025.R",
        )
        assert_untouched(world, "t-staged")

    def test_many_dirty_entries_are_summarised(self, world: World) -> None:
        wt = world.add_task("t-busy")
        for name in ("a", "b", "c", "d", "e"):
            (wt / f"{name}.R").write_text("x\n")
        result = only(run(world), "t-busy")
        assert result.detail == "uncommitted: ?? a.R; ?? b.R; ?? c.R (+2 more)"

    def test_gitignored_deliverable_inside_the_worktree_is_kept(self, world: World) -> None:
        """Ignored files are normally disposable -- but not the task's output."""
        wt = world.add_task("t-deliv", deliverable_paths=[Path("out/summary.o")])
        (wt / "out").mkdir()
        (wt / "out" / "summary.o").write_text("the task's product\n")
        result = only(run(world), "t-deliv")
        assert (result.reason, result.detail) == (
            KeepReason.DIRTY,
            "declared deliverable out/summary.o is gitignored; removal would delete it",
        )
        assert_untouched(world, "t-deliv")

    def test_committed_deliverable_inside_the_worktree_is_fine(self, world: World) -> None:
        wt = world.add_task(
            "t-committed",
            extra_files=("out/summary.md",),
            deliverable_paths=[Path("out/summary.md")],
        )
        result = only(run(world), "t-committed")
        assert result.outcome is Outcome.RECLAIMED
        assert not wt.exists()

    def test_in_flight_task_is_kept(self, world: World) -> None:
        """The dispatcher writes completed before its post-dispatch hook runs."""
        world.add_task("t-hook")
        result = only(run(world, in_flight_task_ids={"t-hook"}), "t-hook")
        assert (result.reason, result.detail) == (
            KeepReason.IN_FLIGHT,
            "a dispatch thread still holds the task",
        )
        assert_untouched(world, "t-hook")

    def test_worktree_on_another_branch_is_kept(self, world: World) -> None:
        wt = world.add_task("t-switched")
        git(wt, "switch", "-q", "-c", "scratch")
        result = only(run(world), "t-switched")
        assert (result.reason, result.detail) == (
            KeepReason.BRANCH_MISMATCH,
            "branch scratch is checked out; expected claude/t-switched",
        )
        assert result.branch == "scratch"
        assert (wt / ".git").is_file()

    def test_detached_head_is_kept(self, world: World) -> None:
        wt = world.add_task("t-detached")
        git(wt, "switch", "-q", "--detach")
        result = only(run(world), "t-detached")
        assert (result.reason, result.detail) == (
            KeepReason.BRANCH_MISMATCH,
            "a detached HEAD is checked out; expected claude/t-detached",
        )
        assert (wt / ".git").is_file()

    def test_locked_worktree_is_kept(self, world: World) -> None:
        wt = world.add_task("t-locked")
        git(world.repo, "worktree", "lock", "--reason", "operator inspecting", str(wt))
        result = only(run(world), "t-locked")
        assert (result.reason, result.detail) == (
            KeepReason.LOCKED,
            "git worktree lock: operator inspecting",
        )
        assert_untouched(world, "t-locked")

    def test_main_worktree_is_never_removed(self, world: World) -> None:
        world.write_task("t-root", working_dir=world.repo)
        world.write_state("t-root", "completed")
        result = only(run(world), "t-root")
        assert result.reason is KeepReason.NOT_LINKED_WORKTREE
        assert (world.repo / ".git").is_dir()

    def test_gitfile_checkout_that_is_its_repos_main_worktree_is_kept(self, world: World) -> None:
        """``.git`` is a file here too, but it is no linked worktree."""
        solo = world.root / "solo"
        git(world.root, "init", "-q", "--separate-git-dir", str(world.root / "solo.git"), str(solo))
        assert (solo / ".git").is_file()
        world.write_task("t-solo", working_dir=solo)
        world.write_state("t-solo", "completed")
        result = only(run(world), "t-solo")
        assert result.reason is KeepReason.NOT_LINKED_WORKTREE
        assert result.detail == f"not a linked worktree of {real(world.root / 'solo.git')}"
        assert (solo / ".git").is_file()

    def test_working_dir_shared_by_two_tasks_is_kept(self, world: World) -> None:
        wt = world.add_task("t-a")
        world.write_task("t-b", working_dir=wt)
        world.write_state("t-b", "completed")
        report = run(world)
        for task_id in ("t-a", "t-b"):
            result = only(report, task_id)
            assert (result.reason, result.detail) == (
                KeepReason.SHARED_WORKING_DIR,
                "working_dir is named by 2 tasks: t-a, t-b",
            )
        assert_untouched(world, "t-a")


# ---------------------------------------------------------------------------
# failures
# ---------------------------------------------------------------------------


def _fail_git(
    monkeypatch: pytest.MonkeyPatch, prefix: list[str], stderr: str = "fatal: simulated"
) -> None:
    real_git = reclaim_mod._git

    def fake(args: list[str], *, cwd: Path, timeout_s: float) -> reclaim_mod._GitResult:
        if args[: len(prefix)] == prefix:
            return reclaim_mod._GitResult(128, "", stderr)
        return real_git(args, cwd=cwd, timeout_s=timeout_s)

    monkeypatch.setattr(reclaim_mod, "_git", fake)


class TestFailures:
    def test_fetch_failure_keeps_everything_and_is_an_error(self, world: World) -> None:
        world.add_task("t-offline")
        git(world.repo, "remote", "set-url", "origin", str(world.root / "missing.git"))
        report = run(world)
        result = only(report, "t-offline")
        assert (result.outcome, result.reason) == (Outcome.KEPT, KeepReason.FETCH_FAILED)
        assert len(report.errors) == 1
        assert report.errors[0].startswith(f"git fetch origin main in {real(world.repo)}: ")
        assert result.detail == report.errors[0]
        assert report.ok is False
        assert_untouched(world, "t-offline")

    def test_failed_removal_is_reported_and_keeps_the_branch(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world.add_task("t-stuck")
        _fail_git(monkeypatch, ["worktree", "remove"], "fatal: simulated removal failure")
        report = run(world)
        result = only(report, "t-stuck")
        assert result.outcome is Outcome.FAILED
        assert result.detail == "git worktree remove: fatal: simulated removal failure"
        assert result.branch_deleted is None
        assert report.ok is False
        assert_untouched(world, "t-stuck")

    def test_status_probe_failure_is_a_git_error(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world.add_task("t-probe")
        _fail_git(monkeypatch, ["--no-optional-locks", "status"])
        result = only(run(world), "t-probe")
        assert (result.reason, result.detail) == (
            KeepReason.GIT_ERROR,
            "git status: fatal: simulated",
        )
        assert result.is_error is True
        assert_untouched(world, "t-probe")

    def test_merge_base_error_is_a_git_error(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world.add_task("t-mb")
        _fail_git(monkeypatch, ["merge-base"])
        result = only(run(world), "t-mb")
        assert (result.reason, result.detail) == (
            KeepReason.GIT_ERROR,
            "git merge-base: fatal: simulated",
        )

    def test_git_branch_d_refusal_keeps_the_branch(self, world: World) -> None:
        """``git branch -d`` checks the branch's upstream, not origin/main. An
        upstream that lacks the commits makes git refuse -- and the refusal is
        honoured, never overridden with ``-D``."""
        wt = world.add_task("t-upstream")
        seed = git(world.repo, "rev-list", "--max-parents=0", "HEAD")
        git(world.repo, "push", "-q", "origin", f"{seed}:refs/heads/stale")
        git(world.repo, "branch", "-q", "--set-upstream-to=origin/stale", "claude/t-upstream")

        report = run(world)

        result = only(report, "t-upstream")
        assert result.outcome is Outcome.RECLAIMED
        assert result.branch_deleted is False
        assert result.detail.startswith("git branch -d kept the branch: ")
        assert "not fully merged" in result.detail
        assert not wt.exists()
        assert world.branch_exists("claude/t-upstream")
        assert report.ok is True
        assert report.counts()["branch_kept"] == 1
        assert report.summary().endswith("; 1 branch(es) kept by git branch -d")

    def test_missing_git_binary_is_reported_not_raised(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world.add_task("t-nogit")
        monkeypatch.setenv("PATH", str(world.root / "empty-bin"))
        result = only(run(world), "t-nogit")
        assert result.reason is KeepReason.GIT_ERROR
        assert result.detail.startswith("git rev-parse: cannot run git: ")

    def test_git_timeout_is_reported_not_raised(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world.add_task("t-slow")

        def timeout(*args: Any, **kwargs: Any) -> None:
            raise reclaim_mod.subprocess.TimeoutExpired(cmd="git", timeout=0.5)

        monkeypatch.setattr(reclaim_mod.subprocess, "run", timeout)
        settings = WorktreeReclaimSettings(git_timeout_s=0.5)
        result = only(run(world, settings=settings), "t-slow")
        assert result.reason is KeepReason.GIT_ERROR
        assert result.detail == (
            "git rev-parse: git rev-parse --path-format=absolute --git-common-dir "
            "timed out after 0.5s"
        )


# ---------------------------------------------------------------------------
# limit and locking
# ---------------------------------------------------------------------------


class TestLimit:
    def test_limit_caps_removals_and_defers_the_rest(self, world: World) -> None:
        for n in range(3):
            world.add_task(f"t-{n}")
        report = run(world, limit=2)
        assert [(r.task_id, r.outcome, r.reason) for r in report.results] == [
            ("t-0", Outcome.RECLAIMED, None),
            ("t-1", Outcome.RECLAIMED, None),
            ("t-2", Outcome.KEPT, KeepReason.LIMIT),
        ]
        assert only(report, "t-2").detail == "this pass already reached its limit of 2"
        assert report.ok is True
        assert_untouched(world, "t-2")

    def test_unmerged_worktrees_do_not_count_against_the_limit(self, world: World) -> None:
        world.add_task("t-0", merge=False)
        world.add_task("t-1")
        report = run(world, limit=1)
        assert [(r.task_id, r.outcome) for r in report.results] == [
            ("t-0", Outcome.KEPT),
            ("t-1", Outcome.RECLAIMED),
        ]

    def test_limit_must_be_positive(self, world: World) -> None:
        with pytest.raises(ReclaimError, match="limit must be at least 1, got 0"):
            run(world, limit=0)


@contextlib.contextmanager
def hold_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def _intercept_removal_lock(monkeypatch: pytest.MonkeyPatch, action: Any) -> None:
    """Run ``action`` inside the lock taken for the removal (the second one:
    the first guards the fetch)."""
    real_lock = reclaim_mod._hook_lock
    calls = {"n": 0}

    @contextlib.contextmanager
    def intercepting(path: Path | None, timeout_s: float) -> Iterator[None]:
        calls["n"] += 1
        with real_lock(path, timeout_s):
            if calls["n"] == 2:
                action()
            yield

    monkeypatch.setattr(reclaim_mod, "_hook_lock", intercepting)


class TestLocking:
    def test_busy_hook_lock_keeps_the_worktree(self, world: World) -> None:
        world.add_task("t-busy")
        settings = WorktreeReclaimSettings(lock_file=LOCK_REL, lock_timeout_s=0.3)
        with hold_lock(world.queue / LOCK_REL):
            report = run(world, settings=settings)
        result = only(report, "t-busy")
        assert (result.outcome, result.reason) == (Outcome.KEPT, KeepReason.LOCK_BUSY)
        assert result.detail == f"{world.queue / LOCK_REL} still held after 0.3s"
        assert report.ok is True, "a busy lock is retried next pass, not an error"
        assert_untouched(world, "t-busy")

    def test_removal_waits_for_the_hook_lock(self, world: World) -> None:
        wt = world.add_task("t-wait")
        lock = world.root / "locks" / "hook.lock"  # absolute lock_file
        settings = WorktreeReclaimSettings(lock_file=str(lock), lock_timeout_s=30)
        held = threading.Event()
        seen: dict[str, bool] = {}

        def hook() -> None:
            with hold_lock(lock):
                held.set()
                time.sleep(0.5)
                seen["worktree_while_held"] = wt.exists()

        thread = threading.Thread(target=hook)
        thread.start()
        assert held.wait(5)
        report = run(world, settings=settings)
        thread.join(5)

        assert seen == {"worktree_while_held": True}
        assert only(report, "t-wait").outcome is Outcome.RECLAIMED
        assert not wt.exists()

    def test_status_is_rechecked_before_removal(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world.add_task("t-flip")
        _intercept_removal_lock(monkeypatch, lambda: world.write_state("t-flip", "pending"))
        result = only(run(world), "t-flip")
        assert (result.reason, result.detail) == (
            KeepReason.STATUS,
            "status changed to pending before removal",
        )
        assert_untouched(world, "t-flip")

    def test_work_that_appears_before_removal_is_not_forced_away(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The first check saw only discardable paths (so --force was due);
        the re-check under the lock must see the new file and back off."""
        wt = world.add_task("t-late")
        _write_problems(wt)
        _intercept_removal_lock(monkeypatch, lambda: (wt / "late.R").write_text("new work\n"))
        result = only(run(world), "t-late")
        assert (result.reason, result.detail) == (KeepReason.DIRTY, "uncommitted: ?? late.R")
        assert (wt / "late.R").exists()
        assert_untouched(world, "t-late")

    def test_worktree_removed_concurrently_is_reported_gone(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wt = world.add_task("t-gone")
        _intercept_removal_lock(monkeypatch, lambda: git(world.repo, "worktree", "remove", str(wt)))
        result = only(run(world), "t-gone")
        assert (result.reason, result.detail) == (
            KeepReason.GONE,
            "the worktree disappeared before removal",
        )
        assert result.is_error is False


# ---------------------------------------------------------------------------
# queue discovery
# ---------------------------------------------------------------------------


class TestQueueDiscovery:
    def test_directory_without_todo_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ReclaimError, match="has no todo/ subdirectory"):
            reclaim_worktrees(tmp_path, WorktreeReclaimSettings(), apply=False)

    def test_tasks_without_a_checkout_are_not_listed(self, world: World) -> None:
        world.write_task("t-none", working_dir=None)
        world.write_task("t-never", working_dir=world.worktree("t-never"))
        world.write_task("t-relative", working_dir=Path("relative/dir"))
        report = run(world)
        assert report.results == ()
        assert report.tasks_scanned == 3
        assert report.summary() == (
            "applied: 0 worktree(s) seen; 0 reclaimed; kept 0 (status), 0 (unmerged), "
            "0 (uncommitted work), 0 (other); 0 failed"
        )

    def test_unparseable_task_yaml_is_reported(self, world: World) -> None:
        (todo_dir(world.queue) / "broken.yaml").write_text("id: [\n")
        world.add_task("t-ok")
        report = run(world)
        assert report.unparseable_tasks == ("broken",)
        assert report.tasks_scanned == 2
        assert only(report, "t-ok").outcome is Outcome.RECLAIMED
        assert report.ok is True

    def test_one_fetch_per_repository(self, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
        for n in range(3):
            world.add_task(f"t-{n}")
        real_git = reclaim_mod._git
        fetches: list[list[str]] = []

        def counting(args: list[str], *, cwd: Path, timeout_s: float) -> reclaim_mod._GitResult:
            if args[0] == "fetch":
                fetches.append(args)
            return real_git(args, cwd=cwd, timeout_s=timeout_s)

        monkeypatch.setattr(reclaim_mod, "_git", counting)
        report = run(world)
        assert fetches == [
            ["fetch", "--quiet", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main"]
        ]
        assert report.counts()["reclaimed"] == 3

    def test_no_candidates_means_no_fetch(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world.add_task("t-running", status="running")
        _fail_git(monkeypatch, ["fetch"])
        report = run(world)
        assert report.errors == ()
        assert only(report, "t-running").reason is KeepReason.STATUS

    def test_worktree_name_placeholder(self, world: World) -> None:
        """A queue whose worktree directory, not task id, names the branch."""
        wt = world.root / "elsewhere" / "wt-7"
        git(world.repo, "worktree", "add", "-q", "-b", "task/wt-7", str(wt), "origin/main")
        world.write_task("t-7", working_dir=wt)
        world.write_state("t-7", "completed")
        settings = WorktreeReclaimSettings(branch_template="task/{worktree_name}")
        result = only(run(world, settings=settings), "t-7")
        assert result.outcome is Outcome.RECLAIMED
        assert result.branch == "task/wt-7"
        assert not wt.exists()
        assert not world.branch_exists("task/wt-7")


class TestReportShape:
    def test_to_json(self, world: World) -> None:
        wt = world.add_task("t-json")
        _write_problems(wt)
        world.add_task("t-open", merge=False)
        payload = run(world, apply=False).to_json()
        assert payload["ok"] is True
        assert payload["applied"] is False
        assert payload["remote"] == "origin"
        assert payload["parent_branch"] == "main"
        assert payload["tasks_scanned"] == 2
        assert payload["counts"] == {
            "seen": 2,
            "reclaimed": 0,
            "would_reclaim": 1,
            "kept": 1,
            "failed": 0,
            "branch_kept": 0,
            "kept_by_reason": {"unmerged": 1},
        }
        assert payload["results"] == [
            {
                "task_id": "t-json",
                "working_dir": str(wt),
                "branch": "claude/t-json",
                "outcome": "would_reclaim",
                "reason": None,
                "detail": "",
                "forced": True,
                "discarded": ["tests/testthat/_problems/"],
                "branch_deleted": None,
            },
            {
                "task_id": "t-open",
                "working_dir": str(world.worktree("t-open")),
                "branch": "claude/t-open",
                "outcome": "kept",
                "reason": "unmerged",
                "detail": "claude/t-open is not an ancestor of origin/main",
                "forced": False,
                "discarded": [],
                "branch_deleted": None,
            },
        ]

    def test_summary_counts_every_reason_class(self) -> None:
        def result(outcome: Outcome, reason: KeepReason | None = None) -> ReclaimResult:
            return ReclaimResult(task_id="t", working_dir="/w", outcome=outcome, reason=reason)

        report = ReclaimReport(
            applied=True,
            remote="origin",
            parent_branch="main",
            tasks_scanned=9,
            results=(
                result(Outcome.RECLAIMED),
                result(Outcome.KEPT, KeepReason.STATUS),
                result(Outcome.KEPT, KeepReason.STATUS),
                result(Outcome.KEPT, KeepReason.UNMERGED),
                result(Outcome.KEPT, KeepReason.DIRTY),
                result(Outcome.KEPT, KeepReason.LOCKED),
                result(Outcome.KEPT, KeepReason.LIMIT),
                result(Outcome.FAILED),
            ),
        )
        assert report.summary() == (
            "applied: 8 worktree(s) seen; 1 reclaimed; kept 2 (status), 1 (unmerged), "
            "1 (uncommitted work), 2 (other); 1 failed"
        )
        assert report.ok is False


class TestEdgeCases:
    def test_bare_repository_with_linked_worktrees(self, world: World) -> None:
        """Repo-level commands run in the bare dir when there is no main checkout."""
        bare = world.root / "bare.git"
        git(world.root, "clone", "-q", "--bare", str(world.origin), str(bare))
        # A bare clone maps remote heads onto local heads; give it the usual
        # remote-tracking layout so origin/main exists, as in a hook setup.
        git(bare, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        git(bare, "fetch", "-q", "origin")
        wt = world.root / "bare-worktrees" / "t-bare"
        git(bare, "worktree", "add", "-q", "-b", "claude/t-bare", str(wt), "origin/main")
        world.commit(wt, "models/t-bare.R")
        git(wt, "push", "-q", "origin", "claude/t-bare")
        world.consolidate("t-bare")
        world.write_task("t-bare", working_dir=wt)
        world.write_state("t-bare", "completed")

        result = only(run(world), "t-bare")

        assert result.outcome is Outcome.RECLAIMED
        assert result.branch_deleted is True
        assert not wt.exists()
        assert git(bare, "branch", "--list", "claude/t-bare") == ""

    def test_real_rename_is_dirty(self, world: World) -> None:
        wt = world.add_task("t-mv")
        git(wt, "mv", "models/t-mv.R", "models/renamed.R")
        result = only(run(world), "t-mv")
        assert (result.reason, result.detail) == (
            KeepReason.DIRTY,
            "uncommitted: R  models/renamed.R",
        )

    def test_status_parser_skips_rename_sources_and_never_reads_junk_as_clean(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        porcelain = "R  new.R\0old.R\0?? tests/testthat/_problems/\0X\0"
        monkeypatch.setattr(
            reclaim_mod, "_git", lambda args, **kw: reclaim_mod._GitResult(0, porcelain, "")
        )
        blocking, discardable = reclaim_mod._worktree_dirt(Path("/wt"), WorktreeReclaimSettings())
        assert blocking == ["R  new.R", "X"]
        assert discardable == ["tests/testthat/_problems/"]

    def test_worktree_list_failure_is_a_git_error(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world.add_task("t-list")
        _fail_git(monkeypatch, ["worktree", "list"])
        result = only(run(world), "t-list")
        assert (result.reason, result.detail) == (
            KeepReason.GIT_ERROR,
            "git worktree list: fatal: simulated",
        )
        assert_untouched(world, "t-list")

    def test_absent_deliverable_inside_the_worktree_does_not_block(self, world: World) -> None:
        wt = world.add_task("t-absent", deliverable_paths=[Path("out/never-written.md")])
        result = only(run(world), "t-absent")
        assert result.outcome is Outcome.RECLAIMED
        assert not wt.exists()

    def test_check_ignore_failure_is_a_git_error(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wt = world.add_task("t-ci", deliverable_paths=[Path("out/summary.o")])
        (wt / "out").mkdir()
        (wt / "out" / "summary.o").write_text("product\n")
        _fail_git(monkeypatch, ["check-ignore"])
        result = only(run(world), "t-ci")
        assert (result.reason, result.detail) == (
            KeepReason.GIT_ERROR,
            "git check-ignore out/summary.o: fatal: simulated",
        )
        assert_untouched(world, "t-ci")

    def test_lock_busy_at_removal_time_keeps_it(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world.add_task("t-late-lock")
        real_lock = reclaim_mod._hook_lock
        calls = {"n": 0}

        @contextlib.contextmanager
        def second_is_busy(path: Path | None, timeout_s: float) -> Iterator[None]:
            calls["n"] += 1
            if calls["n"] == 2:
                raise reclaim_mod._LockBusy("hook lock held")
            with real_lock(path, timeout_s):
                yield

        monkeypatch.setattr(reclaim_mod, "_hook_lock", second_is_busy)
        result = only(run(world), "t-late-lock")
        assert (result.reason, result.detail) == (KeepReason.LOCK_BUSY, "hook lock held")
        assert_untouched(world, "t-late-lock")


def test_unopenable_lock_file_is_a_could_not_run_error(world: World) -> None:
    world.add_task("t-lock")
    blocker = world.queue / "not-a-dir"
    blocker.write_text("a regular file where the lock's directory should be\n")
    settings = WorktreeReclaimSettings(lock_file="not-a-dir/setup_worktree.lock")
    with pytest.raises(
        ReclaimError, match=r"cannot open lock_file .*not-a-dir/setup_worktree\.lock"
    ):
        run(world, settings=settings)
    assert_untouched(world, "t-lock")
