"""Tests for claude_runner.git.worktree."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from claude_runner.git.worktree import (
    WorktreeConfig,
    WorktreeError,
    resolve_worktree_path,
    setup_worktree,
    teardown_worktree,
    worktree_exists,
)


def _make_bare_repo(tmp_path: Path, name: str = "repo") -> Path:
    """Create a fresh git repo with a 'main' branch containing one commit.

    Registers a local ``origin`` that fetches from this same repo, so
    ``git fetch origin`` (used inside setup_worktree) is a no-op but still
    succeeds. That keeps the tests hermetic.
    """
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "ci@example.test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "CI"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    # origin = self — lets `git fetch origin` succeed without network.
    subprocess.run(
        ["git", "remote", "add", "origin", str(repo)],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "fetch", "origin"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def test_resolve_worktree_path_explicit_root(tmp_path: Path) -> None:
    cfg = WorktreeConfig(repo=tmp_path / "repo", branch_name="b", root=tmp_path / "wt")
    path = resolve_worktree_path("my-task", cfg=cfg, default_root=None)
    assert path == tmp_path / "wt"


def test_resolve_worktree_path_default_root_with_template(tmp_path: Path) -> None:
    cfg = WorktreeConfig(repo=tmp_path / "repo", branch_name="b")
    path = resolve_worktree_path("my-task", cfg=cfg, default_root=str(tmp_path / "wt-${task_id}"))
    assert path == tmp_path / "wt-my-task"


def test_resolve_worktree_path_default_root_appends_task_id(tmp_path: Path) -> None:
    cfg = WorktreeConfig(repo=tmp_path / "repo", branch_name="b")
    path = resolve_worktree_path("my-task", cfg=cfg, default_root=str(tmp_path / "wts"))
    assert path == tmp_path / "wts" / "my-task"


def test_resolve_worktree_path_fallback_repo_dot_claude(tmp_path: Path) -> None:
    cfg = WorktreeConfig(repo=tmp_path / "repo", branch_name="b")
    path = resolve_worktree_path("my-task", cfg=cfg, default_root=None)
    assert path == tmp_path / "repo" / ".claude" / "worktrees" / "my-task"


def test_setup_and_teardown_roundtrip(tmp_path: Path) -> None:
    repo = _make_bare_repo(tmp_path)
    cfg = WorktreeConfig(
        repo=repo,
        branch_name="task-1",
        branch_from="origin/main",
        root=tmp_path / "wt-task-1",
    )
    path = setup_worktree("task-1", cfg, default_root=None)
    assert worktree_exists(path)
    # Branch check.
    proc = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert proc.stdout.strip() == "task-1"

    removed = teardown_worktree("task-1", cfg, default_root=None)
    assert removed is True
    assert not worktree_exists(path)


def test_setup_is_idempotent_on_same_branch(tmp_path: Path) -> None:
    repo = _make_bare_repo(tmp_path)
    cfg = WorktreeConfig(
        repo=repo,
        branch_name="task-2",
        branch_from="origin/main",
        root=tmp_path / "wt-task-2",
    )
    p1 = setup_worktree("task-2", cfg, default_root=None)
    p2 = setup_worktree("task-2", cfg, default_root=None)
    assert p1 == p2


def test_setup_rejects_wrong_branch_at_path(tmp_path: Path) -> None:
    repo = _make_bare_repo(tmp_path)
    cfg_first = WorktreeConfig(
        repo=repo,
        branch_name="task-a",
        branch_from="origin/main",
        root=tmp_path / "wt-shared",
    )
    setup_worktree("task-a", cfg_first, default_root=None)

    cfg_second = WorktreeConfig(
        repo=repo,
        branch_name="task-b",
        branch_from="origin/main",
        root=tmp_path / "wt-shared",
    )
    with pytest.raises(WorktreeError):
        setup_worktree("task-b", cfg_second, default_root=None)


def test_teardown_refuses_dirty_worktree(tmp_path: Path) -> None:
    repo = _make_bare_repo(tmp_path)
    cfg = WorktreeConfig(
        repo=repo,
        branch_name="task-dirty",
        branch_from="origin/main",
        root=tmp_path / "wt-dirty",
    )
    path = setup_worktree("task-dirty", cfg, default_root=None)
    (path / "stray.txt").write_text("uncommitted\n")
    with pytest.raises(WorktreeError):
        teardown_worktree("task-dirty", cfg, default_root=None)


def test_teardown_reports_missing_as_false(tmp_path: Path) -> None:
    repo = _make_bare_repo(tmp_path)
    cfg = WorktreeConfig(
        repo=repo,
        branch_name="never-existed",
        branch_from="origin/main",
        root=tmp_path / "wt-missing",
    )
    assert teardown_worktree("nope", cfg, default_root=None) is False


def test_setup_rejects_invalid_branch_name(tmp_path: Path) -> None:
    repo = _make_bare_repo(tmp_path)
    cfg = WorktreeConfig(
        repo=repo,
        branch_name="bad branch name",  # space is invalid
        branch_from="origin/main",
        root=tmp_path / "wt-bad",
    )
    with pytest.raises(WorktreeError):
        setup_worktree("t", cfg, default_root=None)


def test_setup_rejects_non_repo(tmp_path: Path) -> None:
    cfg = WorktreeConfig(
        repo=tmp_path / "not-a-repo",
        branch_name="fine",
        root=tmp_path / "wt-nrp",
    )
    (tmp_path / "not-a-repo").mkdir()
    with pytest.raises(WorktreeError):
        setup_worktree("t", cfg, default_root=None)
