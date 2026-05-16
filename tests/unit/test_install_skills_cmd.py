"""Tests for cli/install_skills_cmd.py — install / uninstall / list.

We use a temp HOME so the real ``~/.claude/skills/`` is never touched.
"""

from __future__ import annotations

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
    """All four packaged skills must resolve to existing paths."""
    for name in SKILL_NAMES:
        path = _packaged_skill_dir(name)
        assert path.exists()
        assert path.is_dir()
        assert (path / "SKILL.md").exists()


def test_packaged_skill_dir_raises_for_unknown(home_tmp: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _packaged_skill_dir("no-such-skill-name")


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


def test_install_skills_overwrite_flag(runner: CliRunner, home_tmp: Path) -> None:
    runner.invoke(app, ["--yes"])
    result = runner.invoke(app, ["--yes", "--overwrite"])
    assert result.exit_code == 0
    assert "installed" in result.stdout


def test_install_skills_propagates_packaged_lookup_error(runner: CliRunner, home_tmp: Path) -> None:
    """If the packaged skill dir resolves to a missing path, exit 2."""
    with patch(
        "claude_task_runner.cli.install_skills_cmd._packaged_skill_dir",
        side_effect=FileNotFoundError("not on disk"),
    ):
        result = runner.invoke(app, ["--yes"])
    assert result.exit_code == 2
    assert "missing skill" in result.stdout


def test_install_skills_continues_on_per_skill_os_error(runner: CliRunner, home_tmp: Path) -> None:
    """If installing one skill raises OSError, continue with the rest."""
    err_counter = {"calls": 0}

    real_install_one = __import__(
        "claude_task_runner.cli.install_skills_cmd", fromlist=["_install_one"]
    )._install_one

    def flaky(name, **kw):
        err_counter["calls"] += 1
        if err_counter["calls"] == 2:
            raise OSError("perm denied")
        return real_install_one(name, **kw)

    with patch(
        "claude_task_runner.cli.install_skills_cmd._install_one",
        side_effect=flaky,
    ):
        result = runner.invoke(app, ["--yes"])
    assert result.exit_code == 0
    assert "perm denied" in result.stdout


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


def test_uninstall_continues_on_per_skill_os_error(runner: CliRunner, home_tmp: Path) -> None:
    runner.invoke(app, ["--yes"])

    err_counter = {"calls": 0}
    orig_unlink = Path.unlink
    orig_rmtree = None
    import shutil

    orig_rmtree = shutil.rmtree

    def flaky_unlink(self):
        err_counter["calls"] += 1
        if err_counter["calls"] == 2:
            raise OSError("perm denied on unlink")
        return orig_unlink(self)

    def flaky_rmtree(p):
        err_counter["calls"] += 1
        if err_counter["calls"] == 2:
            raise OSError("perm denied on rmtree")
        return orig_rmtree(p)

    with (
        patch.object(Path, "unlink", flaky_unlink),
        patch("claude_task_runner.cli.install_skills_cmd.shutil.rmtree", flaky_rmtree),
    ):
        result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 0
    assert "failed to remove" in result.stdout


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
