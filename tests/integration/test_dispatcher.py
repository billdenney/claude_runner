"""Integration tests for runner.dispatcher using the fake claude shim.

The shim at ``tests/fixtures/claude_shim/claude`` masquerades as the
claude binary; tests parameterize its behavior via env vars so each
scenario exercises a different code path without modifying the shim
itself.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from claude_task_runner.clock import RealClock
from claude_task_runner.config.schema import (
    DispatchSettings,
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
        # Autonomous dispatch must accept edits up-front; the default
        # permission policy blocks Write/Edit/Bash-redirect with
        # "permissions not yet granted" prompts that ``--print`` cannot
        # honour. See the comment in build_argv() for the observed
        # symptom (task 130-lowe_2009_omalizumab on 2026-05-21).
        assert "--permission-mode" in argv
        idx = argv.index("--permission-mode")
        assert argv[idx + 1] == "acceptEdits"
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

    def test_settings_json_grants_bash_git_rscript(self, task: Task, fresh_plan: SpawnPlan) -> None:
        """The autonomous dispatch must be allowed to run ``git`` and
        ``Rscript`` from the start. Without ``permissions.allow`` for
        those Bash patterns, ``acceptEdits`` would only auto-grant
        Write/Edit and the agent would exit via ``end_turn`` with the
        model on disk but never committed (observed 2026-05-21 with
        task ``130-lowe_2009_omalizumab``). The runner always emits a
        ``--settings`` JSON document with the additive allow list;
        Claude Code merges it with any project-local
        ``settings.local.json`` so per-repo configs are not clobbered.
        """
        import json

        argv = build_argv(task, fresh_plan, claude_executable="claude")
        assert "--settings" in argv
        idx = argv.index("--settings")
        settings_arg = argv[idx + 1]
        # Settings arg is a JSON string (single-arg shell-friendly form).
        payload = json.loads(settings_arg)
        allow = payload["permissions"]["allow"]
        # The four patterns are the minimum needed for Phase 4
        # (convention check), Phase 5 (vignette render), and Phase 6
        # (registry regen + git commit + push) of /extract-literature-model.
        assert "Bash(git *)" in allow
        assert "Bash(Rscript *)" in allow
        assert "Bash(R *)" in allow
        assert "Bash(make *)" in allow

    def test_no_add_dirs_omits_flag(self, task: Task, fresh_plan: SpawnPlan) -> None:
        argv = build_argv(task, fresh_plan, claude_executable="claude")
        assert "--add-dir" not in argv

    def test_add_dirs_emit_one_flag_per_path(self, task: Task, fresh_plan: SpawnPlan) -> None:
        dirs = [Path("/a/queue"), Path("/b/shared"), Path("/c/data")]
        argv = build_argv(
            task,
            fresh_plan,
            claude_executable="claude",
            add_dirs=dirs,
        )
        # One `--add-dir <path>` per resolved entry; appears before `--`.
        assert argv.count("--add-dir") == 3
        idx0 = argv.index("--add-dir")
        # The three flags should be contiguous (--add-dir, /a/queue, --add-dir, /b/shared, ...).
        assert argv[idx0 : idx0 + 6] == [
            "--add-dir",
            "/a/queue",
            "--add-dir",
            "/b/shared",
            "--add-dir",
            "/c/data",
        ]
        # `--` separator must still come after all flags.
        assert "--" in argv
        assert argv.index("--") > idx0 + 5
        assert argv[-1] == task.prompt


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

    def test_dispatch_logs_resolved_add_dirs(
        self,
        queue_dir: Path,
        task: Task,
        fresh_plan: SpawnPlan,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        reset_shim_env: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The operator-facing dispatch log must surface the resolved
        --add-dir scope so they can verify what the spawned agent saw.
        Without this, debugging a "sandbox blocked my read" only has
        the per-task YAML to read; the runtime resolution is hidden.
        """
        extra = tmp_path / "shared"
        extra.mkdir()
        task_with_extra = task.model_copy(update={"additional_dirs": [extra]})

        import logging as _logging

        with caplog.at_level(_logging.INFO, logger="claude_task_runner.runner.dispatcher"):
            dispatch(
                task=task_with_extra,
                state=TaskState(task_id=task_with_extra.id),
                plan=fresh_plan,
                queue_dir=queue_dir,
                clock=RealClock(),
                settings_caps=_caps(),
                settings_session=_session(),
                settings_hooks=_hooks(),
                settings_dispatch=DispatchSettings(),
                claude_executable=str(SHIM_PATH),
            )

        dispatch_lines = [
            rec.getMessage() for rec in caplog.records if "[dispatch]" in rec.getMessage()
        ]
        assert dispatch_lines, "expected at least one [dispatch] log line"
        line = dispatch_lines[0]
        # Queue dir is always-on; per-task extra is included.
        assert str(queue_dir.resolve()) in line
        assert str(extra.resolve()) in line
        assert f"task_id={task_with_extra.id}" in line

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

    def test_dispatcher_alive_at_lands_in_state(
        self,
        queue_dir: Path,
        task: Task,
        fresh_plan: SpawnPlan,
        monkeypatch: pytest.MonkeyPatch,
        reset_shim_env: None,
    ) -> None:
        """The monitor thread writes ``dispatcher_alive_at`` on the
        running-state YAML so the supervisor's per-tick reaper can use
        it as the cheap Layer-2 liveness signal. Even a fast shim run
        should land the initial-write timestamp; without it the per-
        tick reaper would have to fall back to the heartbeat-only
        classifier for every healthy task."""
        monkeypatch.setenv("SHIM_SESSION_ID", "sess-alive-it")
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

        # The dispatcher_alive_at field is set by the monitor thread's
        # initial-write on start; it survives the finalize because
        # _finalize_state preserves fields not in its update set.
        assert outcome.new_state.dispatcher_alive_at is not None
        # Also persisted to disk (the running-state write under the
        # monitor lock was the last one before completion).
        loaded = load_state(state_path_for(queue_dir, task.id))
        assert loaded.dispatcher_alive_at is not None


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


class TestOutputEvidenceGate:
    """End-to-end checks for the ADR-0020 output-evidence gate when
    ``dispatch()`` runs with a real ``working_dir`` against the shim."""

    @staticmethod
    def _git(cwd: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)

    def _init_repo(self, repo: Path) -> None:
        repo.mkdir()
        self._git(repo, "init", "-q", "-b", "main")
        self._git(repo, "config", "user.email", "test@example.invalid")
        self._git(repo, "config", "user.name", "Test")
        (repo / "README.md").write_text("seed\n")
        self._git(repo, "add", ".")
        self._git(repo, "commit", "-q", "-m", "seed")

    def test_clean_exit_no_artifact_flips_to_failed(
        self,
        queue_dir: Path,
        tmp_path: Path,
        reset_shim_env: None,
    ) -> None:
        repo = tmp_path / "wt"
        self._init_repo(repo)
        worktree_task = Task(
            id="999-no-output",
            title="No output",
            prompt="Do nothing",
            working_dir=repo,
        )
        plan = SpawnPlan(
            strategy=ResumeStrategy.FRESH,
            session_id=None,
            prompt=worktree_task.prompt,
            extra_args=[],
        )
        outcome = dispatch(
            task=worktree_task,
            state=TaskState(task_id=worktree_task.id),
            plan=plan,
            queue_dir=queue_dir,
            clock=RealClock(),
            settings_caps=_caps(),
            settings_session=_session(),
            settings_hooks=_hooks(),
            claude_executable=str(SHIM_PATH),
        )
        assert outcome.new_state.status == "failed"
        assert outcome.run_record.stop_reason == "end_turn_no_output"
        assert outcome.run_record.error is not None
        assert "no new commit" in outcome.run_record.error

    def test_declared_deliverable_satisfies_gate(
        self,
        queue_dir: Path,
        tmp_path: Path,
        reset_shim_env: None,
    ) -> None:
        repo = tmp_path / "wt"
        self._init_repo(repo)
        # Pre-existing deliverable. The shim produces no commit but the
        # declared path exists ⇒ gate passes.
        (repo / "out.R").write_text("# stub\n")
        worktree_task = Task(
            id="998-has-deliverable",
            title="Has deliverable",
            prompt="Do nothing",
            working_dir=repo,
            deliverable_paths=[Path("out.R")],
        )
        plan = SpawnPlan(
            strategy=ResumeStrategy.FRESH,
            session_id=None,
            prompt=worktree_task.prompt,
            extra_args=[],
        )
        outcome = dispatch(
            task=worktree_task,
            state=TaskState(task_id=worktree_task.id),
            plan=plan,
            queue_dir=queue_dir,
            clock=RealClock(),
            settings_caps=_caps(),
            settings_session=_session(),
            settings_hooks=_hooks(),
            claude_executable=str(SHIM_PATH),
        )
        assert outcome.new_state.status == "completed"
        assert outcome.run_record.stop_reason == "end_turn"
