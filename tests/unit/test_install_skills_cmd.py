"""Tests for cli/install_skills_cmd.py — install / uninstall / list.

We use a temp HOME so the real ``~/.claude/skills/`` is never touched.
"""

from __future__ import annotations

import errno
import importlib
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from claude_task_runner.cli.install_skills_cmd import (
    SKILL_NAMES,
    _install_one,
    _packaged_skill_dir,
    _skills_target_dir,
    _supports_symlinks,
    app,
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def home_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect Path.home() to a clean tmp dir so the real ~/.claude/skills
    is untouched by these tests."""
    home = tmp_path / "homedir"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_skills_target_dir_creates_path(home_tmp: Path) -> None:
    target = _skills_target_dir()
    assert target == home_tmp / ".claude" / "skills"
    assert target.is_dir()


def test_packaged_skill_dir_resolves_each_name() -> None:
    """Every packaged skill (operator + agent) must resolve to an existing path."""
    for name in SKILL_NAMES:
        path = _packaged_skill_dir(name)
        assert path.exists()
        assert path.is_dir()
        assert (path / "SKILL.md").exists()


def test_packaged_skill_dir_raises_for_unknown(home_tmp: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _packaged_skill_dir("no-such-skill-name")


def _blocked_skills_import(monkeypatch: pytest.MonkeyPatch) -> ModuleNotFoundError:
    """Make ``claude_task_runner.skills`` unimportable, as in a wheel built
    without ``skills/``, and return the error importing it now raises."""
    monkeypatch.setitem(sys.modules, "claude_task_runner.skills", None)
    with pytest.raises(ModuleNotFoundError) as excinfo:
        importlib.import_module("claude_task_runner.skills")
    return excinfo.value


def test_packaged_skill_dir_raises_file_not_found_without_skills_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing skills package raises the FileNotFoundError that
    ``install-skills`` reports as ``missing skill``, not ModuleNotFoundError."""
    import_error = _blocked_skills_import(monkeypatch)
    with pytest.raises(FileNotFoundError) as excinfo:
        _packaged_skill_dir("runner-status")
    assert str(excinfo.value) == (
        "packaged skill 'runner-status' not found: "
        f"the claude_task_runner.skills package is missing ({import_error})"
    )
    assert isinstance(excinfo.value.__cause__, ModuleNotFoundError)


def test_supports_symlinks_yes_on_normal_fs(tmp_path: Path) -> None:
    """A normal POSIX tmpfs supports symlinks."""
    assert _supports_symlinks(tmp_path) is True
    # Probe file must be cleaned up.
    assert not (tmp_path / ".symlink_probe").exists()


def test_supports_symlinks_no_when_oserror(tmp_path: Path) -> None:
    """A filesystem rejecting symlink_to → returns False."""
    with patch.object(Path, "symlink_to", side_effect=OSError("operation not permitted")):
        assert _supports_symlinks(tmp_path) is False


# ---------------------------------------------------------------------------
# _install_one
# ---------------------------------------------------------------------------


def test_install_one_symlink(home_tmp: Path) -> None:
    target = home_tmp / ".claude" / "skills"
    target.mkdir(parents=True, exist_ok=True)
    installed, detail = _install_one(
        "runner-status",
        target_dir=target,
        use_symlinks=True,
        overwrite=False,
    )
    assert installed is True
    assert "symlinked" in detail
    dst = target / "runner-status"
    assert dst.is_symlink()


def test_install_one_copy(home_tmp: Path) -> None:
    target = home_tmp / ".claude" / "skills"
    target.mkdir(parents=True, exist_ok=True)
    installed, detail = _install_one(
        "runner-status",
        target_dir=target,
        use_symlinks=False,
        overwrite=False,
    )
    assert installed is True
    assert "copied" in detail
    dst = target / "runner-status"
    assert dst.is_dir()
    assert not dst.is_symlink()


def test_install_one_skips_existing_no_overwrite(home_tmp: Path) -> None:
    target = home_tmp / ".claude" / "skills"
    target.mkdir(parents=True, exist_ok=True)
    (target / "runner-status").mkdir()
    installed, detail = _install_one(
        "runner-status",
        target_dir=target,
        use_symlinks=True,
        overwrite=False,
    )
    assert installed is False
    assert "already present" in detail


def test_install_one_overwrite_replaces_existing(home_tmp: Path) -> None:
    target = home_tmp / ".claude" / "skills"
    target.mkdir(parents=True, exist_ok=True)
    # A pre-existing different content directory.
    dst = target / "runner-status"
    dst.mkdir()
    (dst / "old-file.md").write_text("legacy", encoding="utf-8")
    installed, _detail = _install_one(
        "runner-status",
        target_dir=target,
        use_symlinks=True,
        overwrite=True,
    )
    assert installed is True
    # The legacy file is gone (replaced by symlink to package).
    assert not (dst / "old-file.md").exists() or dst.is_symlink()


def test_install_one_overwrite_replaces_existing_symlink(home_tmp: Path) -> None:
    target = home_tmp / ".claude" / "skills"
    target.mkdir(parents=True, exist_ok=True)
    dst = target / "runner-status"
    # Pre-existing symlink to nowhere.
    dst.symlink_to(home_tmp / "no-such-target")
    installed, _detail = _install_one(
        "runner-status",
        target_dir=target,
        use_symlinks=False,
        overwrite=True,
    )
    assert installed is True


# ---------------------------------------------------------------------------
# install (the typer callback) end-to-end
# ---------------------------------------------------------------------------


def test_install_skills_yes_symlinks(runner: CliRunner, home_tmp: Path) -> None:
    result = runner.invoke(app, ["--yes"])
    assert result.exit_code == 0
    for name in SKILL_NAMES:
        assert (home_tmp / ".claude" / "skills" / name).exists()


def test_install_skills_yes_copy(runner: CliRunner, home_tmp: Path) -> None:
    result = runner.invoke(app, ["--yes", "--copy"])
    assert result.exit_code == 0
    for name in SKILL_NAMES:
        installed_path = home_tmp / ".claude" / "skills" / name
        assert installed_path.is_dir()
        assert not installed_path.is_symlink()


def test_install_skills_aborts_on_no(runner: CliRunner, home_tmp: Path) -> None:
    """Default prompt answer is N → exit 1."""
    result = runner.invoke(app, [], input="n\n")
    assert result.exit_code == 1
    assert "Aborted" in result.stdout


def test_install_skills_idempotent_without_overwrite(runner: CliRunner, home_tmp: Path) -> None:
    """Second --yes invocation reports skipped, not error."""
    runner.invoke(app, ["--yes"])
    result = runner.invoke(app, ["--yes"])
    assert result.exit_code == 0
    assert "skipped" in result.stdout
    assert "Failed to install" not in result.stdout


def test_install_skills_overwrite_flag(runner: CliRunner, home_tmp: Path) -> None:
    runner.invoke(app, ["--yes"])
    result = runner.invoke(app, ["--yes", "--overwrite"])
    assert result.exit_code == 0
    assert "installed" in result.stdout


def test_install_skills_propagates_packaged_lookup_error(runner: CliRunner, home_tmp: Path) -> None:
    """If the packaged skill dir resolves to a missing path, exit 2.

    The path is printed as it is: Rich markup would drop the ``[venv]``."""
    message = "packaged skill 'runner-status' not found at expected path /opt/[venv]/runner-status"
    with patch(
        "claude_task_runner.cli.install_skills_cmd._packaged_skill_dir",
        side_effect=FileNotFoundError(message),
    ):
        result = runner.invoke(app, ["--yes"])
    assert result.exit_code == 2
    assert result.stdout == f"missing skill: {message}\n"


def test_install_skills_missing_skills_package_exits_2(
    runner: CliRunner, home_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the skills package (a wheel built without ``skills/``), the
    first skill is reported as missing and the command exits 2, instead of
    a ModuleNotFoundError traceback and exit 1. Nothing is installed."""
    import_error = _blocked_skills_import(monkeypatch)
    result = runner.invoke(app, ["--yes"])
    assert result.exit_code == 2
    assert result.stdout == (
        f"missing skill: packaged skill {SKILL_NAMES[0]!r} not found: "
        f"the claude_task_runner.skills package is missing ({import_error})\n"
    )
    assert list((home_tmp / ".claude" / "skills").iterdir()) == []


def test_install_skills_continues_on_per_skill_os_error(runner: CliRunner, home_tmp: Path) -> None:
    """If installing one skill raises OSError, the rest are still installed,
    the failure is reported, and the command exits 2, not 0."""
    err_counter = {"calls": 0}

    def flaky(name, **kw):
        err_counter["calls"] += 1
        if err_counter["calls"] == 2:
            raise OSError("perm denied")
        return _install_one(name, **kw)

    with patch(
        "claude_task_runner.cli.install_skills_cmd._install_one",
        side_effect=flaky,
    ):
        result = runner.invoke(app, ["--yes"])
    assert result.exit_code == 2
    assert f"  failed to install {SKILL_NAMES[1]}: perm denied\n" in result.stdout
    assert result.stdout.endswith(f"Failed to install 1 of {len(SKILL_NAMES)} skills.\n")
    installed = sorted(p.name for p in (home_tmp / ".claude" / "skills").iterdir())
    assert installed == sorted(n for n in SKILL_NAMES if n != SKILL_NAMES[1])


def test_install_skills_failing_copy_exits_2(runner: CliRunner, home_tmp: Path) -> None:
    """A copy that fails is reported with its error, the other skills are
    still copied, and the command exits 2.

    The error is printed as raised: Rich markup would drop the ``[team]``."""
    failing = SKILL_NAMES[-1]
    real_copytree = shutil.copytree

    def copytree(src, dst, *args, **kwargs):
        if Path(dst).name == failing:
            raise OSError(errno.ENOSPC, "No space left on device", "/srv/[team]/skills")
        return real_copytree(src, dst, *args, **kwargs)

    with patch("claude_task_runner.cli.install_skills_cmd.shutil.copytree", copytree):
        result = runner.invoke(app, ["--yes", "--copy"])
    assert result.exit_code == 2
    assert (
        f"  failed to install {failing}: "
        f"[Errno {errno.ENOSPC}] No space left on device: '/srv/[team]/skills'\n"
    ) in result.stdout
    assert result.stdout.endswith(f"Failed to install 1 of {len(SKILL_NAMES)} skills.\n")
    target = home_tmp / ".claude" / "skills"
    copied = sorted(p.name for p in target.iterdir())
    assert copied == sorted(n for n in SKILL_NAMES if n != failing)
    assert all((target / n).is_dir() and not (target / n).is_symlink() for n in copied)


@pytest.mark.skipif(os.geteuid() == 0, reason="root is not denied by directory permissions")
def test_install_skills_read_only_target_exits_2(runner: CliRunner, home_tmp: Path) -> None:
    """With ``~/.claude/skills`` read-only, the symlink probe fails, so the
    install falls back to copying, and every copy is refused. Each failure
    is reported and the command exits 2, not 0."""
    target = home_tmp / ".claude" / "skills"
    target.mkdir(parents=True)
    target.chmod(0o555)
    try:
        result = runner.invoke(app, ["--yes"])
    finally:
        target.chmod(0o755)
    assert result.exit_code == 2
    for name in SKILL_NAMES:
        assert (
            f"  failed to install {name}: [Errno {errno.EACCES}] Permission denied: "
            f"'{target / name}'\n"
        ) in result.stdout
    assert result.stdout.endswith(
        f"Failed to install {len(SKILL_NAMES)} of {len(SKILL_NAMES)} skills.\n"
    )
    assert list(target.iterdir()) == []


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------


def test_uninstall_no_skills_present(runner: CliRunner, home_tmp: Path) -> None:
    result = runner.invoke(app, ["uninstall"])
    assert result.exit_code == 0
    assert "No task-runner skills" in result.stdout


def test_uninstall_yes_removes_all(runner: CliRunner, home_tmp: Path) -> None:
    runner.invoke(app, ["--yes"])
    result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 0
    for name in SKILL_NAMES:
        assert not (home_tmp / ".claude" / "skills" / name).exists()


def test_uninstall_aborts_on_no(runner: CliRunner, home_tmp: Path) -> None:
    runner.invoke(app, ["--yes"])
    result = runner.invoke(app, ["uninstall"], input="n\n")
    assert result.exit_code == 1
    assert "Aborted" in result.stdout


@pytest.mark.parametrize("copy", [False, True], ids=["symlinks", "copies"])
def test_uninstall_continues_on_per_skill_os_error(
    runner: CliRunner, home_tmp: Path, copy: bool
) -> None:
    """If removing one skill raises OSError, the rest are still removed, the
    failure is reported, and the command exits 2, not 0.

    The error is printed as raised: Rich markup would drop the ``[locked]``."""
    runner.invoke(app, ["--yes", "--copy"] if copy else ["--yes"])
    failing = SKILL_NAMES[1]
    orig_unlink = Path.unlink
    orig_rmtree = shutil.rmtree

    def flaky_unlink(self, missing_ok=False):
        if self.name == failing:
            raise OSError("perm denied on [locked]/unlink")
        return orig_unlink(self, missing_ok=missing_ok)

    def flaky_rmtree(p, *args, **kwargs):
        if Path(p).name == failing:
            raise OSError("perm denied on [locked]/rmtree")
        return orig_rmtree(p, *args, **kwargs)

    with (
        patch.object(Path, "unlink", flaky_unlink),
        patch("claude_task_runner.cli.install_skills_cmd.shutil.rmtree", flaky_rmtree),
    ):
        result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 2
    how = "rmtree" if copy else "unlink"
    assert f"  failed to remove {failing}: perm denied on [locked]/{how}\n" in result.stdout
    assert result.stdout.endswith(f"Failed to remove 1 of {len(SKILL_NAMES)} skills.\n")
    assert [p.name for p in (home_tmp / ".claude" / "skills").iterdir()] == [failing]


@pytest.mark.skipif(os.geteuid() == 0, reason="root is not denied by directory permissions")
def test_uninstall_read_only_target_exits_2(runner: CliRunner, home_tmp: Path) -> None:
    """With ``~/.claude/skills`` read-only, every removal is refused. Each
    failure is reported, the skills stay, and the command exits 2, not 0."""
    runner.invoke(app, ["--yes"])
    target = home_tmp / ".claude" / "skills"
    target.chmod(0o555)
    try:
        result = runner.invoke(app, ["uninstall", "--yes"])
    finally:
        target.chmod(0o755)
    assert result.exit_code == 2
    for name in SKILL_NAMES:
        # The symlinks are unlinked; rmtree's message differs between Pythons.
        assert (
            f"  failed to remove {name}: [Errno {errno.EACCES}] Permission denied: "
            f"'{target / name}'\n"
        ) in result.stdout
    assert result.stdout.endswith(
        f"Failed to remove {len(SKILL_NAMES)} of {len(SKILL_NAMES)} skills.\n"
    )
    assert sorted(p.name for p in target.iterdir()) == sorted(SKILL_NAMES)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_when_none_installed(runner: CliRunner, home_tmp: Path) -> None:
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    # 4 skill names; each one prints with ✗ marker.
    for name in SKILL_NAMES:
        assert name in result.stdout
    assert "not installed" in result.stdout


def test_list_when_symlinks_present(runner: CliRunner, home_tmp: Path) -> None:
    runner.invoke(app, ["--yes"])
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "symlinked" in result.stdout


def test_list_when_copies_present(runner: CliRunner, home_tmp: Path) -> None:
    runner.invoke(app, ["--yes", "--copy"])
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "copied" in result.stdout
