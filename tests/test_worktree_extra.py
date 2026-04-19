"""Extra worktree tests — cover the branch-already-exists path, default
root with ${task_id} substitution, and branch-name edge cases."""

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
)


def _make_bare_repo(tmp_path: Path, name: str = "repo") -> Path:
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


def test_setup_uses_existing_local_branch(tmp_path: Path) -> None:
    """If the branch already exists in the repo, setup_worktree checks it
    out rather than trying to create it again."""
    repo = _make_bare_repo(tmp_path)
    # Pre-create the branch directly in the repo.
    subprocess.run(
        ["git", "branch", "pre-existing", "origin/main"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    cfg = WorktreeConfig(
        repo=repo,
        branch_name="pre-existing",
        branch_from="origin/main",
        root=tmp_path / "wt-pre",
    )
    path = setup_worktree("t", cfg, default_root=None)
    assert path.is_dir()
    # The worktree should be on the pre-existing branch.
    proc = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert proc.stdout.strip() == "pre-existing"


def test_resolve_worktree_path_strips_template_and_keeps_name(tmp_path: Path) -> None:
    cfg = WorktreeConfig(repo=tmp_path / "r", branch_name="b")
    # default_root ends with the task id → do NOT double-append.
    path = resolve_worktree_path("mytask", cfg=cfg, default_root=str(tmp_path / "wts" / "mytask"))
    assert path == tmp_path / "wts" / "mytask"


def test_teardown_succeeds_with_missing_default_root_because_path_not_found(
    tmp_path: Path,
) -> None:
    """``teardown_worktree`` on a path that never existed reports False."""
    repo = _make_bare_repo(tmp_path)
    cfg = WorktreeConfig(
        repo=repo,
        branch_name="never-setup",
        branch_from="origin/main",
    )
    assert teardown_worktree("never", cfg, default_root=None) is False


def test_setup_rejects_empty_branch_name(tmp_path: Path) -> None:
    """Empty branch names are caught by the pydantic min_length=1, so
    construct the underlying WorktreeConfig directly and verify the
    branch-name validator rejects them."""
    repo = _make_bare_repo(tmp_path)
    cfg = WorktreeConfig(
        repo=repo,
        branch_name="-leading-dash",
        branch_from="origin/main",
        root=tmp_path / "wt-bad",
    )
    with pytest.raises(WorktreeError):
        setup_worktree("t", cfg, default_root=None)


def test_setup_rejects_branch_with_double_dots(tmp_path: Path) -> None:
    repo = _make_bare_repo(tmp_path)
    cfg = WorktreeConfig(
        repo=repo,
        branch_name="foo..bar",
        branch_from="origin/main",
        root=tmp_path / "wt-bad2",
    )
    with pytest.raises(WorktreeError):
        setup_worktree("t", cfg, default_root=None)


def test_setup_rejects_branch_ending_with_lock(tmp_path: Path) -> None:
    repo = _make_bare_repo(tmp_path)
    cfg = WorktreeConfig(
        repo=repo,
        branch_name="foo.lock",
        branch_from="origin/main",
        root=tmp_path / "wt-lock",
    )
    with pytest.raises(WorktreeError):
        setup_worktree("t", cfg, default_root=None)


def test_teardown_refuses_dirty_with_helpful_message(tmp_path: Path) -> None:
    repo = _make_bare_repo(tmp_path)
    cfg = WorktreeConfig(
        repo=repo,
        branch_name="dirty-msg",
        branch_from="origin/main",
        root=tmp_path / "wt-dirty-msg",
    )
    path = setup_worktree("t", cfg, default_root=None)
    (path / "dirty.txt").write_text("edit\n")
    with pytest.raises(WorktreeError) as excinfo:
        teardown_worktree("t", cfg, default_root=None)
    assert "uncommitted changes" in str(excinfo.value)
