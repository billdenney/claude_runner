"""Tests for claude_runner.runner.preamble."""

from __future__ import annotations

from pathlib import Path

from claude_runner.config import Settings
from claude_runner.defaults import Effort
from claude_runner.runner.preamble import build_preamble, should_inject
from claude_runner.todo.schema import GitWorktreeSpec, TaskSpec


def _spec(
    *,
    task_id: str = "t1",
    prompt: str = "do the thing",
    working_dir: Path | None = None,
    git_worktree: GitWorktreeSpec | None = None,
    inject_preamble: bool | None = None,
) -> TaskSpec:
    return TaskSpec(
        id=task_id,
        title="t",
        prompt=prompt,
        working_dir=working_dir or Path("/tmp"),
        allowed_tools=("Read",),
        model="claude-3-5-sonnet-20241022",
        effort=Effort.MEDIUM,
        max_turns=10,
        estimated_input_tokens=1000,
        git_worktree=git_worktree,
        inject_preamble=inject_preamble,
    )


def test_build_preamble_includes_task_id_and_sidecar_path() -> None:
    spec = _spec(task_id="my-task")
    text = build_preamble(
        spec=spec,
        sidecar_dir=Path("/tmp/sidecar/my-task"),
        worktree_path=None,
    )
    assert "my-task" in text
    assert "/tmp/sidecar/my-task" in text
    assert "sidecar stop-and-ask" in text.lower()
    assert "gh pr create" in text  # gh read-only section present
    # Must end with a separator and "# Task prompt" header.
    assert "# Task prompt" in text


def test_build_preamble_omits_sidecar_section_when_no_sidecar_dir() -> None:
    spec = _spec()
    text = build_preamble(spec=spec, sidecar_dir=None, worktree_path=None)
    assert "CLAUDE_RUNNER_SIDECAR_DIR=(unset)" in text
    # The detailed sidecar section is keyed off sidecar_dir being non-None.
    assert "request-NNN.json" not in text


def test_build_preamble_includes_worktree_section_when_git_worktree_present(
    tmp_path: Path,
) -> None:
    gw = GitWorktreeSpec(repo=tmp_path / "repo", branch_name="feat-x")
    spec = _spec(git_worktree=gw)
    text = build_preamble(
        spec=spec,
        sidecar_dir=tmp_path / "sc",
        worktree_path=tmp_path / "worktrees" / "t1",
    )
    assert "Git worktree already set up" in text
    assert "feat-x" in text
    assert str(tmp_path / "worktrees" / "t1") in text


def test_build_preamble_omits_worktree_section_when_no_git_worktree(
    tmp_path: Path,
) -> None:
    spec = _spec()
    text = build_preamble(spec=spec, sidecar_dir=tmp_path, worktree_path=None)
    assert "Git worktree already set up" not in text


def test_should_inject_task_override_wins_true() -> None:
    settings = Settings(budget_source="static", inject_preamble=False)
    spec = _spec(inject_preamble=True)
    assert should_inject(spec=spec, settings_inject=settings.inject_preamble) is True


def test_should_inject_task_override_wins_false() -> None:
    settings = Settings(budget_source="static", inject_preamble=True)
    spec = _spec(inject_preamble=False)
    assert should_inject(spec=spec, settings_inject=settings.inject_preamble) is False


def test_should_inject_falls_back_to_settings_when_task_null() -> None:
    settings = Settings(budget_source="static", inject_preamble=True)
    spec = _spec(inject_preamble=None)
    assert should_inject(spec=spec, settings_inject=settings.inject_preamble) is True
    settings2 = Settings(budget_source="static", inject_preamble=False)
    assert should_inject(spec=spec, settings_inject=settings2.inject_preamble) is False
