"""A clean-exit run that commits but never pushes must not be 'completed'.

Regression cover for the failure mode that silently stranded 51 complete
extractions on the nlmixr2lib queue (observed 2026-08-20): the worker
committed its model files -- satisfying ADR-0020's ``has_commit`` gate --
then ended its turn waiting on a slow gate ("I'll push once check()
returns"). The run was marked ``completed``, ``completed`` is not in the
orchestrator's dispatchable set, and the task was never re-selected. The
only copy of three weeks of work was a set of local branch tips.

19 of those 51 got there via an earlier attempt that failed
``uncommitted_work_left`` -- so the uncommitted-work guard, by pushing
workers to commit before their gates finished, converted a loud failure
into a silent one. This gate closes that step.
"""

import subprocess
from pathlib import Path

import pytest

from claude_task_runner.runner.dispatcher import OutputEvidence, _unpushed_commits


class TestOutputEvidence:
    def test_pushed_work_is_not_flagged(self) -> None:
        ev = OutputEvidence(has_commit=True, has_sidecar=False, has_deliverable=True, unpushed=())
        assert ev.any is True
        assert ev.left_commits_unpushed is False

    def test_unpushed_commit_is_flagged(self) -> None:
        """The bug: committed, evidence gate passes, nothing on the remote."""
        ev = OutputEvidence(
            has_commit=True,
            has_sidecar=False,
            has_deliverable=True,
            unpushed=("a1b2c3d feat(model): add Yau_2023_diazepam_human_pbpk",),
        )
        assert ev.any is True, "evidence gate alone still passes -- that is the bug"
        assert ev.left_commits_unpushed is True, "the new gate must catch it"

    def test_clean_skip_is_not_flagged(self) -> None:
        """A genuine skip: deliverable written, no commit, nothing to push."""
        ev = OutputEvidence(has_commit=False, has_sidecar=False, has_deliverable=True)
        assert ev.any is True
        assert ev.left_commits_unpushed is False

    def test_default_unpushed_is_empty(self) -> None:
        """Existing constructions without the new field keep old behaviour."""
        ev = OutputEvidence(has_commit=True, has_sidecar=False, has_deliverable=False)
        assert ev.unpushed == ()
        assert ev.left_commits_unpushed is False

    def test_uncommitted_and_unpushed_are_independent(self) -> None:
        """The two gates describe different stages and must not alias."""
        ev = OutputEvidence(
            has_commit=True,
            has_sidecar=False,
            has_deliverable=True,
            uncommitted=("inst/modeldb/specificDrugs/Half_done.R",),
            unpushed=("a1b2c3d wip",),
        )
        assert ev.left_work_uncommitted is True
        assert ev.left_commits_unpushed is True

    def test_sidecar_exit_with_unpushed_commit_is_still_flagged(self) -> None:
        """Stopping to ask a question does not make unpushed work safe.

        The dispatcher flips such a run to ``awaiting_sidecar`` separately;
        this asserts only that the evidence itself reports the risk.
        """
        ev = OutputEvidence(
            has_commit=True,
            has_sidecar=True,
            has_deliverable=False,
            unpushed=("a1b2c3d checkpoint",),
        )
        assert ev.left_commits_unpushed is True


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.fixture()
def origin_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """A bare 'origin' plus a working clone with one pushed commit."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)

    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "T")
    (work / "seed.txt").write_text("seed\n")
    _git(work, "add", "seed.txt")
    _git(work, "commit", "-qm", "seed")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    return origin, work


class TestUnpushedCommitsHelper:
    def test_fresh_clone_has_nothing_unpushed(self, origin_and_clone) -> None:
        _origin, work = origin_and_clone
        assert _unpushed_commits(work) == []

    def test_local_commit_is_reported(self, origin_and_clone) -> None:
        _origin, work = origin_and_clone
        (work / "model.R").write_text("f <- function() {}\n")
        _git(work, "add", "model.R")
        _git(work, "commit", "-qm", "feat(model): add model.R")
        found = _unpushed_commits(work)
        assert len(found) == 1
        assert "add model.R" in found[0]

    def test_pushing_clears_it_without_a_fetch(self, origin_and_clone) -> None:
        """git push updates the remote-tracking ref itself."""
        _origin, work = origin_and_clone
        (work / "model.R").write_text("f <- function() {}\n")
        _git(work, "add", "model.R")
        _git(work, "commit", "-qm", "feat(model): add model.R")
        assert _unpushed_commits(work) != []
        _git(work, "push", "-q", "origin", "HEAD:refs/heads/task-branch")
        assert _unpushed_commits(work) == []

    def test_multiple_commits_all_reported(self, origin_and_clone) -> None:
        _origin, work = origin_and_clone
        for i in range(3):
            (work / f"f{i}.R").write_text("x\n")
            _git(work, "add", f"f{i}.R")
            _git(work, "commit", "-qm", f"commit {i}")
        assert len(_unpushed_commits(work)) == 3

    def test_repo_with_no_remotes_is_not_flagged(self, tmp_path: Path) -> None:
        """A remote-less checkout has nothing to push TO -- stay silent.

        Without this guard every commit in a local-only queue would read as
        unpushed and the gate would fail every single run.
        """
        solo = tmp_path / "solo"
        solo.mkdir()
        subprocess.run(["git", "init", "-q", str(solo)], check=True)
        _git(solo, "config", "user.email", "t@example.com")
        _git(solo, "config", "user.name", "T")
        (solo / "a.txt").write_text("a\n")
        _git(solo, "add", "a.txt")
        _git(solo, "commit", "-qm", "only commit")
        assert _unpushed_commits(solo) == []

    def test_commit_reachable_from_another_remote_branch_is_not_flagged(
        self, origin_and_clone
    ) -> None:
        """Merged-and-branch-deleted work is on origin/main; do not flag it."""
        _origin, work = origin_and_clone
        (work / "model.R").write_text("f <- function() {}\n")
        _git(work, "add", "model.R")
        _git(work, "commit", "-qm", "feat(model): add model.R")
        _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")
        assert _unpushed_commits(work) == []

    def test_nonexistent_dir_does_not_raise(self, tmp_path: Path) -> None:
        assert _unpushed_commits(tmp_path / "nope") == []
