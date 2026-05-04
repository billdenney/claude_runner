"""Tests for cli.install_skills_cmd — installs skills into ~/.claude/skills/."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from claude_task_runner.cli.install_skills_cmd import (
    SKILL_NAMES,
    _packaged_skill_dir,
    app,
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``Path.home()`` so we never touch the real ~/.claude/."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


class TestPackagedSkillDir:
    def test_resolves_each_skill(self) -> None:
        for name in SKILL_NAMES:
            path = _packaged_skill_dir(name)
            assert path.exists()
            assert (path / "SKILL.md").exists()


class TestInstall:
    def test_default_yes_installs_all(self, runner: CliRunner, isolated_home: Path) -> None:
        result = runner.invoke(app, ["--yes"])
        assert result.exit_code == 0, result.stdout
        for name in SKILL_NAMES:
            installed = isolated_home / ".claude" / "skills" / name
            assert installed.exists() or installed.is_symlink()
            # SKILL.md must be reachable through whichever mode was used.
            assert (installed / "SKILL.md").is_file() or (
                installed / "SKILL.md"
            ).resolve().is_file()

    def test_copy_mode(self, runner: CliRunner, isolated_home: Path) -> None:
        result = runner.invoke(app, ["--yes", "--copy"])
        assert result.exit_code == 0, result.stdout
        for name in SKILL_NAMES:
            installed = isolated_home / ".claude" / "skills" / name
            # Copy mode produces a real directory, never a symlink.
            assert installed.is_dir()
            assert not installed.is_symlink()

    def test_existing_without_overwrite_skips(self, runner: CliRunner, isolated_home: Path) -> None:
        # Pre-populate with placeholder.
        target = isolated_home / ".claude" / "skills" / SKILL_NAMES[0]
        target.mkdir(parents=True)
        (target / "marker").write_text("preexisting")

        result = runner.invoke(app, ["--yes"])
        assert result.exit_code == 0, result.stdout
        # Marker survived because we skipped this skill.
        assert (target / "marker").read_text() == "preexisting"

    def test_overwrite_replaces(self, runner: CliRunner, isolated_home: Path) -> None:
        target = isolated_home / ".claude" / "skills" / SKILL_NAMES[0]
        target.mkdir(parents=True)
        (target / "marker").write_text("preexisting")

        result = runner.invoke(app, ["--yes", "--overwrite", "--copy"])
        assert result.exit_code == 0, result.stdout
        # Marker is gone (directory was replaced).
        assert not (target / "marker").exists()
        assert (target / "SKILL.md").is_file()


class TestUninstall:
    def test_removes_installed(self, runner: CliRunner, isolated_home: Path) -> None:
        runner.invoke(app, ["--yes", "--copy"])
        for name in SKILL_NAMES:
            assert (isolated_home / ".claude" / "skills" / name).exists()
        result = runner.invoke(app, ["uninstall", "--yes"])
        assert result.exit_code == 0, result.stdout
        for name in SKILL_NAMES:
            assert not (isolated_home / ".claude" / "skills" / name).exists()

    def test_uninstall_no_op_when_absent(self, runner: CliRunner, isolated_home: Path) -> None:
        result = runner.invoke(app, ["uninstall", "--yes"])
        assert result.exit_code == 0
        assert "No task-runner skills" in result.stdout


class TestList:
    def test_lists_status(self, runner: CliRunner, isolated_home: Path) -> None:
        runner.invoke(app, ["--yes", "--copy"])
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        for name in SKILL_NAMES:
            assert name in result.stdout
