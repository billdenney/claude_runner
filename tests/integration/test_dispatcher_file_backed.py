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
artifacts exist. The log an owned run leaves is also finalized a second
time as a worker that exited with no supervisor watching, which must
reach the owned run's verdict.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from claude_task_runner.clock import RealClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.config.schema import (
    HookSettings,
    TaskCapsSettings,
)
from claude_task_runner.queue.schema import Task, TaskState
from claude_task_runner.queue.store import (
    load_state,
    queue_runtime_dir,
    state_path_for,
    write_state_atomic,
)
from claude_task_runner.runner import dispatcher as dispatcher_mod
from claude_task_runner.runner.dispatcher import (
    DispatchOutcome,
    dispatch,
    finalize_exited_worker,
)
from claude_task_runner.runner.session import ResumeStrategy, SpawnPlan

SHIM_PATH = Path(__file__).parent.parent / "fixtures" / "claude_shim" / "claude"

_SETTINGS = load_settings(None)
"""Package defaults; the adopted finalize requires the dispatch and hook settings."""


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


def _dispatch(
    queue_dir: Path,
    task: Task,
    plan: SpawnPlan,
    *,
    adopt: bool,
    on_park: Callable[[TaskState, str], None] | None = None,
) -> object:
    return dispatch(
        task=task,
        state=TaskState(task_id=task.id),
        plan=plan,
        queue_dir=queue_dir,
        clock=RealClock(),
        settings_caps=_caps(),
        settings_hooks=_hooks(),
        claude_executable=str(SHIM_PATH),
        adopt_workers=adopt,
        on_park=on_park,
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


@pytest.mark.parametrize(
    ("stop_reason", "is_error", "status"),
    [
        pytest.param("end_turn", "false", "completed", id="success"),
        pytest.param("error_during_execution", "true", "failed", id="error-result"),
    ],
)
def test_exited_worker_finalize_matches_owned_finalize(
    queue_dir: Path,
    task: Task,
    fresh_plan: SpawnPlan,
    monkeypatch: pytest.MonkeyPatch,
    reset_shim_env: None,
    stop_reason: str,
    is_error: str,
    status: str,
) -> None:
    """A worker that exits with no supervisor watching is classified as the
    owned path classifies the same worker. The owned run leaves its log on
    disk; the test re-seeds the task as ``running`` on that log, as a
    supervisor restart would have left it, and finalizes it again as an
    exited worker."""
    monkeypatch.setenv("SHIM_SESSION_ID", "sess-owned")
    monkeypatch.setenv("SHIM_STOP_REASON", stop_reason)
    monkeypatch.setenv("SHIM_IS_ERROR", is_error)
    owned = _dispatch(queue_dir, task, fresh_plan, adopt=True)
    assert isinstance(owned, DispatchOutcome)

    log = queue_dir / ".claude_task_runner" / "logs" / task.id / "attempt-1.stream.jsonl"
    running = TaskState(
        task_id=task.id,
        status="running",
        attempts=1,
        last_started_at=owned.run_record.started_at,
        pid=owned.run_record.pid,
        log_path=str(log),
    )
    write_state_atomic(running, state_path_for(queue_dir, task.id))

    exited = finalize_exited_worker(
        task=task,
        state=running,
        queue_dir=queue_dir,
        settings_dispatch=_SETTINGS.dispatch,
        settings_hooks=_SETTINGS.hooks,
        clock=RealClock(),
    )

    assert exited is not None
    assert exited.new_state.status == owned.new_state.status == status
    assert exited.run_record.stop_reason == stop_reason
    assert exited.new_state.session_id == owned.new_state.session_id == "sess-owned"
    for field in ("stop_reason", "error", "usage", "cost_usd", "killed_by_cap", "attempt", "pid"):
        assert getattr(exited.run_record, field) == getattr(owned.run_record, field), field


@pytest.mark.parametrize("adopt", [False, True])
def test_running_state_never_carries_an_earlier_attempts_worker(
    queue_dir: Path,
    task: Task,
    fresh_plan: SpawnPlan,
    monkeypatch: pytest.MonkeyPatch,
    reset_shim_env: None,
    adopt: bool,
) -> None:
    """A state demoted at a restart keeps its old worker's pid and log. The
    next attempt's running writes must carry no trace of them: none before
    the spawn, and its own pid with its own log (None when pipe-backed)
    after. Otherwise the worker probe compares the new pipe-backed worker's
    stdout with the old log and calls it a different process."""
    import claude_task_runner.runner.dispatcher as dmod

    old_log = queue_dir / ".claude_task_runner" / "logs" / task.id / "attempt-1.stream.jsonl"
    stale = TaskState(
        task_id=task.id,
        status="failed",
        attempts=1,
        pid=4_000_001,
        log_path=str(old_log),
        stop_reason="orphaned_by_supervisor_restart",
    )
    write_state_atomic(stale, state_path_for(queue_dir, task.id))
    running_writes: list[tuple[int | None, str | None]] = []
    real_write = dmod.write_state_atomic

    def _spy(state: TaskState, path: Path) -> None:
        if state.status == "running":
            running_writes.append((state.pid, state.log_path))
        real_write(state, path)

    monkeypatch.setattr(dmod, "write_state_atomic", _spy)

    outcome = dispatch(
        task=task,
        state=stale,
        plan=fresh_plan,
        queue_dir=queue_dir,
        clock=RealClock(),
        settings_caps=_caps(),
        settings_hooks=_hooks(),
        claude_executable=str(SHIM_PATH),
        adopt_workers=adopt,
    )

    new_log = str(queue_dir / ".claude_task_runner" / "logs" / task.id / "attempt-2.stream.jsonl")
    assert running_writes[0] == (None, None)
    worker_pid = outcome.run_record.pid
    assert worker_pid is not None and worker_pid != 4_000_001
    assert running_writes[1] == (worker_pid, new_log if adopt else None)
    assert all(pid in (None, worker_pid) for pid, _log in running_writes)
    assert all(log in (None, new_log) for _pid, log in running_writes)


@pytest.mark.usefixtures("real_proc")
def test_spawn_records_the_workers_boot_and_start_tick(
    queue_dir: Path,
    task: Task,
    fresh_plan: SpawnPlan,
    monkeypatch: pytest.MonkeyPatch,
    reset_shim_env: None,
) -> None:
    """The spawn writes the worker's boot id and start tick to its attempt's
    identity file beside the stream log, and no state YAML carries them, so
    a runner that does not know them still reads every state."""
    import claude_task_runner.runner.dispatcher as dmod

    written: list[dict[str, object]] = []
    real_write = dmod.write_state_atomic

    def _spy(state: TaskState, path: Path) -> None:
        written.append(state.model_dump(mode="json"))
        real_write(state, path)

    monkeypatch.setattr(dmod, "write_state_atomic", _spy)

    outcome = _dispatch(queue_dir, task, fresh_plan, adopt=True)

    identity = json.loads(
        (queue_dir / ".claude_task_runner" / "logs" / task.id / "attempt-1.worker").read_text()
    )
    assert identity["pid"] == outcome.run_record.pid  # type: ignore[attr-defined]
    assert identity["boot_id"] == Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    assert isinstance(identity["start_ticks"], int) and identity["start_ticks"] > 0
    assert written and all(
        "worker_boot_id" not in w and "worker_start_ticks" not in w for w in written
    )


# ---------------------------------------------------------------------------
# A dispatch loop that raises (H88): the worker is stopped and the attempt
# recorded, or parked when the worker cannot be stopped
# ---------------------------------------------------------------------------

_INIT_LINES = 1
"""The shim's first line is its ``system/init`` event."""


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> object:
    """Keeps every worker ``dispatch`` spawns, and kills any still running
    at teardown."""
    procs: list[subprocess.Popen[str]] = []
    real_popen = subprocess.Popen

    def _popen(*args: object, **kwargs: object) -> subprocess.Popen[str]:
        proc = real_popen(*args, **kwargs)  # type: ignore[call-overload]
        procs.append(proc)
        return proc

    monkeypatch.setattr(dispatcher_mod.subprocess, "Popen", _popen)
    yield procs
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


@pytest.mark.parametrize("adopt", [True, False], ids=["file-backed", "pipe"])
def test_a_loop_that_raises_mid_run_stops_the_worker_and_records_a_failure(
    queue_dir: Path,
    task: Task,
    fresh_plan: SpawnPlan,
    monkeypatch: pytest.MonkeyPatch,
    reset_shim_env: None,
    spawned: list[subprocess.Popen[str]],
    raise_in_dispatch_loop: Callable[[int | None], None],
    adopt: bool,
) -> None:
    """The loop raises after the init event while the worker still runs. The
    worker is stopped, and exactly one run is recorded, failed with
    dispatch_loop_error and the exception, with the session id the loop
    had already seen."""
    monkeypatch.setenv("SHIM_SESSION_ID", "sess-loop")
    monkeypatch.setenv("SHIM_SLEEP_BETWEEN_S", "30")
    raise_in_dispatch_loop(_INIT_LINES)

    outcome = _dispatch(queue_dir, task, fresh_plan, adopt=adopt)
    assert isinstance(outcome, DispatchOutcome)

    [worker] = spawned
    assert worker.returncode == -signal.SIGTERM
    reloaded = load_state(state_path_for(queue_dir, task.id))
    assert reloaded == outcome.new_state
    assert (reloaded.status, reloaded.stop_reason, reloaded.error) == (
        "failed",
        "dispatch_loop_error",
        "dispatch loop raised RuntimeError: boom",
    )
    [run] = reloaded.runs
    assert (run.stop_reason, run.error, run.pid) == (
        "dispatch_loop_error",
        "dispatch loop raised RuntimeError: boom",
        worker.pid,
    )
    assert run.killed_by_cap is None
    assert reloaded.session_id == "sess-loop"
    assert (reloaded.pid, reloaded.log_path) == (None, None)


@pytest.mark.parametrize("adopt", [True, False], ids=["file-backed", "pipe"])
def test_a_loop_that_raises_before_the_first_line_records_an_empty_failure(
    queue_dir: Path,
    task: Task,
    fresh_plan: SpawnPlan,
    monkeypatch: pytest.MonkeyPatch,
    reset_shim_env: None,
    spawned: list[subprocess.Popen[str]],
    raise_in_dispatch_loop: Callable[[int | None], None],
    adopt: bool,
) -> None:
    """The loop raises before it reads a line, and the worker has written
    none. The worker is stopped, and the run fails with dispatch_loop_error
    and nothing a stream event would have given: no session id, no usage."""
    monkeypatch.setenv("SHIM_SLEEP_BEFORE_S", "30")
    raise_in_dispatch_loop(0)

    outcome = _dispatch(queue_dir, task, fresh_plan, adopt=adopt)
    assert isinstance(outcome, DispatchOutcome)

    [worker] = spawned
    assert worker.returncode == -signal.SIGTERM
    reloaded = load_state(state_path_for(queue_dir, task.id))
    assert reloaded == outcome.new_state
    assert (reloaded.status, reloaded.stop_reason, reloaded.error) == (
        "failed",
        "dispatch_loop_error",
        "dispatch loop raised RuntimeError: boom",
    )
    [run] = reloaded.runs
    assert (run.stop_reason, run.usage.total_tokens, run.skipped_stream_lines) == (
        "dispatch_loop_error",
        0,
        0,
    )
    assert reloaded.session_id is None


@pytest.mark.parametrize("adopt", [True, False], ids=["file-backed", "pipe"])
def test_a_terminal_result_written_before_the_loop_raised_stands(
    queue_dir: Path,
    task: Task,
    fresh_plan: SpawnPlan,
    monkeypatch: pytest.MonkeyPatch,
    reset_shim_env: None,
    spawned: list[subprocess.Popen[str]],
    raise_in_dispatch_loop: Callable[[int | None], None],
    adopt: bool,
) -> None:
    """The loop raises only after the worker's terminal result: the worker
    finished its work, so its result is the run's record and the task is
    completed, not failed and dispatched again."""
    monkeypatch.setenv("SHIM_SESSION_ID", "sess-done")
    raise_in_dispatch_loop(None)

    outcome = _dispatch(queue_dir, task, fresh_plan, adopt=adopt)
    assert isinstance(outcome, DispatchOutcome)

    reloaded = load_state(state_path_for(queue_dir, task.id))
    assert (reloaded.status, reloaded.stop_reason, reloaded.error) == (
        "completed",
        "end_turn",
        None,
    )
    [run] = reloaded.runs
    assert (run.stop_reason, run.error) == ("end_turn", None)
    assert reloaded.session_id == "sess-done"


@pytest.mark.parametrize("adopt", [True, False], ids=["file-backed", "pipe"])
def test_a_terminal_result_read_ahead_of_the_loop_error_stands(
    queue_dir: Path,
    task: Task,
    fresh_plan: SpawnPlan,
    monkeypatch: pytest.MonkeyPatch,
    reset_shim_env: None,
    spawned: list[subprocess.Popen[str]],
    raise_in_dispatch_loop: Callable[[int | None], None],
    adopt: bool,
) -> None:
    """The worker writes every event in one write, so the read that gives
    the loop the event it raises after also takes the terminal result. The
    result still counts, from the pipe's buffer or the re-read log, and the
    run completes."""
    monkeypatch.setenv("SHIM_SESSION_ID", "sess-ahead")
    monkeypatch.setenv("SHIM_SINGLE_WRITE", "1")
    raise_in_dispatch_loop(_INIT_LINES + 2)

    outcome = _dispatch(queue_dir, task, fresh_plan, adopt=adopt)
    assert isinstance(outcome, DispatchOutcome)

    reloaded = load_state(state_path_for(queue_dir, task.id))
    assert reloaded == outcome.new_state
    assert (reloaded.status, reloaded.stop_reason, reloaded.error, reloaded.session_id) == (
        "completed",
        "end_turn",
        None,
        "sess-ahead",
    )
    [run] = reloaded.runs
    assert (run.stop_reason, run.error) == ("end_turn", None)


@pytest.mark.parametrize("adopt", [True, False], ids=["file-backed", "pipe"])
def test_a_terminal_result_read_ahead_of_a_raise_in_the_loop_body_stands(
    queue_dir: Path,
    task: Task,
    fresh_plan: SpawnPlan,
    monkeypatch: pytest.MonkeyPatch,
    reset_shim_env: None,
    spawned: list[subprocess.Popen[str]],
    adopt: bool,
) -> None:
    """The loop's own body raises (here the cap check, on the second event),
    which closes the chain of generators feeding it. That must not close
    the worker's stdout: the result read ahead with that event is still
    there for the drain, and the run completes."""
    monkeypatch.setenv("SHIM_SESSION_ID", "sess-body")
    monkeypatch.setenv("SHIM_SINGLE_WRITE", "1")
    real_evaluate = dispatcher_mod.caps_mod.evaluate_caps
    calls: list[int] = []

    def _raises_on_the_second_event(**kwargs: object) -> object:
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real_evaluate(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(dispatcher_mod.caps_mod, "evaluate_caps", _raises_on_the_second_event)

    outcome = _dispatch(queue_dir, task, fresh_plan, adopt=adopt)
    assert isinstance(outcome, DispatchOutcome)

    reloaded = load_state(state_path_for(queue_dir, task.id))
    assert reloaded == outcome.new_state
    assert (reloaded.status, reloaded.stop_reason, reloaded.error, reloaded.session_id) == (
        "completed",
        "end_turn",
        None,
        "sess-body",
    )


@pytest.mark.parametrize("adopt", [True, False], ids=["file-backed", "pipe"])
def test_a_line_that_is_not_utf8_is_a_malformed_line(
    queue_dir: Path,
    task: Task,
    fresh_plan: SpawnPlan,
    monkeypatch: pytest.MonkeyPatch,
    reset_shim_env: None,
    adopt: bool,
) -> None:
    """Bytes that are not UTF-8 decode with replacement characters on both
    paths, so their line is skipped as malformed, not an exception in the
    dispatch loop, and the run completes."""
    monkeypatch.setenv("SHIM_INVALID_UTF8_LINE", "1")

    outcome = _dispatch(queue_dir, task, fresh_plan, adopt=adopt)
    assert isinstance(outcome, DispatchOutcome)

    reloaded = load_state(state_path_for(queue_dir, task.id))
    assert (reloaded.status, reloaded.stop_reason) == ("completed", "end_turn")
    [run] = reloaded.runs
    assert (run.stop_reason, run.skipped_stream_lines) == ("end_turn", 1)


@pytest.mark.parametrize("adopt", [True, False], ids=["file-backed", "pipe"])
def test_a_monitor_thread_that_cannot_start_stops_the_worker(
    queue_dir: Path,
    task: Task,
    fresh_plan: SpawnPlan,
    monkeypatch: pytest.MonkeyPatch,
    reset_shim_env: None,
    spawned: list[subprocess.Popen[str]],
    adopt: bool,
) -> None:
    """The dispatcher-alive monitor's thread cannot start. The worker is not
    left running unwatched: it is stopped, and its run fails with
    dispatch_loop_error naming the error."""
    monkeypatch.setenv("SHIM_SLEEP_BETWEEN_S", "30")

    def _cannot_start(_monitor: object) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(dispatcher_mod._DispatcherAliveMonitor, "start", _cannot_start)

    outcome = _dispatch(queue_dir, task, fresh_plan, adopt=adopt)
    assert isinstance(outcome, DispatchOutcome)

    [worker] = spawned
    assert worker.returncode == -signal.SIGTERM
    reloaded = load_state(state_path_for(queue_dir, task.id))
    assert reloaded == outcome.new_state
    assert (reloaded.status, reloaded.stop_reason, reloaded.error) == (
        "failed",
        "dispatch_loop_error",
        "dispatch loop raised RuntimeError: can't start new thread",
    )


def test_a_worker_that_exited_on_its_own_keeps_its_own_record(
    queue_dir: Path,
    task: Task,
    fresh_plan: SpawnPlan,
    monkeypatch: pytest.MonkeyPatch,
    reset_shim_env: None,
    spawned: list[subprocess.Popen[str]],
    raise_in_dispatch_loop: Callable[[int | None], None],
) -> None:
    """The worker fails and exits on its own, and the loop raises only after
    that. Nothing is signalled, since the worker's pid may be another
    process's by now, and its run records its own exit and stderr, not
    dispatch_loop_error. File-backed: the tailer reaps the worker before the
    loop ends, so it is gone when the loop raises."""
    monkeypatch.setenv("SHIM_FAIL_FAST", "worker crashed")
    raise_in_dispatch_loop(None)
    stopped: list[int] = []
    monkeypatch.setattr(dispatcher_mod, "_terminate", lambda process: stopped.append(process.pid))

    outcome = _dispatch(queue_dir, task, fresh_plan, adopt=True)
    assert isinstance(outcome, DispatchOutcome)

    [worker] = spawned
    assert (worker.returncode, stopped) == (1, [])
    reloaded = load_state(state_path_for(queue_dir, task.id))
    assert reloaded == outcome.new_state
    [run] = reloaded.runs
    assert (run.stop_reason, run.error) == ("process_exit_nonzero", "worker crashed")


@pytest.mark.parametrize("adopt", [True, False], ids=["file-backed", "pipe"])
def test_a_worker_that_cannot_be_stopped_is_parked_with_no_run(
    queue_dir: Path,
    task: Task,
    fresh_plan: SpawnPlan,
    monkeypatch: pytest.MonkeyPatch,
    reset_shim_env: None,
    spawned: list[subprocess.Popen[str]],
    raise_in_dispatch_loop: Callable[[int | None], None],
    adopt: bool,
) -> None:
    """When the worker cannot be stopped it may still run, so the attempt is
    not recorded as over: the task is parked possibly_hung with the
    worker's pid, so it is not dispatched again and its run is recorded
    once it exits, and dispatch raises. The park goes to ``on_park``, to be
    reported as the supervisor's other parks are."""
    monkeypatch.setenv("SHIM_SLEEP_BETWEEN_S", "30")
    raise_in_dispatch_loop(_INIT_LINES)

    stops: list[int] = []

    def _cannot_stop(process: subprocess.Popen[str]) -> None:
        stops.append(process.pid)
        raise dispatcher_mod.TerminateFailed(f"task pid {process.pid} survived SIGKILL")

    monkeypatch.setattr(dispatcher_mod, "_terminate", _cannot_stop)
    parks: list[tuple[TaskState, str]] = []

    with pytest.raises(dispatcher_mod.DispatchError) as exc_info:
        _dispatch(
            queue_dir,
            task,
            fresh_plan,
            adopt=adopt,
            on_park=lambda state, why: parks.append((state, why)),
        )

    [worker] = spawned
    assert (stops, worker.poll()) == ([worker.pid], None)
    assert str(exc_info.value) == (
        f"task {task.id}: dispatch loop raised RuntimeError: boom; "
        f"worker pid={worker.pid} could not be stopped and is parked"
    )
    reloaded = load_state(state_path_for(queue_dir, task.id))
    assert (reloaded.status, reloaded.stop_reason) == ("possibly_hung", "worker_still_running")
    assert reloaded.pid == worker.pid
    # The parked-worker pass probes the worker by its log and records its run
    # from it, so a file-backed park keeps the log; a pipe-backed one has none.
    log = queue_dir / ".claude_task_runner" / "logs" / task.id / "attempt-1.stream.jsonl"
    assert reloaded.log_path == (str(log) if adopt else None)
    assert reloaded.runs == []
    assert parks == [(reloaded, "after its dispatch loop failed and it could not be stopped")]


def _dispatch_in_memory(
    queue_dir: Path,
    task: Task,
    plan: SpawnPlan,
    *,
    on_park: Callable[[TaskState, str], None] | None = None,
) -> object:
    """``dispatch`` with ``persist_state=False``, which also means the pipe path."""
    return dispatch(
        task=task,
        state=TaskState(task_id=task.id),
        plan=plan,
        queue_dir=queue_dir,
        clock=RealClock(),
        settings_caps=_caps(),
        settings_hooks=_hooks(),
        claude_executable=str(SHIM_PATH),
        persist_state=False,
        on_park=on_park,
    )


def test_a_loop_that_raises_in_memory_returns_its_failed_run(
    queue_dir: Path,
    task: Task,
    fresh_plan: SpawnPlan,
    monkeypatch: pytest.MonkeyPatch,
    reset_shim_env: None,
    spawned: list[subprocess.Popen[str]],
    raise_in_dispatch_loop: Callable[[int | None], None],
) -> None:
    """With ``persist_state=False`` no state file is written. The worker is
    still stopped, and the outcome carries the dispatch_loop_error run."""
    monkeypatch.setenv("SHIM_SESSION_ID", "sess-memory")
    monkeypatch.setenv("SHIM_SLEEP_BETWEEN_S", "30")
    raise_in_dispatch_loop(_INIT_LINES)

    outcome = _dispatch_in_memory(queue_dir, task, fresh_plan)
    assert isinstance(outcome, DispatchOutcome)

    [worker] = spawned
    assert worker.returncode == -signal.SIGTERM
    assert not state_path_for(queue_dir, task.id).exists()
    assert (
        outcome.new_state.status,
        outcome.new_state.stop_reason,
        outcome.new_state.error,
        outcome.new_state.session_id,
    ) == ("failed", "dispatch_loop_error", "dispatch loop raised RuntimeError: boom", "sess-memory")
    assert outcome.run_record.stop_reason == "dispatch_loop_error"


def test_a_worker_that_cannot_be_stopped_in_memory_is_not_parked(
    queue_dir: Path,
    task: Task,
    fresh_plan: SpawnPlan,
    monkeypatch: pytest.MonkeyPatch,
    reset_shim_env: None,
    spawned: list[subprocess.Popen[str]],
    raise_in_dispatch_loop: Callable[[int | None], None],
) -> None:
    """With ``persist_state=False`` there is no state to park: nothing is
    written or reported, and dispatch raises, saying so."""
    monkeypatch.setenv("SHIM_SLEEP_BETWEEN_S", "30")
    raise_in_dispatch_loop(_INIT_LINES)

    def _cannot_stop(process: subprocess.Popen[str]) -> None:
        raise dispatcher_mod.TerminateFailed(f"task pid {process.pid} survived SIGKILL")

    monkeypatch.setattr(dispatcher_mod, "_terminate", _cannot_stop)
    parks: list[tuple[TaskState, str]] = []

    with pytest.raises(dispatcher_mod.DispatchError) as exc_info:
        _dispatch_in_memory(
            queue_dir, task, fresh_plan, on_park=lambda state, why: parks.append((state, why))
        )

    [worker] = spawned
    assert worker.poll() is None
    assert str(exc_info.value) == (
        f"task {task.id}: dispatch loop raised RuntimeError: boom; worker pid={worker.pid} "
        "could not be stopped and is not parked: this dispatch keeps no state"
    )
    assert not state_path_for(queue_dir, task.id).exists()
    assert parks == []


_TOKEN_CAP = 5
"""Below the 7 tokens of the shim's first assistant event, so its cap kill
fires there, while the worker sleeps before its result."""


def _dispatch_capped(queue_dir: Path, task: Task, plan: SpawnPlan, *, adopt: bool) -> object:
    return dispatch(
        task=task,
        state=TaskState(task_id=task.id),
        plan=plan,
        queue_dir=queue_dir,
        clock=RealClock(),
        settings_caps=_caps().model_copy(update={"max_tokens_per_task": _TOKEN_CAP}),
        settings_hooks=_hooks(),
        claude_executable=str(SHIM_PATH),
        adopt_workers=adopt,
    )


@pytest.mark.parametrize("adopt", [True, False], ids=["file-backed", "pipe"])
def test_a_cap_kill_that_raises_is_retried_and_recorded_as_the_cap(
    queue_dir: Path,
    task: Task,
    fresh_plan: SpawnPlan,
    monkeypatch: pytest.MonkeyPatch,
    reset_shim_env: None,
    spawned: list[subprocess.Popen[str]],
    adopt: bool,
) -> None:
    """The loop's cap kill raises TerminateFailed, which ends the loop. The
    stop after it ends the worker, and the run is killed by the cap it
    exceeded, not a dispatch_loop_error naming the failed kill."""
    monkeypatch.setenv("SHIM_SLEEP_BEFORE_RESULT_S", "30")
    real_terminate = dispatcher_mod._terminate
    calls: list[int] = []

    def _fails_once(process: subprocess.Popen[str]) -> None:
        calls.append(process.pid)
        if len(calls) == 1:
            raise dispatcher_mod.TerminateFailed(f"task pid {process.pid} survived SIGKILL")
        real_terminate(process)

    monkeypatch.setattr(dispatcher_mod, "_terminate", _fails_once)

    outcome = _dispatch_capped(queue_dir, task, fresh_plan, adopt=adopt)
    assert isinstance(outcome, DispatchOutcome)

    [worker] = spawned
    assert (calls, worker.returncode) == ([worker.pid, worker.pid], -signal.SIGTERM)
    reloaded = load_state(state_path_for(queue_dir, task.id))
    assert reloaded == outcome.new_state
    [run] = reloaded.runs
    assert (run.stop_reason, run.killed_by_cap, run.error) == (
        "killed_by_cap",
        "tokens",
        f"tokens cap exceeded: 7 > {_TOKEN_CAP}",
    )


@pytest.mark.parametrize("adopt", [True, False], ids=["file-backed", "pipe"])
def test_a_cap_kill_that_cannot_stop_the_worker_parks_it(
    queue_dir: Path,
    task: Task,
    fresh_plan: SpawnPlan,
    monkeypatch: pytest.MonkeyPatch,
    reset_shim_env: None,
    spawned: list[subprocess.Popen[str]],
    adopt: bool,
) -> None:
    """Neither the cap kill nor the stop after it ends the worker, so the
    task is parked with the worker on record and no run, and dispatch
    raises."""
    monkeypatch.setenv("SHIM_SLEEP_BEFORE_RESULT_S", "30")
    calls: list[int] = []

    def _cannot_stop(process: subprocess.Popen[str]) -> None:
        calls.append(process.pid)
        raise dispatcher_mod.TerminateFailed(f"task pid {process.pid} survived SIGKILL")

    monkeypatch.setattr(dispatcher_mod, "_terminate", _cannot_stop)

    with pytest.raises(dispatcher_mod.DispatchError) as exc_info:
        _dispatch_capped(queue_dir, task, fresh_plan, adopt=adopt)

    [worker] = spawned
    assert (calls, worker.poll()) == ([worker.pid, worker.pid], None)
    assert str(exc_info.value) == (
        f"task {task.id}: dispatch loop raised TerminateFailed: task pid {worker.pid} "
        f"survived SIGKILL; worker pid={worker.pid} could not be stopped and is parked"
    )
    reloaded = load_state(state_path_for(queue_dir, task.id))
    assert (reloaded.status, reloaded.stop_reason, reloaded.pid, reloaded.runs) == (
        "possibly_hung",
        "worker_still_running",
        worker.pid,
        [],
    )
