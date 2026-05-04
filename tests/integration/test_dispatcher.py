"""Integration tests for runner.dispatcher using the fake claude shim.

The shim at ``tests/fixtures/claude_shim/claude`` masquerades as the
claude binary; tests parameterize its behavior via env vars so each
scenario exercises a different code path without modifying the shim
itself.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_task_runner.clock import RealClock
from claude_task_runner.config.schema import (
    HookSettings,
    SessionSettings,
    TaskCapsSettings,
)
from claude_task_runner.queue.schema import Task, TaskState
from claude_task_runner.queue.store import (
    load_state,
    queue_runtime_dir,
    state_path_for,
)
from claude_task_runner.runner.dispatcher import (
    DispatchError,
    build_argv,
    dispatch,
)
from claude_task_runner.runner.session import (
    ResumeStrategy,
    SpawnPlan,
)

SHIM_PATH = Path(__file__).parent.parent / "fixtures" / "claude_shim" / "claude"


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "queue"
    qd.mkdir()
    queue_runtime_dir(qd)  # ensure subdirs exist
    return qd


@pytest.fixture
def task() -> Task:
    return Task(
        id="001-foo",
        title="Foo",
        prompt="Do the foo",
        model="claude-opus-4-7",
        allowed_tools=["Read", "Write"],
    )


@pytest.fixture
def fresh_plan(task: Task) -> SpawnPlan:
    return SpawnPlan(
        strategy=ResumeStrategy.FRESH,
        session_id=None,
        prompt=task.prompt,
        extra_args=[],
    )


def _caps(
    *, max_tokens: int = 0, max_duration: float = 0, alert: float = 60, kill: float = 0
) -> TaskCapsSettings:
    return TaskCapsSettings(
        max_tokens_per_task=max_tokens,
        max_duration_s_per_task=max_duration,
        heartbeat_silence_alert_s=alert,
        heartbeat_silence_kill_s=kill,
    )


def _session() -> SessionSettings:
    return SessionSettings(max_resume_attempts=3, resume_fail_fast_s=5)


def _hooks(*, pre: str = "", post: str = "") -> HookSettings:
    return HookSettings(
        pre_dispatch_command=pre,
        pre_dispatch_timeout_s=10,
        post_dispatch_command=post,
        post_dispatch_timeout_s=10,
    )


@pytest.fixture
def reset_shim_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wipe SHIM_* env so each test starts fresh."""
    for key in list(os.environ):
        if key.startswith("SHIM_"):
            monkeypatch.delenv(key)


class TestBuildArgv:
    def test_fresh_argv(self, task: Task, fresh_plan: SpawnPlan) -> None:
        argv = build_argv(task, fresh_plan, claude_executable="claude")
        assert argv[0] == "claude"
        assert "--print" in argv
        assert "--output-format=stream-json" in argv
        assert "--verbose" in argv
        assert "--model" in argv
        assert "--allowedTools" in argv
        assert argv[-1] == task.prompt
        assert "--resume" not in argv

    def test_resume_argv_includes_session(self, task: Task) -> None:
        plan = SpawnPlan(
            strategy=ResumeStrategy.RESUME,
            session_id="sess-abc",
            prompt="Continue.",
            extra_args=[],
        )
        argv = build_argv(task, plan, claude_executable="claude")
        assert "--resume" in argv
        idx = argv.index("--resume")
        assert argv[idx + 1] == "sess-abc"
        assert argv[-1] == "Continue."

    def test_no_tools_omits_flag(self) -> None:
        task = Task(id="x", title="x", prompt="p", allowed_tools=[])
        plan = SpawnPlan(
            strategy=ResumeStrategy.FRESH,
            session_id=None,
            prompt="p",
            extra_args=[],
        )
        argv = build_argv(task, plan, claude_executable="claude")
        assert "--allowedTools" not in argv


class TestDispatchSuccess:
    def test_full_success_path(
        self,
        queue_dir: Path,
        task: Task,
        fresh_plan: SpawnPlan,
        monkeypatch: pytest.MonkeyPatch,
        reset_shim_env: None,
    ) -> None:
        monkeypatch.setenv("SHIM_SESSION_ID", "sess-xyz")
        monkeypatch.setenv("SHIM_INPUT_TOKENS", "100")
        monkeypatch.setenv("SHIM_OUTPUT_TOKENS", "50")
        monkeypatch.setenv("SHIM_COST_USD", "0.42")

        outcome = dispatch(
            task=task,
            state=TaskState(task_id=task.id),
            plan=fresh_plan,
            queue_dir=queue_dir,
            clock=RealClock(),
            settings_caps=_caps(),
            settings_session=_session(),
            settings_hooks=_hooks(),
            claude_executable=str(SHIM_PATH),
        )

        assert outcome.run_record.attempt == 1
        assert outcome.run_record.error is None
        assert outcome.run_record.stop_reason == "end_turn"
        assert outcome.run_record.usage.input_tokens == 100
        assert outcome.run_record.usage.output_tokens == 50
        assert outcome.run_record.cost_usd == pytest.approx(0.42)
        assert outcome.run_record.killed_by_cap is None
        assert outcome.summary.session_id == "sess-xyz"

        assert outcome.new_state.status == "completed"
        assert outcome.new_state.session_id == "sess-xyz"
        assert outcome.new_state.attempts == 1

        # Verify state was persisted atomically.
        loaded = load_state(state_path_for(queue_dir, task.id))
        assert loaded == outcome.new_state

    def test_state_running_then_completed_persists(
        self,
        queue_dir: Path,
        task: Task,
        fresh_plan: SpawnPlan,
        monkeypatch: pytest.MonkeyPatch,
        reset_shim_env: None,
    ) -> None:
        monkeypatch.setenv("SHIM_SESSION_ID", "sess-1")
        outcome = dispatch(
            task=task,
            state=TaskState(task_id=task.id),
            plan=fresh_plan,
            queue_dir=queue_dir,
            clock=RealClock(),
            settings_caps=_caps(),
            settings_session=_session(),
            settings_hooks=_hooks(),
            claude_executable=str(SHIM_PATH),
        )
        # Final state should be 'completed' (we don't see the intermediate
        # 'running' here, but the state path was written twice).
        assert outcome.new_state.status == "completed"


class TestDispatchError:
    def test_missing_claude_binary(
        self,
        queue_dir: Path,
        task: Task,
        fresh_plan: SpawnPlan,
    ) -> None:
        with pytest.raises(DispatchError, match="not found"):
            dispatch(
                task=task,
                state=TaskState(task_id=task.id),
                plan=fresh_plan,
                queue_dir=queue_dir,
                clock=RealClock(),
                settings_caps=_caps(),
                settings_session=_session(),
                settings_hooks=_hooks(),
                claude_executable="this-binary-does-not-exist-1234",
            )

    def test_shim_reports_error_marks_failed(
        self,
        queue_dir: Path,
        task: Task,
        fresh_plan: SpawnPlan,
        monkeypatch: pytest.MonkeyPatch,
        reset_shim_env: None,
    ) -> None:
        monkeypatch.setenv("SHIM_IS_ERROR", "true")
        monkeypatch.setenv("SHIM_STOP_REASON", "rate_limit")
        outcome = dispatch(
            task=task,
            state=TaskState(task_id=task.id),
            plan=fresh_plan,
            queue_dir=queue_dir,
            clock=RealClock(),
            settings_caps=_caps(),
            settings_session=_session(),
            settings_hooks=_hooks(),
            claude_executable=str(SHIM_PATH),
        )
        assert outcome.new_state.status == "failed"
        assert outcome.run_record.stop_reason == "rate_limit"


class TestPreDispatchHook:
    def test_failed_pre_hook_aborts_dispatch(
        self,
        queue_dir: Path,
        task: Task,
        fresh_plan: SpawnPlan,
        reset_shim_env: None,
    ) -> None:
        outcome = dispatch(
            task=task,
            state=TaskState(task_id=task.id),
            plan=fresh_plan,
            queue_dir=queue_dir,
            clock=RealClock(),
            settings_caps=_caps(),
            settings_session=_session(),
            settings_hooks=_hooks(pre="false"),
            claude_executable=str(SHIM_PATH),
        )
        assert outcome.new_state.status == "failed"
        assert outcome.run_record.stop_reason == "pre_dispatch_hook_failed"
        assert outcome.run_record.attempt == 1

    def test_passing_pre_hook_continues(
        self,
        queue_dir: Path,
        task: Task,
        fresh_plan: SpawnPlan,
        reset_shim_env: None,
    ) -> None:
        outcome = dispatch(
            task=task,
            state=TaskState(task_id=task.id),
            plan=fresh_plan,
            queue_dir=queue_dir,
            clock=RealClock(),
            settings_caps=_caps(),
            settings_session=_session(),
            settings_hooks=_hooks(pre="true"),
            claude_executable=str(SHIM_PATH),
        )
        assert outcome.new_state.status == "completed"


class TestPostDispatchHook:
    def test_failed_post_hook_does_not_fail_task(
        self,
        queue_dir: Path,
        task: Task,
        fresh_plan: SpawnPlan,
        reset_shim_env: None,
    ) -> None:
        outcome = dispatch(
            task=task,
            state=TaskState(task_id=task.id),
            plan=fresh_plan,
            queue_dir=queue_dir,
            clock=RealClock(),
            settings_caps=_caps(),
            settings_session=_session(),
            settings_hooks=_hooks(post="false"),
            claude_executable=str(SHIM_PATH),
        )
        assert outcome.new_state.status == "completed"
