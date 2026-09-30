"""Git repositories in the shape the runner-merge-claude-branches helpers meet.

A bare ``origin.git`` and a clone ``repo``. Task branches are cut from ``main``
and pushed the way a runner worker pushes them, so the clone sees each as
``origin/claude/<name>``; :func:`consolidate` then folds some of them into a
worktree at ``repo/.worktrees/<name>`` the way merge_branches.sh does, one
``git merge --no-ff -X theirs`` per branch, which is where every helper looks.

Not collected by pytest (no ``test_`` prefix).
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import pytest

from claude_task_runner.cli.install_skills_cmd import _packaged_skill_dir

from ._git_world import git, isolate_git

SKILL_DIR = _packaged_skill_dir("runner-merge-claude-branches")


def run(
    *cmd: str | Path, cwd: Path | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a script with stdin closed, so a prompt can never wait on a terminal."""
    return subprocess.run(
        [str(c) for c in cmd],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def run_helper(script: str, *args: str | Path) -> subprocess.CompletedProcess[str]:
    """Run one of the skill's Python helpers the way merge_branches.sh does."""
    return run(sys.executable, SKILL_DIR / script, *args)


def load_helper(name: str) -> ModuleType:
    """Import a helper script as a module.

    The skill directory is on ``sys.path`` while it imports, as it is when the
    script runs, so a helper that imports a sibling module finds it.
    """
    path = str(SKILL_DIR)
    sys.path.insert(0, path)
    try:
        return importlib.import_module(name)
    finally:
        sys.path.remove(path)


def commit(repo: Path, files: dict[str, str], message: str) -> None:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, "add", *files)
    git(repo, "commit", "-qm", message)


def push_task_branch(
    repo: Path, branch: str, files: dict[str, str], message: str, start: str = "main"
) -> None:
    """Commit ``files`` on ``branch``, cut from ``start``, and push it as a task worker does."""
    git(repo, "checkout", "-q", "-b", branch, start)
    commit(repo, files, message)
    git(repo, "push", "-q", "origin", branch)
    git(repo, "checkout", "-q", "main")


def push_more(repo: Path, branch: str, files: dict[str, str], message: str) -> None:
    """Push another commit to an existing task branch, as a worker still at work does."""
    git(repo, "checkout", "-q", branch)
    commit(repo, files, message)
    git(repo, "push", "-q", "origin", branch)
    git(repo, "checkout", "-q", "main")


def new_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: dict[str, str]) -> Path:
    """A clone whose origin's ``main`` holds ``files`` (and ignores ``.worktrees/``)."""
    isolate_git(tmp_path, monkeypatch)
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    repo = tmp_path / "repo"
    git(tmp_path, "clone", "-q", str(origin), str(repo))
    commit(repo, {".gitignore": ".worktrees/\n", **files}, "base")
    git(repo, "push", "-q", "origin", "main")
    return repo


def update_main(repo: Path, files: dict[str, str], message: str) -> None:
    """Move ``main`` on, on origin too, as later merges to main do."""
    commit(repo, files, message)
    git(repo, "push", "-q", "origin", "main")


def consolidate(repo: Path, branches: Sequence[str], name: str) -> Path:
    """merge_branches.sh's merge step: ``git merge --no-ff -X theirs`` per branch."""
    git(repo, "fetch", "-q", "origin")
    wt = repo / ".worktrees" / name
    git(repo, "worktree", "add", "-q", "-b", name, str(wt), "origin/main")
    for branch in branches:
        git(wt, "merge", "-q", "--no-ff", "--no-edit", "-X", "theirs", branch)
    return wt
