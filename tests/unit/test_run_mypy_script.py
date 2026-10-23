"""``scripts/run_mypy.sh``, the pre-commit mypy hook's entry.

A ``language: system`` hook runs with the committer's PATH, which has no
mypy unless the dev virtualenv is activated. The script uses the
checkout's ``.venv``, else, in a linked worktree, the main checkout's
``.venv``, else mypy from PATH; with none it exits 127 and says where it
looked. Each test runs the script as pre-commit does, by its path (its
shebang and exec bit), with a PATH that holds git, bash and no mypy but the
fake ones it installs, which print which they are and their arguments.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from ._git_world import git, isolate_git

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "run_mypy.sh"
ADVICE = "activate the virtualenv holding the dev extras (pip install -e '.[dev]')"


def _make_checkout(path: Path) -> Path:
    """A git checkout at ``path`` with one commit, so a worktree can be added."""
    path.mkdir(parents=True)
    git(path, "init", "-q")
    (path / "README").write_text("x\n")
    git(path, "add", "README")
    git(path, "commit", "-q", "-m", "init")
    return path


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    isolate_git(tmp_path, monkeypatch)
    return _make_checkout(tmp_path / "repo")


def _worktree(checkout: Path, path: Path) -> Path:
    git(checkout, "worktree", "add", "-q", "-b", "feature", str(path))
    return path


def _fake_mypy(path: Path, name: str, code: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho "{name} $*"\nexit {code}\n')
    path.chmod(0o755)


def _run(
    cwd: Path,
    tmp_path: Path,
    path_bin: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run the script by its path in ``cwd``, with git, bash and, when
    given, ``path_bin`` on PATH; every other directory holding a mypy is
    left off. Discovery of a repository stops at ``tmp_path``. Returns the
    exit code, stdout and stderr."""
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    for tool in ("git", "bash"):
        found = shutil.which(tool)
        assert found is not None
        if not (tools / tool).exists():
            (tools / tool).symlink_to(found)
    dirs = [str(tools)] if path_bin is None else [str(path_bin), str(tools)]
    full_env = {
        **os.environ,
        "PATH": os.pathsep.join(dirs),
        "GIT_CEILING_DIRECTORIES": str(tmp_path),
        **(env or {}),
    }
    proc = subprocess.run(
        [str(SCRIPT), "src", "--strict"],
        cwd=cwd,
        env=full_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_the_pre_commit_hook_runs_this_script() -> None:
    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text())
    hooks = [hook for repo in config["repos"] for hook in repo["hooks"] if hook["id"] == "mypy"]
    assert [(h["entry"], h["language"], h["pass_filenames"], h["types"]) for h in hooks] == [
        ("scripts/run_mypy.sh src", "system", False, ["python"])
    ]
    assert os.access(SCRIPT, os.X_OK)


def test_the_checkouts_venv_comes_before_path(checkout: Path, tmp_path: Path) -> None:
    """An unrelated mypy on PATH lacks the project's plugin and stubs."""
    _fake_mypy(tmp_path / "elsewhere" / "mypy", "path")
    _fake_mypy(checkout / ".venv" / "bin" / "mypy", "checkout")

    assert _run(checkout, tmp_path, tmp_path / "elsewhere") == (0, "checkout src --strict\n", "")


def test_a_worktree_uses_the_main_checkouts_venv_before_path(
    checkout: Path, tmp_path: Path
) -> None:
    """From a subdirectory of a linked worktree too, as a caller may run it
    from anywhere in it."""
    _fake_mypy(checkout / ".venv" / "bin" / "mypy", "main")
    _fake_mypy(tmp_path / "elsewhere" / "mypy", "path")
    worktree = _worktree(checkout, tmp_path / "wt")
    (worktree / "sub").mkdir()

    assert _run(worktree / "sub", tmp_path, tmp_path / "elsewhere") == (
        0,
        "main src --strict\n",
        "",
    )


def test_a_worktree_with_its_own_venv_prefers_it(checkout: Path, tmp_path: Path) -> None:
    _fake_mypy(checkout / ".venv" / "bin" / "mypy", "main")
    worktree = _worktree(checkout, tmp_path / "wt")
    _fake_mypy(worktree / ".venv" / "bin" / "mypy", "worktree")

    assert _run(worktree, tmp_path) == (0, "worktree src --strict\n", "")


def test_path_when_no_venv_has_mypy(checkout: Path, tmp_path: Path) -> None:
    _fake_mypy(tmp_path / "active" / "mypy", "path")

    assert _run(checkout, tmp_path, tmp_path / "active") == (0, "path src --strict\n", "")


def test_git_dir_set_as_git_sets_it_for_a_worktrees_hook(checkout: Path, tmp_path: Path) -> None:
    """git runs a linked worktree's hooks in its root with GIT_DIR naming
    the worktree's git directory."""
    _fake_mypy(checkout / ".venv" / "bin" / "mypy", "main")
    worktree = _worktree(checkout, tmp_path / "wt")

    git_dir = str(checkout / ".git" / "worktrees" / "wt")
    assert _run(worktree, tmp_path, env={"GIT_DIR": git_dir}) == (0, "main src --strict\n", "")


def test_a_path_with_a_space(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolate_git(tmp_path, monkeypatch)
    checkout = _make_checkout(tmp_path / "my repo")
    _fake_mypy(checkout / ".venv" / "bin" / "mypy", "spaced")

    assert _run(checkout, tmp_path) == (0, "spaced src --strict\n", "")


def test_outside_a_git_checkout_only_path_is_searched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``.venv`` there is no checkout's, and git's own error is not
    shown."""
    isolate_git(tmp_path, monkeypatch)
    plain = tmp_path / "plain"
    _fake_mypy(plain / ".venv" / "bin" / "mypy", "not-a-checkout")

    without = _run(plain, tmp_path)
    _fake_mypy(tmp_path / "active" / "mypy", "path")
    with_path = _run(plain, tmp_path, tmp_path / "active")

    assert without == (127, "", f"run_mypy.sh: no mypy found (looked in: PATH); {ADVICE}\n")
    assert with_path == (0, "path src --strict\n", "")


def test_mypys_exit_code_is_the_scripts(checkout: Path, tmp_path: Path) -> None:
    _fake_mypy(checkout / ".venv" / "bin" / "mypy", "checkout", code=3)

    assert _run(checkout, tmp_path)[:2] == (3, "checkout src --strict\n")


def test_no_mypy_in_a_checkout_names_each_place_once(checkout: Path, tmp_path: Path) -> None:
    top = os.path.realpath(checkout)
    assert _run(checkout, tmp_path) == (
        127,
        "",
        f"run_mypy.sh: no mypy found (looked in: {top}/.venv, PATH); {ADVICE}\n",
    )


def test_no_mypy_in_a_worktree_names_both_venvs(checkout: Path, tmp_path: Path) -> None:
    worktree = _worktree(checkout, tmp_path / "wt")

    top, main = os.path.realpath(worktree), os.path.realpath(checkout)
    assert _run(worktree, tmp_path) == (
        127,
        "",
        f"run_mypy.sh: no mypy found (looked in: {top}/.venv, {main}/.venv, PATH); {ADVICE}\n",
    )
