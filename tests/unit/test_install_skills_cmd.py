"""Tests for cli/install_skills_cmd.py — install / uninstall / list.

We use a temp HOME so the real ``~/.claude/skills/`` is never touched.
Permission errors are injected with ``_fs_faults``, which raises them where
the kernel would, so these tests also run as root.
"""

from __future__ import annotations

import errno
import importlib
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from claude_task_runner.cli.install_skills_cmd import (
    _REMOVAL_KINDS,
    SKILL_NAMES,
    SkillState,
    _install_one,
    _packaged_skill_dir,
    _supports_symlinks,
    app,
    skill_state,
    skills_dir,
)

from ._fs_faults import read_only, unsearchable
from ._skill_installs import gone, make


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def home_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect Path.home() to a clean tmp dir so the real ~/.claude/skills
    is untouched by these tests.

    Its name has brackets: a line that printed a path through Rich markup
    would drop the ``[dir]``, so the tests that pin output catch it."""
    home = tmp_path / "home[dir]"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture
def skills(home_tmp: Path) -> Path:
    """An empty ``~/.claude/skills/``."""
    path = home_tmp / ".claude" / "skills"
    path.mkdir(parents=True)
    return path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_skills_dir_is_under_home_and_not_created(home_tmp: Path) -> None:
    assert skills_dir() == home_tmp / ".claude" / "skills"
    assert not (home_tmp / ".claude").exists()


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


def test_supports_symlinks_no_when_the_probe_cannot_be_removed(tmp_path: Path) -> None:
    probe = tmp_path / ".symlink_probe"
    real_unlink = Path.unlink

    def unlink(self, missing_ok=False):
        if self == probe:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_unlink(self, missing_ok=missing_ok)

    with patch.object(Path, "unlink", unlink):
        assert _supports_symlinks(tmp_path) is False
    assert probe.is_symlink()


def test_supports_symlinks_lets_an_unexpected_error_through(tmp_path: Path) -> None:
    """The ``return`` that was in its ``finally`` block swallowed this error
    whenever removing the probe failed too."""
    with (
        patch.object(Path, "symlink_to", side_effect=RuntimeError("unexpected")),
        patch.object(
            Path, "unlink", side_effect=PermissionError(errno.EACCES, "Permission denied")
        ),
        pytest.raises(RuntimeError, match="unexpected"),
    ):
        _supports_symlinks(tmp_path)


# ---------------------------------------------------------------------------
# skill_state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", list(SkillState))
def test_skill_state_classifies_each_state(skills: Path, state: SkillState) -> None:
    """Every state, since ``make`` fails for one it cannot build."""
    path = skills / "runner-status"
    make(state, path)
    assert skill_state(path) is state


def test_skill_state_missing_without_a_skills_directory(home_tmp: Path) -> None:
    assert skill_state(home_tmp / ".claude" / "skills" / "runner-status") is SkillState.MISSING
    assert not (home_tmp / ".claude").exists()


def test_skill_state_missing_when_the_skills_directory_is_a_file(home_tmp: Path) -> None:
    (home_tmp / ".claude").mkdir()
    (home_tmp / ".claude" / "skills").write_text("", encoding="utf-8")
    assert skill_state(home_tmp / ".claude" / "skills" / "runner-status") is SkillState.MISSING


def test_skill_state_dangling_for_a_symlink_loop(skills: Path) -> None:
    path = skills / "runner-status"
    path.symlink_to(skills / "loop")
    (skills / "loop").symlink_to(path)
    assert skill_state(path) is SkillState.DANGLING


def _file_in_place(path: Path) -> None:
    path.write_text("not a directory\n", encoding="utf-8")


def _link_to_a_directory_without_skill_md(path: Path) -> None:
    (path.parent / "elsewhere").mkdir()
    path.symlink_to(path.parent / "elsewhere")


def _skill_md_is_a_directory(path: Path) -> None:
    (path / "SKILL.md").mkdir(parents=True)


@pytest.mark.parametrize(
    "build",
    [_file_in_place, _link_to_a_directory_without_skill_md, _skill_md_is_a_directory],
)
def test_skill_state_incomplete_without_a_skill_md_file(skills: Path, build) -> None:
    path = skills / "runner-status"
    build(path)
    assert skill_state(path) is SkillState.INCOMPLETE


@pytest.mark.parametrize("below", ["skills", "the skill"])
def test_skill_state_raises_when_it_cannot_check(
    skills: Path, monkeypatch: pytest.MonkeyPatch, below: str
) -> None:
    """A permission error is raised, not taken to mean nothing is there."""
    path = skills / "runner-status"
    make(SkillState.COPIED, path)
    locked, denied = (skills, path) if below == "skills" else (path, path / "SKILL.md")
    unsearchable(monkeypatch, locked)
    with pytest.raises(PermissionError) as excinfo:
        skill_state(path)
    assert excinfo.value.errno == errno.EACCES
    assert excinfo.value.filename == str(denied)


def test_removal_kinds_name_every_state_uninstall_can_find() -> None:
    assert set(_REMOVAL_KINDS) == set(SkillState) - {SkillState.MISSING}


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


def test_install_one_skips_existing_no_overwrite(skills: Path) -> None:
    dst = skills / "runner-status"
    make(SkillState.COPIED, dst)
    installed, detail = _install_one(
        "runner-status",
        target_dir=skills,
        use_symlinks=True,
        overwrite=False,
    )
    assert installed is False
    assert detail == f"already present at {dst}"


def test_install_one_refuses_an_incomplete_entry_without_overwrite(skills: Path) -> None:
    """Something without a SKILL.md is no install to skip, and may hold files
    someone wants, so it takes --overwrite to replace it."""
    dst = skills / "runner-status"
    make(SkillState.INCOMPLETE, dst)
    with pytest.raises(FileExistsError) as excinfo:
        _install_one("runner-status", target_dir=skills, use_symlinks=True, overwrite=False)
    assert str(excinfo.value) == f"{dst} has no SKILL.md; rerun with --overwrite to replace it"
    assert (dst / "notes.md").read_text(encoding="utf-8") == "half a copy\n"


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
    # The legacy directory is gone, replaced by a symlink to the package.
    assert skill_state(dst) is SkillState.SYMLINKED
    assert not (dst / "old-file.md").exists()


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


@pytest.mark.parametrize("use_symlinks", [True, False], ids=["symlink", "copy"])
def test_install_one_replaces_a_dangling_symlink_without_overwrite(
    skills: Path, use_symlinks: bool
) -> None:
    """A symlink to nothing holds nothing to keep, so it is replaced."""
    dst = skills / "runner-status"
    make(SkillState.DANGLING, dst)
    src = _packaged_skill_dir("runner-status")
    installed, detail = _install_one(
        "runner-status", target_dir=skills, use_symlinks=use_symlinks, overwrite=False
    )
    assert installed is True
    how = f"symlinked → {src}" if use_symlinks else f"copied from {src}"
    assert detail == f"{how}, replacing a broken symlink to {gone(dst)}"
    assert skill_state(dst) is (SkillState.SYMLINKED if use_symlinks else SkillState.COPIED)


def _partial_copytree(error: OSError):
    """A copytree that creates ``dst``, copies part of it, then raises ``error``."""

    def copytree(src, dst, *args, **kwargs):
        Path(dst).mkdir()
        (Path(dst) / "notes.md").write_text("half a copy\n", encoding="utf-8")
        raise error

    return copytree


def test_install_one_removes_a_partial_copy(skills: Path) -> None:
    """Left in place, a partial copy would pass for an install on the next run."""
    error = OSError(errno.ENOSPC, "No space left on device")
    with (
        patch(
            "claude_task_runner.cli.install_skills_cmd.shutil.copytree", _partial_copytree(error)
        ),
        pytest.raises(OSError) as excinfo,
    ):
        _install_one("runner-status", target_dir=skills, use_symlinks=False, overwrite=False)
    assert excinfo.value is error
    assert skill_state(skills / "runner-status") is SkillState.MISSING


def test_install_one_reports_a_partial_copy_it_cannot_remove(skills: Path) -> None:
    dst = skills / "runner-status"
    error = OSError(errno.ENOSPC, "No space left on device")
    cleanup_error = PermissionError(errno.EACCES, "Permission denied", str(dst / "notes.md"))
    real_rmtree = shutil.rmtree

    def rmtree(path, *args, **kwargs):
        if Path(path) == dst:
            raise cleanup_error
        return real_rmtree(path, *args, **kwargs)

    with (
        patch(
            "claude_task_runner.cli.install_skills_cmd.shutil.copytree", _partial_copytree(error)
        ),
        patch("claude_task_runner.cli.install_skills_cmd.shutil.rmtree", rmtree),
        pytest.raises(OSError) as excinfo,
    ):
        _install_one("runner-status", target_dir=skills, use_symlinks=False, overwrite=False)
    assert str(excinfo.value) == (
        f"[Errno {errno.ENOSPC}] No space left on device; the partial copy at {dst} "
        f"could not be removed: [Errno {errno.EACCES}] Permission denied: '{dst / 'notes.md'}'"
    )
    assert excinfo.value.__cause__ is error
    # What is left has no SKILL.md, so it is not taken for an install.
    assert skill_state(dst) is SkillState.INCOMPLETE


def test_install_one_leaves_an_entry_it_did_not_create(skills: Path) -> None:
    """If something else creates the directory first, copytree's
    FileExistsError is raised, and that directory is not removed."""
    dst = skills / "runner-status"

    def copytree(src, dst, *args, **kwargs):
        Path(dst).mkdir()
        raise FileExistsError(errno.EEXIST, "File exists", str(dst))

    with (
        patch("claude_task_runner.cli.install_skills_cmd.shutil.copytree", copytree),
        pytest.raises(FileExistsError),
    ):
        _install_one("runner-status", target_dir=skills, use_symlinks=False, overwrite=False)
    assert dst.is_dir()


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


def test_install_skills_read_only_target_exits_2(
    runner: CliRunner, skills: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``~/.claude/skills`` read-only, the symlink probe fails, so the
    install falls back to copying, and every copy is refused. Each failure
    is reported and the command exits 2, not 0."""
    read_only(monkeypatch, skills)
    result = runner.invoke(app, ["--yes"])
    assert result.exit_code == 2
    for name in SKILL_NAMES:
        assert (
            f"  failed to install {name}: [Errno {errno.EACCES}] Permission denied: "
            f"'{skills / name}'\n"
        ) in result.stdout
    assert result.stdout.endswith(
        f"Failed to install {len(SKILL_NAMES)} of {len(SKILL_NAMES)} skills.\n"
    )
    assert list(skills.iterdir()) == []


def test_install_skills_cannot_create_the_skills_directory(
    runner: CliRunner, home_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only HOME without ``~/.claude`` is one clean error and exit 2,
    not a traceback."""
    read_only(monkeypatch, home_tmp)
    result = runner.invoke(app, ["--yes"])
    assert result.exit_code == 2
    assert result.stdout == (
        f"cannot create {home_tmp / '.claude' / 'skills'}: "
        f"[Errno {errno.EACCES}] Permission denied: '{home_tmp / '.claude'}'\n"
    )


def test_install_skills_unsearchable_target_exits_2(
    runner: CliRunner, skills: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skill that cannot be checked is shown so in the list the prompt
    confirms, then fails to install; it does not stop the command."""
    unsearchable(monkeypatch, skills)
    result = runner.invoke(app, ["--yes"])
    assert result.exit_code == 2
    for name in SKILL_NAMES:
        error = f"[Errno {errno.EACCES}] Permission denied: '{skills / name}'"
        assert f"  • {name}: {_packaged_skill_dir(name)} (cannot check: {error})\n" in result.stdout
        assert f"  failed to install {name}: {error}\n" in result.stdout
    assert result.stdout.endswith(
        f"Failed to install {len(SKILL_NAMES)} of {len(SKILL_NAMES)} skills.\n"
    )


def test_install_skills_replaces_a_dangling_symlink(runner: CliRunner, skills: Path) -> None:
    """A symlink into a deleted checkout used to be skipped as already
    present, so the install exited 0 and left it broken."""
    name = SKILL_NAMES[0]
    make(SkillState.DANGLING, skills / name)
    src = _packaged_skill_dir(name)
    result = runner.invoke(app, ["--yes"])
    assert result.exit_code == 0
    assert f"  • {name}: {src} (broken symlink, will be replaced)\n" in result.stdout
    assert (
        f"  installed {name}: symlinked → {src}, replacing a broken symlink to "
        f"{gone(skills / name)}\n"
    ) in result.stdout
    assert all(skill_state(skills / n) is SkillState.SYMLINKED for n in SKILL_NAMES)


def test_install_skills_incomplete_entry_needs_overwrite(runner: CliRunner, skills: Path) -> None:
    """Without --overwrite, an entry without a SKILL.md is a failure that
    names the flag, not a skip; with it, the entry is replaced."""
    name = SKILL_NAMES[0]
    dst = skills / name
    make(SkillState.INCOMPLETE, dst)
    src = _packaged_skill_dir(name)

    result = runner.invoke(app, ["--yes"])
    assert result.exit_code == 2
    assert f"  • {name}: {src} (no SKILL.md, needs --overwrite)\n" in result.stdout
    assert (
        f"  failed to install {name}: {dst} has no SKILL.md; rerun with --overwrite to replace it\n"
    ) in result.stdout
    assert result.stdout.endswith(f"Failed to install 1 of {len(SKILL_NAMES)} skills.\n")
    assert skill_state(dst) is SkillState.INCOMPLETE
    assert all(skill_state(skills / n) is SkillState.SYMLINKED for n in SKILL_NAMES[1:])

    result = runner.invoke(app, ["--yes", "--overwrite"])
    assert result.exit_code == 0
    assert f"  • {name}: {src} (no SKILL.md, will be replaced)\n" in result.stdout
    assert skill_state(dst) is SkillState.SYMLINKED


def test_install_skills_failed_copy_leaves_nothing_behind(runner: CliRunner, skills: Path) -> None:
    """The partial copy is removed, so the next run installs the skill
    instead of skipping it as already present."""
    failing = SKILL_NAMES[-1]
    real_copytree = shutil.copytree
    partial = _partial_copytree(OSError(errno.ENOSPC, "No space left on device"))

    def copytree(src, dst, *args, **kwargs):
        if Path(dst).name == failing:
            return partial(src, dst, *args, **kwargs)
        return real_copytree(src, dst, *args, **kwargs)

    with patch("claude_task_runner.cli.install_skills_cmd.shutil.copytree", copytree):
        result = runner.invoke(app, ["--yes", "--copy"])
    assert result.exit_code == 2
    assert skill_state(skills / failing) is SkillState.MISSING

    result = runner.invoke(app, ["--yes", "--copy"])
    assert result.exit_code == 0
    assert f"  installed {failing}: copied from {_packaged_skill_dir(failing)}\n" in result.stdout
    assert all(skill_state(skills / n) is SkillState.COPIED for n in SKILL_NAMES)


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------


def test_uninstall_no_skills_present(runner: CliRunner, home_tmp: Path) -> None:
    result = runner.invoke(app, ["uninstall"])
    assert result.exit_code == 0
    assert "No task-runner skills" in result.stdout


def test_uninstall_does_not_create_the_skills_directory(
    runner: CliRunner, home_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a read-only HOME is no error: there is nothing to remove."""
    read_only(monkeypatch, home_tmp)
    result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 0
    assert result.stdout == (
        f"No task-runner skills found under {home_tmp / '.claude' / 'skills'}\n"
    )
    assert not (home_tmp / ".claude").exists()


def test_uninstall_yes_removes_all(runner: CliRunner, home_tmp: Path) -> None:
    runner.invoke(app, ["--yes"])
    result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 0
    for name in SKILL_NAMES:
        assert not (home_tmp / ".claude" / "skills" / name).exists()


def test_uninstall_removes_broken_entries(runner: CliRunner, skills: Path) -> None:
    for name, state in zip(SKILL_NAMES, [SkillState.DANGLING, SkillState.INCOMPLETE], strict=False):
        make(state, skills / name)
    result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 0
    assert f"  • {SKILL_NAMES[0]} (broken symlink)\n  • {SKILL_NAMES[1]} (no SKILL.md)\n" in (
        result.stdout
    )
    assert list(skills.iterdir()) == []


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


def test_uninstall_read_only_target_exits_2(
    runner: CliRunner, home_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``~/.claude/skills`` read-only, every removal is refused. Each
    failure is reported, the skills stay, and the command exits 2, not 0."""
    runner.invoke(app, ["--yes"])
    target = home_tmp / ".claude" / "skills"
    read_only(monkeypatch, target)
    result = runner.invoke(app, ["uninstall", "--yes"])
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


def test_uninstall_unsearchable_target_exits_2(
    runner: CliRunner, skills: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skills that cannot be checked are reported, not taken for absent."""
    unsearchable(monkeypatch, skills)
    result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 2
    assert result.stdout == "".join(
        f"  cannot check {name}: [Errno {errno.EACCES}] Permission denied: '{skills / name}'\n"
        for name in SKILL_NAMES
    ) + (f"Could not check {len(SKILL_NAMES)} of {len(SKILL_NAMES)} skills.\n")


def test_uninstall_removes_the_rest_when_one_cannot_be_checked(
    runner: CliRunner, skills: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner.invoke(app, ["--yes"])
    unchecked = skills / SKILL_NAMES[0]
    unsearchable(monkeypatch, unchecked)
    result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 2
    assert result.stdout.startswith(
        f"  cannot check {SKILL_NAMES[0]}: [Errno {errno.EACCES}] Permission denied: "
        f"'{unchecked / 'SKILL.md'}'\n"
    )
    assert result.stdout.endswith(
        f"  removed {SKILL_NAMES[-1]}\nCould not check 1 of {len(SKILL_NAMES)} skills.\n"
    )
    assert [p.name for p in skills.iterdir()] == [SKILL_NAMES[0]]


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_when_none_installed(runner: CliRunner, home_tmp: Path) -> None:
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert result.stdout == "".join(f"  ✗ {name}: not installed\n" for name in SKILL_NAMES)
    assert not (home_tmp / ".claude").exists()


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


_LIST_LINES = {
    SkillState.MISSING: "  ✗ {name}: not installed",
    SkillState.SYMLINKED: "  ✓ {name}: symlinked → {src}",
    SkillState.COPIED: "  ✓ {name}: copied at {path}",
    SkillState.DANGLING: "  ✗ {name}: broken symlink → {gone}, which does not exist",
    SkillState.INCOMPLETE: "  ✗ {name}: no SKILL.md in {path}",
}


@pytest.mark.parametrize("state", list(SkillState))
def test_list_shows_each_state(runner: CliRunner, skills: Path, state: SkillState) -> None:
    """A broken symlink or an entry without a SKILL.md used to show as installed."""
    name = SKILL_NAMES[0]
    path = skills / name
    make(state, path)
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    line = _LIST_LINES[state].format(
        name=name, src=_packaged_skill_dir(name), path=path, gone=gone(path)
    )
    assert result.stdout == f"{line}\n" + "".join(
        f"  ✗ {n}: not installed\n" for n in SKILL_NAMES[1:]
    )


def test_list_does_not_create_the_skills_directory(
    runner: CliRunner, home_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``list`` used to create ``~/.claude/skills``, so a read-only HOME
    crashed it."""
    read_only(monkeypatch, home_tmp)
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert result.stdout == "".join(f"  ✗ {name}: not installed\n" for name in SKILL_NAMES)
    assert not (home_tmp / ".claude").exists()


def test_list_unsearchable_target_exits_2(
    runner: CliRunner, skills: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unsearchable(monkeypatch, skills)
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 2
    assert result.stdout == "".join(
        f"  ? {name}: cannot check: [Errno {errno.EACCES}] Permission denied: '{skills / name}'\n"
        for name in SKILL_NAMES
    ) + (f"Could not check {len(SKILL_NAMES)} of {len(SKILL_NAMES)} skills.\n")
