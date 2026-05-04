"""Tests for runner.session — resume_or_fresh planning."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_task_runner.config.schema import SessionSettings
from claude_task_runner.queue.schema import Task, TaskState
from claude_task_runner.runner.session import (
    CONTINUATION_PROMPT,
    ResumeStrategy,
    fall_through_to_fresh,
    plan_next_spawn,
    session_jsonl_exists,
)


@pytest.fixture
def task() -> Task:
    return Task(
        id="001-foo",
        title="Foo",
        prompt="Original task prompt",
        model="claude-opus-4-7",
    )


def _settings(*, max_attempts: int = 3, fail_fast: float = 5) -> SessionSettings:
    return SessionSettings(max_resume_attempts=max_attempts, resume_fail_fast_s=fail_fast)


@pytest.fixture
def projects_dir(tmp_path: Path) -> Path:
    pd = tmp_path / "projects"
    pd.mkdir()
    return pd


def _seed_session_jsonl(projects_dir: Path, session_id: str) -> Path:
    proj = projects_dir / "some-project-slug"
    proj.mkdir(exist_ok=True)
    path = proj / f"{session_id}.jsonl"
    path.write_text('{"some": "session log"}\n')
    return path


class TestSessionJsonlExists:
    def test_missing_dir(self, tmp_path: Path) -> None:
        assert session_jsonl_exists("abc", claude_projects_dir=tmp_path / "nope") is False

    def test_missing_session(self, projects_dir: Path) -> None:
        assert session_jsonl_exists("abc", claude_projects_dir=projects_dir) is False

    def test_present_in_subdir(self, projects_dir: Path) -> None:
        _seed_session_jsonl(projects_dir, "abc-123")
        assert session_jsonl_exists("abc-123", claude_projects_dir=projects_dir) is True


class TestPlanNextSpawn:
    def test_no_session_id_fresh(self, task: Task, projects_dir: Path) -> None:
        state = TaskState(task_id=task.id)
        plan = plan_next_spawn(
            task,
            state,
            settings=_settings(),
            claude_projects_dir=projects_dir,
        )
        assert plan.strategy is ResumeStrategy.FRESH
        assert plan.prompt == task.prompt
        assert plan.session_id is None

    def test_session_present_resume(self, task: Task, projects_dir: Path) -> None:
        _seed_session_jsonl(projects_dir, "sess-abc")
        state = TaskState(task_id=task.id, session_id="sess-abc")
        plan = plan_next_spawn(
            task,
            state,
            settings=_settings(),
            claude_projects_dir=projects_dir,
        )
        assert plan.strategy is ResumeStrategy.RESUME
        assert plan.prompt == CONTINUATION_PROMPT
        assert plan.session_id == "sess-abc"

    def test_resume_attempts_capped(self, task: Task, projects_dir: Path) -> None:
        _seed_session_jsonl(projects_dir, "sess-abc")
        state = TaskState(task_id=task.id, session_id="sess-abc", resume_attempts=3)
        plan = plan_next_spawn(
            task,
            state,
            settings=_settings(max_attempts=3),
            claude_projects_dir=projects_dir,
        )
        assert plan.strategy is ResumeStrategy.FRESH

    def test_session_jsonl_missing_falls_back_to_fresh(
        self, task: Task, projects_dir: Path
    ) -> None:
        # Session ID set but no jsonl on disk.
        state = TaskState(task_id=task.id, session_id="missing-session")
        plan = plan_next_spawn(
            task,
            state,
            settings=_settings(),
            claude_projects_dir=projects_dir,
        )
        assert plan.strategy is ResumeStrategy.FRESH

    def test_extra_args_passed_through(self, task: Task, projects_dir: Path) -> None:
        state = TaskState(task_id=task.id)
        plan = plan_next_spawn(
            task,
            state,
            settings=_settings(),
            claude_projects_dir=projects_dir,
            extra_args=["--debug"],
        )
        assert plan.extra_args == ["--debug"]


class TestFallThrough:
    def test_resume_becomes_fresh(self) -> None:
        plan = (
            fall_through_to_fresh.__wrapped__
            if hasattr(fall_through_to_fresh, "__wrapped__")
            else fall_through_to_fresh
        )
        # Manually construct a RESUME plan
        from claude_task_runner.runner.session import SpawnPlan

        resume = SpawnPlan(
            strategy=ResumeStrategy.RESUME,
            session_id="sess-abc",
            prompt=CONTINUATION_PROMPT,
            extra_args=["--model", "claude-opus-4-7"],
        )
        fresh = plan(resume, "ORIGINAL")
        assert fresh.strategy is ResumeStrategy.FRESH
        assert fresh.session_id is None
        assert fresh.prompt == "ORIGINAL"
        assert fresh.extra_args == ["--model", "claude-opus-4-7"]

    def test_fresh_passes_through(self) -> None:
        from claude_task_runner.runner.session import SpawnPlan

        original = SpawnPlan(
            strategy=ResumeStrategy.FRESH,
            session_id=None,
            prompt="P",
            extra_args=[],
        )
        result = fall_through_to_fresh(original, "different")
        assert result is original
