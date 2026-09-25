"""A throwaway git "world" in the shape the worktree reclaim (ADR-0034) meets.

Mirrors the nlmixr2lib ingestion queue in production:

* ``origin.git`` -- a bare remote whose ``main`` is the parent branch;
* ``repo`` -- the clone the pre-dispatch hook works in. Every task gets
  ``repo/.claude/worktrees/<id>`` on branch ``claude/<id>``, forked from
  ``origin/main`` exactly as ``setup_worktree.sh`` does it (which also makes
  ``origin/main`` the branch's upstream);
* ``consolidator`` -- a second clone standing in for the
  runner-merge-claude-branches consolidation: it folds task branches into
  ``main`` with a real merge commit and pushes, so ``repo``'s own
  ``origin/main`` stays stale until the reclaim fetches;
* ``queue`` -- ``todo/<id>.yaml`` naming the worktree as ``working_dir`` and
  ``.claude_task_runner/state/<id>.yaml`` carrying the status.

Not collected by pytest (no ``test_`` prefix); test modules build it through
their own ``world`` fixture with :meth:`World.create`.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from claude_task_runner.queue.schema import Task, TaskState
from claude_task_runner.queue.store import (
    queue_runtime_dir,
    state_path_for,
    task_path_for,
    todo_dir,
    write_state_atomic,
    write_task_atomic,
)

_GITCONFIG = """\
[user]
\tname = Runner Test
\temail = runner-test@example.com
[init]
\tdefaultBranch = main
[commit]
\tgpgsign = false
[advice]
\tdetachedHead = false
"""

_LEAKY_GIT_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
)


def git(cwd: Path, *args: str) -> str:
    """Run git for fixture setup; any failure fails the test loudly."""
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"git {' '.join(args)} failed in {cwd}: {proc.stderr}"
    return proc.stdout.strip()


def real(path: Path) -> str:
    return os.path.realpath(path)


@dataclass
class World:
    root: Path
    origin: Path
    repo: Path
    consolidator: Path
    queue: Path

    @classmethod
    def create(cls, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
        # Keep the developer's own git config (signing, hooks, default
        # branch) and any GIT_DIR inherited from a git hook out of both the
        # fixture and the code under test, which runs git in this process's
        # environment.
        gitconfig = tmp_path / "gitconfig"
        gitconfig.write_text(_GITCONFIG)
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        for var in _LEAKY_GIT_ENV:
            monkeypatch.delenv(var, raising=False)

        origin = tmp_path / "origin.git"
        git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
        repo = tmp_path / "repo"
        git(tmp_path, "clone", "-q", str(origin), str(repo))
        # tests/testthat/ is tracked, as in an R package, so an untracked
        # _problems/ directory shows up as its own `git status` entry.
        (repo / "tests" / "testthat").mkdir(parents=True)
        (repo / "tests" / "testthat" / "test-seed.R").write_text("# seed\n")
        (repo / ".gitignore").write_text("*.o\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "seed")
        git(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
        git(repo, "fetch", "-q", "origin")
        consolidator = tmp_path / "consolidator"
        git(tmp_path, "clone", "-q", str(origin), str(consolidator))

        queue = tmp_path / "queue"
        queue.mkdir()
        todo_dir(queue)
        queue_runtime_dir(queue)
        return cls(tmp_path, origin, repo, consolidator, queue)

    # -- git state -----------------------------------------------------------

    def worktree(self, task_id: str) -> Path:
        return self.repo / ".claude" / "worktrees" / task_id

    def add_task(
        self,
        task_id: str,
        *,
        status: str | None = "completed",
        commit: bool = True,
        merge: bool = True,
        extra_files: tuple[str, ...] = (),
        **task_fields: Any,
    ) -> Path:
        """Create a task's worktree the way the hook does, then play out its run.

        ``commit`` makes the worker commit a model and push its branch;
        ``merge`` then consolidates that branch into origin/main.
        """
        wt = self.worktree(task_id)
        git(self.repo, "worktree", "add", "-q", "-b", f"claude/{task_id}", str(wt), "origin/main")
        if commit:
            for rel in (f"models/{task_id}.R", *extra_files):
                self.commit(wt, rel)
            git(wt, "push", "-q", "origin", f"claude/{task_id}")
        if merge:
            assert commit, "only a pushed branch can be consolidated"
            self.consolidate(task_id)
        self.write_task(task_id, working_dir=wt, **task_fields)
        if status is not None:
            self.write_state(task_id, status)
        return wt

    def commit(self, wt: Path, rel: str) -> None:
        path = wt / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {rel}\n")
        git(wt, "add", rel)
        git(wt, "commit", "-qm", f"Add {rel}")

    def consolidate(self, task_id: str) -> None:
        """Fold ``claude/<id>`` into origin/main with a real merge commit."""
        git(self.consolidator, "fetch", "-q", "origin")
        git(self.consolidator, "reset", "-q", "--hard", "origin/main")
        git(
            self.consolidator,
            "merge",
            "-q",
            "--no-ff",
            "-m",
            f"Merge claude/{task_id}",
            f"origin/claude/{task_id}",
        )
        git(self.consolidator, "push", "-q", "origin", "HEAD:refs/heads/main")

    def branch_exists(self, branch: str) -> bool:
        proc = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=str(self.repo),
            check=False,
        )
        return proc.returncode == 0

    def is_ancestor(self, branch: str, of: str) -> bool:
        proc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", branch, of], cwd=str(self.repo), check=False
        )
        return proc.returncode == 0

    def registered_worktrees(self) -> set[str]:
        listing = git(self.repo, "worktree", "list", "--porcelain")
        return {
            real(Path(line.split(" ", 1)[1]))
            for line in listing.splitlines()
            if line.startswith("worktree ")
        }

    # -- queue state ---------------------------------------------------------

    def write_task(self, task_id: str, *, working_dir: Path | None, **fields: Any) -> None:
        task = Task(id=task_id, title=task_id, prompt="extract", working_dir=working_dir, **fields)
        write_task_atomic(task, task_path_for(self.queue, task_id))

    def write_state(self, task_id: str, status: str) -> None:
        state = TaskState.model_validate({"task_id": task_id, "status": status})
        write_state_atomic(state, state_path_for(self.queue, task_id))
