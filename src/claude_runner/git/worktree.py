"""Per-task git worktree setup and teardown.

The runner creates a dedicated worktree for each task whose YAML declares a
``git_worktree`` block, so concurrent tasks cannot clobber each other's
working tree. Worktrees are preserved through ``AWAITING_INPUT`` so a task
that is waiting on operator input can resume in the same checkout.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)


class WorktreeError(RuntimeError):
    """Raised when a worktree operation cannot be completed safely."""


@dataclass(slots=True, frozen=True)
class WorktreeConfig:
    """Immutable description of one task's worktree requirement."""

    repo: Path
    """Absolute path to the source repository (where `.git` lives)."""
    branch_name: str
    """Name of the new branch to create for the worktree."""
    branch_from: str = "origin/main"
    """Git ref to branch from (default: ``origin/main``)."""
    root: Path | None = None
    """Explicit worktree path override; when None, the caller resolves it
    from ``claude_runner.toml::worktree_root`` or a default."""


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` with sensible defaults. Propagates non-zero exits."""
    if shutil.which("git") is None:
        raise WorktreeError("'git' executable not found on PATH")
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)} failed (rc={proc.returncode}) in {cwd}\n"
            f"stderr: {proc.stderr.strip()}"
        )
    return proc


def resolve_worktree_path(task_id: str, *, cfg: WorktreeConfig, default_root: str | None) -> Path:
    """Compute the worktree path given the task id, YAML override, and config default.

    Precedence: ``cfg.root`` (explicit) > ``default_root`` (from
    ``claude_runner.toml``) > ``<repo>/.claude/worktrees/<task_id>``.

    ``${task_id}`` in ``default_root`` is substituted; if absent, ``task_id``
    is appended as a subdirectory.
    """
    if cfg.root is not None:
        return cfg.root
    if default_root:
        root = default_root.replace("${task_id}", task_id)
        p = Path(root).expanduser()
        if "${task_id}" not in default_root and p.name != task_id:
            p = p / task_id
        return p
    return cfg.repo / ".claude" / "worktrees" / task_id


def worktree_exists(path: Path) -> bool:
    """Return True if ``path`` is a registered worktree of any repo."""
    return path.is_dir() and (path / ".git").exists()


def _branch_exists_locally(branch: str, *, repo: Path) -> bool:
    try:
        proc = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=str(repo),
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise WorktreeError(f"git show-ref failed: {exc}") from exc
    return proc.returncode == 0


def setup_worktree(task_id: str, cfg: WorktreeConfig, *, default_root: str | None) -> Path:
    """Create (or reuse) a worktree for ``task_id`` and return its path.

    Idempotent: if a worktree already exists at the resolved path on the
    expected branch, it is returned as-is. If the worktree path exists but
    is dirty (uncommitted changes) and on the wrong branch, raises
    :class:`WorktreeError` rather than silently damaging work.
    """
    if not cfg.repo.is_dir():
        raise WorktreeError(f"worktree repo path is not a directory: {cfg.repo}")
    if not (cfg.repo / ".git").exists():
        raise WorktreeError(f"worktree repo is not a git repository: {cfg.repo}")
    if not _is_valid_branch_name(cfg.branch_name):
        raise WorktreeError(f"invalid git branch name: {cfg.branch_name!r}")

    path = resolve_worktree_path(task_id, cfg=cfg, default_root=default_root)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Fetch once so branch_from (typically origin/main) is up to date.
    _run_git(["fetch", "origin"], cwd=cfg.repo)

    if worktree_exists(path):
        # Reuse if the current branch matches.
        current = _run_git(["branch", "--show-current"], cwd=path).stdout.strip()
        if current == cfg.branch_name:
            _log.info("reusing existing worktree %s on branch %s", path, cfg.branch_name)
            return path
        raise WorktreeError(
            f"worktree {path} exists but is on branch {current!r}, expected {cfg.branch_name!r}"
        )

    # Choose the right git worktree add incantation based on whether the
    # branch already exists somewhere in the repo.
    if _branch_exists_locally(cfg.branch_name, repo=cfg.repo):
        _run_git(
            ["worktree", "add", str(path), cfg.branch_name],
            cwd=cfg.repo,
        )
    else:
        _run_git(
            ["worktree", "add", "-b", cfg.branch_name, str(path), cfg.branch_from],
            cwd=cfg.repo,
        )
    _log.info("created worktree %s on branch %s (from %s)", path, cfg.branch_name, cfg.branch_from)
    return path


def teardown_worktree(task_id: str, cfg: WorktreeConfig, *, default_root: str | None) -> bool:
    """Remove a worktree created by :func:`setup_worktree`.

    Returns True if the worktree was removed, False if it did not exist.
    Refuses to remove a worktree with uncommitted changes (caller should
    decide whether to force via ``--force`` flag if they want). The source
    branch is never deleted; only the worktree checkout.
    """
    path = resolve_worktree_path(task_id, cfg=cfg, default_root=default_root)
    if not worktree_exists(path):
        return False

    status = _run_git(["status", "--porcelain"], cwd=path).stdout
    if status.strip():
        raise WorktreeError(
            f"refusing to remove worktree {path}: uncommitted changes present\n{status.rstrip()}"
        )

    _run_git(["worktree", "remove", str(path)], cwd=cfg.repo)
    _log.info("removed worktree %s", path)
    return True


# Git's branch-naming rules are complex; this is a conservative subset that
# covers everything we actually produce.
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]*$")


def _is_valid_branch_name(name: str) -> bool:
    if not name or len(name) > 255:
        return False
    if name.startswith("-") or name.endswith("/") or name.endswith(".lock"):
        return False
    if ".." in name or "//" in name or "\\" in name:
        return False
    return bool(_BRANCH_RE.match(name))
