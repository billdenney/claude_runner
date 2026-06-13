"""Integration tests for the file-backed worker path (ADR-0025).

When ``adopt_workers=True``, ``dispatch()`` redirects the worker's
stdout/stderr to per-attempt files under
``<queue>/.claude_task_runner/logs/<task_id>/`` and tails the stdout file
instead of reading a pipe. The finalized ``DispatchOutcome`` must be
equivalent to the legacy pipe path for the same worker, and the stdout
log path must be recorded on ``TaskState.log_path`` mid-run so a fresh
supervisor can adopt the worker after a restart.

These drive the real ``dispatch()`` against the bundled fake ``claude``
shim, once with ``adopt_workers=False`` (pipe) and once True (files),
and assert the outcomes match where it matters and that the on-disk log
artifacts exist.
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
from claude_task_runner.runner.dispatcher import dispatch
from claude_task_runner.runner.session import ResumeStrategy, SpawnPlan

SHIM_PATH = Path(__file__).parent.parent / "fixtures" / "claude_shim" / "claude"


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "queue"
    qd.mkdir()
    queue_runtime_dir(qd)
    return qd


@pytest.fixture
def task() -> Task:
    # No working_dir ⇒ the output-evidence gate is skipped, so a clean
    # end_turn is honoured as completed (keeps the equivalence focused on
    # the file-vs-pipe plumbing, not the ADR-0020 gate).
    return Task(id="001-fb", title="Foo", prompt="Do the foo", working_dir=None)


@pytest.fixture
def fresh_plan(task: Task) -> SpawnPlan:
    return SpawnPlan(
        strategy=ResumeStrategy.FRESH, session_id=None, prompt=task.prompt, extra_args=[]
    )


def _caps() -> TaskCapsSettings:
    return TaskCapsSettings(
        max_tokens_per_task=0,
        max_duration_s_per_task=0,
        heartbeat_silence_alert_s=600,
        heartbeat_silence_kill_s=0,
    )


def _session() -> SessionSettings:
    return SessionSettings(max_resume_attempts=3, resume_fail_fast_s=5)


def _hooks() -> HookSettings:
    return HookSettings(
        pre_dispatch_command="",
        pre_dispatch_timeout_s=10,
        post_dispatch_command="",
        post_dispatch_timeout_s=10,
    )


@pytest.fixture
def reset_shim_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("SHIM_"):
            monkeypatch.delenv(key)


def _dispatch(queue_dir: Path, task: Task, plan: SpawnPlan, *, adopt: bool) -> object:
    return dispatch(
        task=task,
        state=TaskState(task_id=task.id),
        plan=plan,
        queue_dir=queue_dir,
        clock=RealClock(),
        settings_caps=_caps(),
        settings_session=_session(),
        settings_hooks=_hooks(),
        claude_executable=str(SHIM_PATH),
        adopt_workers=adopt,
    )


def test_file_backed_success_matches_pipe(
    queue_dir: Path,
    task: Task,
    fresh_plan: SpawnPlan,
    monkeypatch: pytest.MonkeyPatch,
    reset_shim_env: None,
) -> None:
    """The file-backed finalize is equivalent to the pipe finalize for the
    same shim run: same status, stop_reason, usage, cost, session id."""
    monkeypatch.setenv("SHIM_SESSION_ID", "sess-fb")
    monkeypatch.setenv("SHIM_INPUT_TOKENS", "100")
    monkeypatch.setenv("SHIM_OUTPUT_TOKENS", "50")
    monkeypatch.setenv("SHIM_COST_USD", "0.42")

    pipe = _dispatch(queue_dir, task, fresh_plan, adopt=False)

    # Fresh queue dir for the file-backed run so the second dispatch
    # starts from a clean state (attempt counts, etc.).
    files = _dispatch(queue_dir, task, fresh_plan, adopt=True)

    assert pipe.new_state.status == files.new_state.status == "completed"  # type: ignore[attr-defined]
    assert pipe.run_record.stop_reason == files.run_record.stop_reason == "end_turn"  # type: ignore[attr-defined]
    assert files.run_record.usage.input_tokens == 100  # type: ignore[attr-defined]
    assert files.run_record.usage.output_tokens == 50  # type: ignore[attr-defined]
    assert files.run_record.cost_usd == pytest.approx(0.42)  # type: ignore[attr-defined]
    assert files.summary.session_id == "sess-fb"  # type: ignore[attr-defined]


def test_file_backed_writes_log_files(
    queue_dir: Path,
    task: Task,
    fresh_plan: SpawnPlan,
    monkeypatch: pytest.MonkeyPatch,
    reset_shim_env: None,
) -> None:
    """The worker's stdout/stderr land in the per-attempt log files, and
    the stdout log contains the parseable stream the adopter will re-tail."""
    monkeypatch.setenv("SHIM_SESSION_ID", "sess-logged")

    _dispatch(queue_dir, task, fresh_plan, adopt=True)

    logs_dir = queue_dir / ".claude_task_runner" / "logs" / task.id
    stdout_log = logs_dir / "attempt-1.stream.jsonl"
    stderr_log = logs_dir / "attempt-1.stderr"
    assert stdout_log.exists()
    assert stderr_log.exists()
    # The stdout log holds the NDJSON stream (the session-init line at least).
    body = stdout_log.read_text()
    assert "sess-logged" in body
    assert '"type"' in body


def test_file_backed_records_log_path_during_run(
    queue_dir: Path,
    task: Task,
    fresh_plan: SpawnPlan,
    monkeypatch: pytest.MonkeyPatch,
    reset_shim_env: None,
) -> None:
    """``TaskState.log_path`` is written alongside the pid right after
    Popen (so a restart mid-run finds the stream) and cleared on
    finalize. We capture the running-state write via a spy on
    ``write_state_atomic`` to assert the mid-run value, then assert the
    final state has it cleared."""
    import claude_task_runner.runner.dispatcher as dmod

    monkeypatch.setenv("SHIM_SESSION_ID", "sess-lp")

    seen_log_paths: list[str | None] = []
    real_write = dmod.write_state_atomic

    def _spy(state: object, path: object) -> None:
        if getattr(state, "status", None) == "running":
            seen_log_paths.append(getattr(state, "log_path", None))
        real_write(state, path)  # type: ignore[arg-type]

    monkeypatch.setattr(dmod, "write_state_atomic", _spy)

    outcome = _dispatch(queue_dir, task, fresh_plan, adopt=True)

    # At least one running-state write carried a concrete log_path.
    concrete = [p for p in seen_log_paths if p is not None]
    assert concrete, f"expected a running-state log_path write, saw {seen_log_paths}"
    expected = str(queue_dir / ".claude_task_runner" / "logs" / task.id / "attempt-1.stream.jsonl")
    assert concrete[0] == expected

    # Finalized state clears log_path (next attempt records a fresh one).
    assert outcome.new_state.log_path is None  # type: ignore[attr-defined]
    reloaded = load_state(state_path_for(queue_dir, task.id))
    assert reloaded.log_path is None


def test_pipe_path_records_no_log_path(
    queue_dir: Path,
    task: Task,
    fresh_plan: SpawnPlan,
    monkeypatch: pytest.MonkeyPatch,
    reset_shim_env: None,
) -> None:
    """With adoption OFF, no log file is created and log_path stays None —
    the legacy pipe behaviour is unchanged."""
    monkeypatch.setenv("SHIM_SESSION_ID", "sess-pipe")

    outcome = _dispatch(queue_dir, task, fresh_plan, adopt=False)

    assert outcome.new_state.log_path is None  # type: ignore[attr-defined]
    logs_dir = queue_dir / ".claude_task_runner" / "logs" / task.id
    # No per-task log directory created on the pipe path.
    assert not logs_dir.exists()


def test_file_backed_error_path_marks_failed(
    queue_dir: Path,
    task: Task,
    fresh_plan: SpawnPlan,
    monkeypatch: pytest.MonkeyPatch,
    reset_shim_env: None,
) -> None:
    """A shim that emits an error result over the file-backed path is
    classified failed, with the error stop_reason — same as the pipe
    path, and the stderr tail is read from the stderr file."""
    monkeypatch.setenv("SHIM_IS_ERROR", "true")
    monkeypatch.setenv("SHIM_STOP_REASON", "rate_limit")

    outcome = _dispatch(queue_dir, task, fresh_plan, adopt=True)
    assert outcome.new_state.status == "failed"  # type: ignore[attr-defined]
    assert outcome.run_record.stop_reason == "rate_limit"  # type: ignore[attr-defined]
