"""Put a skill's directory in ``~/.claude/skills/`` into a given state.

``install_skills_cmd.skill_state`` classifies what is at a skill's path;
:func:`make` builds each :class:`SkillState`, so a test can put any of
them in place.
"""

from __future__ import annotations

from pathlib import Path

from claude_task_runner.cli.install_skills_cmd import SkillState, _packaged_skill_dir


def gone(path: Path) -> Path:
    """Where a broken symlink at ``path`` points: a checkout since deleted."""
    return path.parent.parent / "deleted-checkout" / path.name


def make(state: SkillState, path: Path) -> None:
    """Put ``state`` at ``path``, a skill's directory whose parent exists.

    ``path.name`` must be a packaged skill for SYMLINKED.
    """
    if state is SkillState.SYMLINKED:
        path.symlink_to(_packaged_skill_dir(path.name))
    elif state is SkillState.COPIED:
        path.mkdir()
        (path / "SKILL.md").write_text(f"# {path.name}\n", encoding="utf-8")
    elif state is SkillState.DANGLING:
        path.symlink_to(gone(path))
    elif state is SkillState.INCOMPLETE:
        path.mkdir()
        (path / "notes.md").write_text("half a copy\n", encoding="utf-8")
    elif state is not SkillState.MISSING:
        raise AssertionError(f"make() has no case for {state!r}")
