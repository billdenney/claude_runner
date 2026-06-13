"""Unit tests for adopted-worker monitoring (ADR-0025).

``runner.dispatcher.adopt_worker`` re-attaches to a ``claude --print``
worker this supervisor did NOT spawn: it has a ``pid`` + a ``log_path``
on the task's ``TaskState`` but no ``Popen``. Liveness is
``os.kill(pid, 0)``; completion is "pid gone"; the outcome is inferred
from the terminal stream-json ``result`` event in the log (no
``returncode`` available).

These tests drive ``adopt_worker`` with:

* a fully-written stdout log (so the tailer drains once and stops),
* a monkeypatched ``_pid_alive`` so we control "alive" vs "gone" without
  a real process,
* a no-op ``sleep_fn`` so no wall-clock time passes,

and assert exact terminal status / stop_reason / RunRecord fields.

Covered: adopt-alive→completed (terminal result), adopt-crashed→failed
(pid gone, no terminal result), adopt cap/silence→terminate-by-pid, and
the recheck race guard (a concurrent reaper finalize is not clobbered).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_task_runner.clock import RealClock
from claude_task_runner.config.schema import TaskCapsSettings
from claude_task_runner.queue.schema import Task, TaskState
from claude_task_runner.queue.store import (
    load_state,
    queue_runtime_dir,
    state_path_for,
    todo_dir,
    write_state_atomic,
)
from claude_task_runner.runner import dispatcher as dispatcher_mod
from claude_task_runner.runner.dispatcher import adopt_worker

_PID = 999_001


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


def _caps(
    *, alert: float = 600.0, kill: float = 0.0, max_duration: float = 0.0
) -> TaskCapsSettings:
    return TaskCapsSettings(
        max_tokens_per_task=0,
        max_duration_s_per_task=max_duration,
        heartbeat_silence_alert_s=alert,
        heartbeat_silence_kill_s=kill,
        # Large interval so the alive monitor's background loop never
        # fires a second persist during the short test.
        dispatcher_alive_write_interval_s=600.0,
    )


def _result_line(stop_reason: str = "end_turn", *, is_error: bool = False) -> str:
    sub = "error" if is_error else "success"
    return (
        f'{{"type":"result","subtype":"{sub}","stop_reason":"{stop_reason}",'
        f'"is_error":{"true" if is_error else "false"},"total_cost_usd":0.07,'
        '"duration_ms":1234,"usage":{"input_tokens":120,"output_tokens":80}}'
    )


def _init_line(session_id: str = "sess-adopt") -> str:
    return f'{{"type":"system","subtype":"init","session_id":"{session_id}"}}'


def _assistant_line() -> str:
    return (
        '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}],'
        '"usage":{"input_tokens":60,"output_tokens":40}}}'
    )


def _seed_running(
    queue_dir: Path,
    task: Task,
    *,
    log_path: Path,
    started_at: datetime,
    last_heartbeat_at: datetime | None = None,
    pid: int = _PID,
    session_id: str | None = None,
) -> TaskState:
    state = TaskState(
        task_id=task.id,
        status="running",
        # A running task always has >= 1 attempt (the dispatch that
        # spawned the worker bumped it before the run).
        attempts=1,
        last_started_at=started_at,
        last_heartbeat_at=last_heartbeat_at,
        pid=pid,
        log_path=str(log_path),
        session_id=session_id,
    )
    write_state_atomic(state, state_path_for(queue_dir, task.id))
    return state


def _write_log(log_path: Path, lines: list[str]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("".join(line + "\n" for line in lines))


def test_adopt_alive_finalizes_from_terminal_result(
    queue_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker whose log carries a terminal ``end_turn`` result and whose
    pid is gone finalizes as completed, with usage/cost/session inferred
    from the stream — no ``returncode`` consulted."""
    task = Task(id="010-adopt-ok", title="t", prompt="p", working_dir=None)
    log = queue_dir / ".claude_task_runner" / "logs" / task.id / "attempt-1.stream.jsonl"
    _write_log(log, [_init_line(), _assistant_line(), _result_line("end_turn")])

    started = datetime(2026, 6, 13, 12, 0, tzinfo=UTC)
    state = _seed_running(queue_dir, task, log_path=log, started_at=started)

    # Pid is already gone ⇒ tailer drains the complete log once and stops.
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: False)

    outcome = adopt_worker(
        task=task,
        state=state,
        queue_dir=queue_dir,
        clock=RealClock(),
        settings_caps=_caps(),
        sleep_fn=lambda _s: None,
    )

    assert outcome.new_state.status == "completed"
    assert outcome.run_record.stop_reason == "end_turn"
    assert outcome.run_record.error is None
    # Usage + cost inferred from the terminal result event.
    assert outcome.run_record.usage.input_tokens == 120
    assert outcome.run_record.usage.output_tokens == 80
    assert outcome.run_record.cost_usd == pytest.approx(0.07)
    # Attempt count is NOT bumped — adoption monitors the existing attempt
    # (seeded at 1, the value the original dispatch set).
    assert outcome.run_record.attempt == 1
    assert outcome.summary.session_id == "sess-adopt"

    # Persisted: pid + log_path cleared on finalize.
    reloaded = load_state(state_path_for(queue_dir, task.id))
    assert reloaded.status == "completed"
    assert reloaded.pid is None
    assert reloaded.log_path is None
    assert reloaded.session_id == "sess-adopt"


def test_adopt_crashed_no_result_finalizes_failed(
    queue_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker whose pid vanished WITHOUT writing a terminal result event
    (crash / OOM mid-run) finalizes as failed — the inferred exit is
    non-zero because there is no result to classify."""
    task = Task(id="011-adopt-crash", title="t", prompt="p", working_dir=None)
    log = queue_dir / ".claude_task_runner" / "logs" / task.id / "attempt-1.stream.jsonl"
    # Init + one assistant message, then nothing — the worker died.
    _write_log(log, [_init_line(), _assistant_line()])

    started = datetime(2026, 6, 13, 12, 0, tzinfo=UTC)
    state = _seed_running(queue_dir, task, log_path=log, started_at=started)

    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: False)

    outcome = adopt_worker(
        task=task,
        state=state,
        queue_dir=queue_dir,
        clock=RealClock(),
        settings_caps=_caps(),
        sleep_fn=lambda _s: None,
    )

    assert outcome.new_state.status == "failed"
    # No terminal result ⇒ _build_run_record records a process-exit failure.
    assert outcome.run_record.stop_reason == "process_exit_nonzero"
    assert outcome.run_record.error is not None
    # Session id was still captured from the init event before the crash.
    assert outcome.summary.session_id == "sess-adopt"

    reloaded = load_state(state_path_for(queue_dir, task.id))
    assert reloaded.status == "failed"
    assert reloaded.pid is None
    assert reloaded.log_path is None


def test_adopt_missing_log_path_finalizes_crashed(queue_dir: Path) -> None:
    """A running state with a live pid but no recorded log_path can't be
    tailed; adopt_worker finalizes it as crashed rather than leaving it
    stuck running. (Defensive — the startup pass screens these out.)"""
    task = Task(id="012-adopt-nolog", title="t", prompt="p", working_dir=None)
    started = datetime(2026, 6, 13, 12, 0, tzinfo=UTC)
    state = TaskState(
        task_id=task.id,
        status="running",
        last_started_at=started,
        pid=_PID,
        log_path=None,
    )
    write_state_atomic(state, state_path_for(queue_dir, task.id))

    outcome = adopt_worker(
        task=task,
        state=state,
        queue_dir=queue_dir,
        clock=RealClock(),
        settings_caps=_caps(),
        sleep_fn=lambda _s: None,
    )
    assert outcome.new_state.status == "failed"


def test_adopt_cap_kill_terminates_by_pid(queue_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An adopted worker that is still alive when a per-task cap is
    breached is SIGTERM'd by pid (killpg) — the same enforcement an owned
    worker gets, but via the by-pid terminate since there is no Popen.

    Driven by the DURATION cap: ``started_at`` is far in the past, so the
    first event's ``evaluate_caps`` (``now - started_at``) trips the cap
    and the loop terminates the worker. The log carries extra events
    after the init so the loop is mid-stream (not at EOF) when it kills —
    abandoning the tailer generator on ``break``, so there is no hang."""
    task = Task(id="013-adopt-cap", title="t", prompt="p", working_dir=None)
    log = queue_dir / ".claude_task_runner" / "logs" / task.id / "attempt-1.stream.jsonl"
    _write_log(log, [_init_line(), _assistant_line(), _assistant_line()])

    # last_started_at one hour ago ⇒ on the first event the 300s duration
    # cap is already exceeded.
    started = datetime.now(UTC) - timedelta(hours=1)
    state = _seed_running(queue_dir, task, log_path=log, started_at=started)

    # The worker stays alive for the whole loop so the CAP verdict (not
    # pid-gone) is what ends it.
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: True)

    # Record the kill instead of signalling a real pid.
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        dispatcher_mod, "_signal_group_by_pid", lambda pid, sig: killed.append((pid, sig))
    )

    outcome = adopt_worker(
        task=task,
        state=state,
        queue_dir=queue_dir,
        clock=RealClock(),
        # 300s duration cap; started_at is 1h ago ⇒ tripped on first event.
        settings_caps=_caps(max_duration=300.0),
        sleep_fn=lambda _s: None,
    )

    # The worker group was SIGTERM'd by pid (the kill mechanism for the
    # adopted path).
    assert killed, "expected a killpg-by-pid signal"
    assert killed[0][0] == _PID
    # Recorded as a duration cap kill.
    assert outcome.run_record.killed_by_cap == "duration"
    assert outcome.new_state.status == "failed"


def test_adopt_finalize_stands_down_when_reaper_won(
    queue_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrency guard: if a per-tick reaper demotes the task off
    ``running`` between the adopt monitor's verdict and its terminal
    write, the adopt monitor must NOT clobber the reaper's record — it
    re-reads status and stands down."""
    task = Task(id="014-adopt-race", title="t", prompt="p", working_dir=None)
    log = queue_dir / ".claude_task_runner" / "logs" / task.id / "attempt-1.stream.jsonl"
    _write_log(log, [_init_line(), _result_line("end_turn")])

    started = datetime(2026, 6, 13, 12, 0, tzinfo=UTC)
    state = _seed_running(queue_dir, task, log_path=log, started_at=started)

    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: False)

    # Simulate a reaper finalize landing during the adopt monitor's run:
    # patch load_state (the recheck) to report a non-running status the
    # first time the recheck reads it.
    real_load_state = dispatcher_mod.load_state

    def racing_load_state(path: Path) -> TaskState:
        s = real_load_state(path)
        # Pretend the reaper already demoted it to failed.
        return s.model_copy(update={"status": "failed", "stop_reason": "killed_by_silent_reaper"})

    monkeypatch.setattr(dispatcher_mod, "load_state", racing_load_state)

    outcome = adopt_worker(
        task=task,
        state=state,
        queue_dir=queue_dir,
        clock=RealClock(),
        settings_caps=_caps(),
        sleep_fn=lambda _s: None,
    )

    # The adopt monitor stood down: it returns the concurrent writer's
    # (reaper's) state, NOT its own completed verdict.
    assert outcome.new_state.status == "failed"
    assert outcome.new_state.stop_reason == "killed_by_silent_reaper"
