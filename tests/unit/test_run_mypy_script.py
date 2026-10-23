"""``scripts/run_mypy.sh``, the pre-commit mypy hook's entry.

A ``language: system`` hook runs with the committer's PATH, which has no
mypy unless the dev virtualenv is activated. The script uses mypy from
PATH when there is one, else the checkout's ``.venv``, else, in a linked
worktree, the main checkout's ``.venv``; with none it exits 127 and says
where it looked. Each test runs it with a PATH that holds git and no mypy
but the fake ones it installs, which print where they are and their
arguments.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ._git_world import git, isolate_git

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_mypy.sh"


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git checkout with one commit, so a worktree can be added to it."""
    isolate_git(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "README").write_text("x\n")
    git(repo, "add", "README")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def _fake_mypy(path: Path, name: str, code: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho "{name} $*"\nexit {code}\n')
    path.chmod(0o755)


def _run(
    cwd: Path, tmp_path: Path, path_bin: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the script in ``cwd`` with git on PATH and, when given,
    ``path_bin``; every other directory holding a mypy is left off."""
    git_bin = tmp_path / "git-bin"
    git_bin.mkdir(exist_ok=True)
    git_exe = shutil.which("git")
    assert git_exe is not None
    if not (git_bin / "git").exists():
        (git_bin / "git").symlink_to(git_exe)
    dirs = [str(git_bin)] if path_bin is None else [str(path_bin), str(git_bin)]
    env = {**os.environ, "PATH": os.pathsep.join(dirs)}
    bash = shutil.which("bash")
    assert bash is not None
    return subprocess.run(
        [bash, str(SCRIPT), "src", "--strict"],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_mypy_on_path_comes_first(checkout: Path, tmp_path: Path) -> None:
    _fake_mypy(tmp_path / "active-venv" / "mypy", "path")
    _fake_mypy(checkout / ".venv" / "bin" / "mypy", "checkout")

    proc = _run(checkout, tmp_path, tmp_path / "active-venv")

    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "path src --strict\n", "")


def test_the_checkouts_own_venv(checkout: Path, tmp_path: Path) -> None:
    _fake_mypy(checkout / ".venv" / "bin" / "mypy", "checkout")

    proc = _run(checkout, tmp_path)

    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "checkout src --strict\n", "")


def test_a_worktree_uses_the_main_checkouts_venv(checkout: Path, tmp_path: Path) -> None:
    """From a subdirectory of a linked worktree too, as pre-commit may run
    from the worktree's root or a caller from anywhere in it."""
    _fake_mypy(checkout / ".venv" / "bin" / "mypy", "main")
    worktree = tmp_path / "wt"
    git(checkout, "worktree", "add", "-q", "-b", "feature", str(worktree))
    (worktree / "sub").mkdir()

    proc = _run(worktree / "sub", tmp_path)

    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "main src --strict\n", "")


def test_a_worktree_with_its_own_venv_prefers_it(checkout: Path, tmp_path: Path) -> None:
    _fake_mypy(checkout / ".venv" / "bin" / "mypy", "main")
    worktree = tmp_path / "wt"
    git(checkout, "worktree", "add", "-q", "-b", "feature", str(worktree))
    _fake_mypy(worktree / ".venv" / "bin" / "mypy", "worktree")

    proc = _run(worktree, tmp_path)

    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "worktree src --strict\n", "")


def test_mypys_exit_code_is_the_scripts(checkout: Path, tmp_path: Path) -> None:
    _fake_mypy(checkout / ".venv" / "bin" / "mypy", "checkout", code=3)

    proc = _run(checkout, tmp_path)

    assert (proc.returncode, proc.stdout) == (3, "checkout src --strict\n")


def test_no_mypy_anywhere_says_where_it_looked(checkout: Path, tmp_path: Path) -> None:
    proc = _run(checkout, tmp_path)

    top = os.path.realpath(checkout)
    assert (proc.returncode, proc.stdout, proc.stderr) == (
        127,
        "",
        f"run_mypy.sh: no mypy on PATH, in {top}/.venv or in {top}/.venv; activate the "
        "virtualenv holding the dev extras (pip install -e '.[dev]')\n",
    )
