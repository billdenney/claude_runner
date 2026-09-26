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

Also ``finalize_exited_worker``, for a worker that exited before any
supervisor could adopt it: it records the run from the log's terminal
result exactly as ``adopt_worker`` would have, and writes nothing when
the log has no result event.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_task_runner.clock import FakeClock, RealClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.config.schema import DispatchSettings, HookSettings, TaskCapsSettings
from claude_task_runner.queue.schema import (
    SidecarOption,
    SidecarQuestion,
    SidecarRequest,
    Task,
    TaskState,
)
from claude_task_runner.queue.store import (
    load_state,
    queue_runtime_dir,
    state_path_for,
    todo_dir,
    write_state_atomic,
)
from claude_task_runner.runner import dispatcher as dispatcher_mod
from claude_task_runner.runner.dispatcher import (
    DispatchOutcome,
    adopt_worker,
    finalize_exited_worker,
)

from ._git_world import git, isolate_git
from ._sidecar_files import write_request

_PID = 999_001

_SETTINGS = load_settings(None)
"""Package defaults; the adopted finalize requires the dispatch and hook settings."""


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
        settings_dispatch=_SETTINGS.dispatch,
        settings_hooks=_SETTINGS.hooks,
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
        settings_dispatch=_SETTINGS.dispatch,
        settings_hooks=_SETTINGS.hooks,
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
        settings_dispatch=_SETTINGS.dispatch,
        settings_hooks=_SETTINGS.hooks,
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
        settings_dispatch=_SETTINGS.dispatch,
        settings_hooks=_SETTINGS.hooks,
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
        settings_dispatch=_SETTINGS.dispatch,
        settings_hooks=_SETTINGS.hooks,
        clock=RealClock(),
        settings_caps=_caps(),
        sleep_fn=lambda _s: None,
    )

    # The adopt monitor stood down: it returns the concurrent writer's
    # (reaper's) state, NOT its own completed verdict.
    assert outcome.new_state.status == "failed"
    assert outcome.new_state.stop_reason == "killed_by_silent_reaper"


# ---------------------------------------------------------------------------
# finalize_exited_worker: the worker exited before any supervisor adopted it
# ---------------------------------------------------------------------------

_STARTED = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
_RESTART = datetime(2026, 9, 26, 18, 30, tzinfo=UTC)


def _attempt_log(queue_dir: Path, task_id: str) -> Path:
    return queue_dir / ".claude_task_runner" / "logs" / task_id / "attempt-1.stream.jsonl"


def _set_mtime(path: Path, when: datetime) -> None:
    os.utime(path, (when.timestamp(), when.timestamp()))


def test_finalize_exited_success_result_completes(queue_dir: Path) -> None:
    """The 2026-09-26 restart incident: the worker finished in the restart
    gap and its log ends in a success result. It is recorded as completed
    with one RunRecord, the session id from the log and the account passed
    in. ``finished_at`` is the log's last write, not the restart."""
    task = Task(id="020-exited-ok", title="t", prompt="p", working_dir=None)
    log = _attempt_log(queue_dir, task.id)
    _write_log(log, [_init_line("sess-exited"), _assistant_line(), _result_line("end_turn")])
    finished = _STARTED + timedelta(minutes=7)
    _set_mtime(log, finished)
    state = _seed_running(queue_dir, task, log_path=log, started_at=_STARTED)

    outcome = finalize_exited_worker(
        task=task,
        state=state,
        queue_dir=queue_dir,
        settings_dispatch=_SETTINGS.dispatch,
        settings_hooks=_SETTINGS.hooks,
        clock=FakeClock(_RESTART),
        account="work",
    )

    assert outcome is not None
    run = outcome.run_record
    assert run.attempt == 1
    assert run.stop_reason == "end_turn"
    assert run.error is None
    assert run.killed_by_cap is None
    assert run.started_at == _STARTED
    assert run.finished_at == finished
    assert run.duration_s == 420.0
    assert run.usage.input_tokens == 120
    assert run.usage.output_tokens == 80
    assert run.cost_usd == pytest.approx(0.07)
    assert run.account == "work"
    assert run.pid == _PID
    assert run.resumed_from_session is None

    reloaded = load_state(state_path_for(queue_dir, task.id))
    assert reloaded == outcome.new_state
    assert reloaded.status == "completed"
    assert reloaded.stop_reason == "end_turn"
    assert reloaded.error is None
    assert reloaded.runs == [run]
    assert reloaded.attempts == 1
    assert reloaded.session_id == "sess-exited"
    assert reloaded.session_account == "work"
    assert reloaded.last_finished_at == finished
    assert reloaded.pid is None
    assert reloaded.log_path is None


@pytest.mark.parametrize(
    "lines",
    [
        pytest.param(None, id="log-file-missing"),
        pytest.param([], id="empty-log"),
        pytest.param([_init_line()], id="init-only"),
        pytest.param([_init_line(), _assistant_line()], id="died-mid-run"),
    ],
)
def test_finalize_exited_without_result_writes_nothing(
    queue_dir: Path, lines: list[str] | None
) -> None:
    """No terminal result event means the worker crashed or was killed.
    Nothing is recorded, so the startup reaper and ``reconcile_orphans``
    handle the task as they always have."""
    task = Task(id="021-exited-crash", title="t", prompt="p", working_dir=None)
    log = _attempt_log(queue_dir, task.id)
    if lines is not None:
        _write_log(log, lines)
    state = _seed_running(queue_dir, task, log_path=log, started_at=_STARTED)

    outcome = finalize_exited_worker(
        task=task,
        state=state,
        queue_dir=queue_dir,
        settings_dispatch=_SETTINGS.dispatch,
        settings_hooks=_SETTINGS.hooks,
        clock=FakeClock(_RESTART),
    )

    assert outcome is None
    assert load_state(state_path_for(queue_dir, task.id)) == state


def test_finalize_exited_without_log_path_writes_nothing(queue_dir: Path) -> None:
    """A state with no recorded log has nothing to finalize from."""
    task = Task(id="025-exited-nolog", title="t", prompt="p", working_dir=None)
    state = TaskState(
        task_id=task.id,
        status="running",
        attempts=1,
        last_started_at=_STARTED,
        pid=_PID,
        log_path=None,
    )
    write_state_atomic(state, state_path_for(queue_dir, task.id))

    outcome = finalize_exited_worker(
        task=task,
        state=state,
        queue_dir=queue_dir,
        settings_dispatch=_SETTINGS.dispatch,
        settings_hooks=_SETTINGS.hooks,
        clock=FakeClock(_RESTART),
    )

    assert outcome is None
    assert load_state(state_path_for(queue_dir, task.id)) == state


@dataclass(frozen=True)
class _Case:
    """One log shape, and the verdict both finalize paths must reach on it."""

    result_line: str
    stderr: str = ""
    open_sidecar: bool = False
    deliverable: bool | None = None
    """``None``: no working_dir (the ADR-0020 gate is skipped). Otherwise
    the task has a working_dir with one declared deliverable, present or
    not."""
    status: str = "completed"
    stop_reason: str = "end_turn"
    error: str | None = None


_CLASSIFICATION_CASES = [
    pytest.param(_Case(result_line=_result_line("end_turn")), id="success"),
    pytest.param(
        _Case(
            result_line=_result_line("error_max_turns", is_error=True),
            stderr="Error: reached max turns (40)\n",
            status="failed",
            stop_reason="error_max_turns",
            error="Error: reached max turns (40)",
        ),
        id="error-result",
    ),
    pytest.param(
        _Case(result_line=_result_line("tool_use"), status="failed", stop_reason="tool_use"),
        id="failure-class-stop-reason",
    ),
    pytest.param(
        _Case(result_line=_result_line("end_turn"), open_sidecar=True, status="awaiting_sidecar"),
        id="open-sidecar",
    ),
    pytest.param(
        _Case(result_line=_result_line("end_turn"), deliverable=True),
        id="output-gate-deliverable",
    ),
    pytest.param(
        _Case(
            result_line=_result_line("end_turn"),
            deliverable=False,
            status="failed",
            stop_reason="end_turn_no_output",
            error=(
                "no observable output produced (no new commit on branch; "
                "no open sidecar; no declared deliverable on disk)"
            ),
        ),
        id="output-gate-miss",
    ),
]


def _seed_case(qd: Path, tmp_path: Path, case: _Case) -> tuple[Task, TaskState]:
    """Seed one ``running`` task whose dead worker left ``case``'s log."""
    working_dir: Path | None = None
    if case.deliverable is not None:
        working_dir = tmp_path / "worktree"
        working_dir.mkdir(exist_ok=True)
        if case.deliverable:
            (working_dir / "report.md").write_text("done\n")
    task = Task(
        id="026-exited-case",
        title="t",
        prompt="p",
        working_dir=working_dir,
        deliverable_paths=[Path("report.md")] if working_dir is not None else [],
    )
    log = _attempt_log(qd, task.id)
    _write_log(log, [_init_line(), _assistant_line(), case.result_line])
    if case.stderr:
        log.with_name("attempt-1.stderr").write_text(case.stderr)
    if case.open_sidecar:
        _open_sidecar(qd, task.id)
    return task, _seed_running(qd, task, log_path=log, started_at=_STARTED)


def _open_sidecar(queue_dir: Path, task_id: str) -> None:
    """File one unanswered sidecar request for ``task_id``, as an agent does."""
    write_request(
        queue_dir,
        SidecarRequest(
            task_id=task_id,
            sequence=1,
            created_at=_STARTED,
            summary="Which encoding?",
            context="Both appear in the source",
            questions=[
                SidecarQuestion(
                    id="encoding",
                    prompt="Which encoding to use?",
                    options=[
                        SidecarOption(value="A", label="Encoding A"),
                        SidecarOption(value="B", label="Encoding B"),
                    ],
                    recommended="A",
                )
            ],
        ),
    )


def _fresh_queue(root: Path) -> Path:
    root.mkdir()
    queue_runtime_dir(root)
    todo_dir(root)
    return root


@pytest.mark.parametrize("case", _CLASSIFICATION_CASES)
def test_finalize_exited_classifies_like_adopt_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: _Case
) -> None:
    """For every log shape, the exited-worker finalize reaches the verdict
    ``adopt_worker`` reaches on the same log once the pid is gone: same
    status, stop_reason, error, usage and cost. Each queue gets its own
    copy of the log, the sidecar and the state; the worktree is shared."""
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: False)
    clock = FakeClock(_RESTART)

    exited_qd = _fresh_queue(tmp_path / "exited")
    task, state = _seed_case(exited_qd, tmp_path, case)
    exited = finalize_exited_worker(
        task=task,
        state=state,
        queue_dir=exited_qd,
        settings_dispatch=_SETTINGS.dispatch,
        settings_hooks=_SETTINGS.hooks,
        clock=clock,
    )

    adopted_qd = _fresh_queue(tmp_path / "adopted")
    task, state = _seed_case(adopted_qd, tmp_path, case)
    adopted: DispatchOutcome = adopt_worker(
        task=task,
        state=state,
        queue_dir=adopted_qd,
        settings_dispatch=_SETTINGS.dispatch,
        settings_hooks=_SETTINGS.hooks,
        clock=clock,
        settings_caps=_caps(),
        sleep_fn=lambda _s: None,
    )

    assert exited is not None
    assert (exited.new_state.status, exited.run_record.stop_reason, exited.run_record.error) == (
        case.status,
        case.stop_reason,
        case.error,
    )
    assert exited.new_state.status == adopted.new_state.status
    assert exited.new_state.session_id == adopted.new_state.session_id == "sess-adopt"
    for field in ("stop_reason", "error", "usage", "cost_usd", "killed_by_cap", "attempt", "pid"):
        assert getattr(exited.run_record, field) == getattr(adopted.run_record, field), field


@pytest.mark.parametrize(
    ("mtime", "expected"),
    [
        pytest.param(_STARTED + timedelta(minutes=5), _STARTED + timedelta(minutes=5), id="inside"),
        pytest.param(_RESTART + timedelta(hours=1), _RESTART, id="after-now"),
        pytest.param(_STARTED - timedelta(hours=1), _STARTED, id="before-start"),
        pytest.param(None, _RESTART, id="log-missing"),
    ],
)
def test_log_finished_at_clamps_to_the_attempt(
    tmp_path: Path, mtime: datetime | None, expected: datetime
) -> None:
    """The finish time is the log's mtime, capped at now and floored at the
    attempt's start (so the duration is never negative); now when the log
    can't be stat'ed."""
    log = tmp_path / "attempt-1.stream.jsonl"
    if mtime is not None:
        log.write_text("{}\n")
        _set_mtime(log, mtime)

    got = dispatcher_mod._log_finished_at(log, started_at=_STARTED, now=_RESTART)

    assert got == expected


# ---------------------------------------------------------------------------
# The owned path's gates on the adopted finalize: the recorded pre-dispatch
# SHA (ADR-0020), the terminal-close gate (ADR-0033), the sidecar re-file
# guard (ADR-0027) and the post-dispatch hook
# ---------------------------------------------------------------------------

_BLOCK_FILE = "needs_acquisition.jsonl"
_REFILE_THRESHOLD = _SETTINGS.failure_classifier.sidecar_refile_loop_threshold


@pytest.fixture
def worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clone of a bare remote with its seed commit pushed, the shape of a
    task worktree whose branch has a remote to push to."""
    isolate_git(tmp_path, monkeypatch)
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    repo = tmp_path / "wt"
    git(tmp_path, "clone", "-q", str(origin), str(repo))
    (repo / "README.md").write_text("seed\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "seed")
    git(repo, "push", "-q", "origin", "HEAD:main")
    return repo


def _commit_and_push(repo: Path, name: str) -> None:
    (repo / name).write_text("work\n")
    git(repo, "add", name)
    git(repo, "commit", "-qm", f"add {name}")
    git(repo, "push", "-q", "origin", "HEAD:main")


def _block_rows(queue_dir: Path) -> list[dict[str, object]]:
    path = queue_dir / _BLOCK_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _worktree_task(worktree: Path, *, deliverable: bool) -> Task:
    return Task(
        id="030-gates",
        title="t",
        prompt="p",
        working_dir=worktree,
        deliverable_paths=[Path("report.md")] if deliverable else [],
    )


def _finalize(
    queue_dir: Path,
    task: Task,
    *,
    pre_sha: str | None,
    entry: str = "exited",
    refile_count: int = 0,
    settings_hooks: HookSettings = _SETTINGS.hooks,
) -> DispatchOutcome:
    """Finalize a running attempt of ``task`` whose dead worker's log ends in
    a success result, through ``entry``: ``finalize_exited_worker`` or
    ``adopt_worker`` (whose pid the caller has made probe dead)."""
    log = _attempt_log(queue_dir, task.id)
    _write_log(log, [_init_line(), _assistant_line(), _result_line("end_turn")])
    state = _seed_running(queue_dir, task, log_path=log, started_at=_STARTED).model_copy(
        update={"pre_dispatch_sha": pre_sha, "sidecar_refile_count": refile_count}
    )
    write_state_atomic(state, state_path_for(queue_dir, task.id))
    settings_dispatch: DispatchSettings = _SETTINGS.dispatch.model_copy(
        update={"dispatch_block_file": _BLOCK_FILE}
    )
    if entry == "exited":
        outcome = finalize_exited_worker(
            task=task,
            state=state,
            queue_dir=queue_dir,
            clock=FakeClock(_RESTART),
            settings_dispatch=settings_dispatch,
            settings_hooks=settings_hooks,
            settings_failure_classifier=_SETTINGS.failure_classifier,
        )
        assert outcome is not None
        return outcome
    return adopt_worker(
        task=task,
        state=state,
        queue_dir=queue_dir,
        clock=FakeClock(_RESTART),
        settings_caps=_caps(),
        settings_dispatch=settings_dispatch,
        settings_hooks=settings_hooks,
        settings_failure_classifier=_SETTINGS.failure_classifier,
        sleep_fn=lambda _s: None,
    )


@pytest.fixture
def dead_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: False)


@pytest.mark.parametrize("entry", ["exited", "adopted"])
def test_adopted_finalize_counts_a_commit_after_the_recorded_sha(
    queue_dir: Path, worktree: Path, dead_pid: None, entry: str
) -> None:
    """The costliest gap: a worktree task whose only output is a pushed
    commit. With the attempt's pre-dispatch HEAD recorded, the ADR-0020
    gate sees the commit, as the owned finalize does, and the run is
    completed instead of failing end_turn_no_output and re-running."""
    pre_sha = git(worktree, "rev-parse", "HEAD")
    _commit_and_push(worktree, "model.R")

    outcome = _finalize(
        queue_dir, _worktree_task(worktree, deliverable=False), pre_sha=pre_sha, entry=entry
    )

    assert outcome.new_state.status == "completed"
    assert outcome.run_record.stop_reason == "end_turn"
    assert outcome.run_record.error is None
    reloaded = load_state(state_path_for(queue_dir, "030-gates"))
    assert reloaded.status == "completed"
    assert reloaded.pre_dispatch_sha is None
    assert _block_rows(queue_dir) == []


@pytest.mark.parametrize(
    ("deliverable", "status", "stop_reason"),
    [
        pytest.param(True, "completed", "end_turn", id="committed-report"),
        pytest.param(False, "failed", "end_turn_no_output", id="commit-only"),
    ],
)
def test_adopted_finalize_without_a_recorded_sha_keeps_the_legacy_gates(
    queue_dir: Path, worktree: Path, deliverable: bool, status: str, stop_reason: str
) -> None:
    """A state written before ``pre_dispatch_sha`` existed can't show the
    commit. The run is finalized as before: a committed report still
    completes, a commit alone still fails the ADR-0020 gate. And the
    ADR-0033 gate stays off, since it would take the committed report for
    a skip and write a block row."""
    if deliverable:
        _commit_and_push(worktree, "report.md")
    else:
        _commit_and_push(worktree, "model.R")

    outcome = _finalize(queue_dir, _worktree_task(worktree, deliverable=deliverable), pre_sha=None)

    assert outcome.new_state.status == status
    assert outcome.run_record.stop_reason == stop_reason
    assert _block_rows(queue_dir) == []


def test_adopted_finalize_writes_the_terminal_close_row(queue_dir: Path, worktree: Path) -> None:
    """A terminal close (a report, no commit, a clean worktree) writes the
    ADR-0033 block row on the adopted finalize, as on the owned one, and
    the run stays completed."""
    pre_sha = git(worktree, "rev-parse", "HEAD")
    (worktree / "report.md").write_text("skipped: not a model paper\n")

    outcome = _finalize(queue_dir, _worktree_task(worktree, deliverable=True), pre_sha=pre_sha)

    assert outcome.new_state.status == "completed"
    rows = _block_rows(queue_dir)
    assert [(row["task"], row["block_dispatch"]) for row in rows] == [("030-gates", True)]


@pytest.mark.parametrize(
    ("pre_sha_known", "commit", "status", "stop_reason", "refile_count"),
    [
        pytest.param(
            True,
            False,
            "failed_circuit_breaker",
            "sidecar_refile_loop",
            _REFILE_THRESHOLD,
            id="no-progress-trips-the-guard",
        ),
        pytest.param(True, True, "awaiting_sidecar", "end_turn", 0, id="a-commit-resets-it"),
        pytest.param(
            False,
            False,
            "awaiting_sidecar",
            "end_turn",
            _REFILE_THRESHOLD - 1,
            id="legacy-state-not-counted",
        ),
    ],
)
def test_adopted_finalize_applies_the_sidecar_refile_guard(
    queue_dir: Path,
    worktree: Path,
    pre_sha_known: bool,
    commit: bool,
    status: str,
    stop_reason: str,
    refile_count: int,
) -> None:
    """ADR-0027 on the adopted finalize. One re-file short of the threshold,
    a run that files another sidecar without committing trips the guard,
    and one that committed resets the count. A legacy state can't show
    the commit, so its sidecar is parked uncounted, as before."""
    pre_sha = git(worktree, "rev-parse", "HEAD")
    if commit:
        _commit_and_push(worktree, "model.R")
    task = _worktree_task(worktree, deliverable=False)
    _open_sidecar(queue_dir, task.id)

    outcome = _finalize(
        queue_dir,
        task,
        pre_sha=pre_sha if pre_sha_known else None,
        refile_count=_REFILE_THRESHOLD - 1,
    )

    reloaded = load_state(state_path_for(queue_dir, task.id))
    assert (reloaded.status, reloaded.stop_reason, reloaded.sidecar_refile_count) == (
        status,
        stop_reason,
        refile_count,
    )
    assert reloaded == outcome.new_state


def _marker_hook(marker: Path) -> HookSettings:
    return _SETTINGS.hooks.model_copy(
        update={
            "post_dispatch_command": (
                f'shell:printf "%s %s %s" "$TASK_ID" "$ATTEMPT" "$SESSION_ID" > {marker}'
            )
        }
    )


@pytest.mark.parametrize("entry", ["exited", "adopted"])
def test_adopted_finalize_runs_the_post_dispatch_hook(
    queue_dir: Path, tmp_path: Path, dead_pid: None, entry: str
) -> None:
    """The post-dispatch hook runs once the adopted finalize records the
    run, with the attempt's task id, attempt number and session id."""
    marker = tmp_path / "hook-ran"
    task = Task(id="031-hook", title="t", prompt="p", working_dir=None)

    _finalize(queue_dir, task, pre_sha=None, entry=entry, settings_hooks=_marker_hook(marker))

    assert marker.read_text() == "031-hook 1 sess-adopt"


def test_adopted_finalize_runs_the_hook_when_the_guard_stands_down(
    queue_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker has exited even when a concurrent writer finalized the
    task first. The owned path runs the hook once its worker exits, so the
    adopted finalize runs it too, with this attempt's session id, while
    the other writer's record stands."""
    marker = tmp_path / "hook-ran"
    task = Task(id="032-hook-race", title="t", prompt="p", working_dir=None)
    real_load_state = dispatcher_mod.load_state

    def _other_writer_won(path: Path) -> TaskState:
        return real_load_state(path).model_copy(
            update={"status": "failed", "stop_reason": "killed_by_silent_reaper"}
        )

    monkeypatch.setattr(dispatcher_mod, "load_state", _other_writer_won)

    outcome = _finalize(queue_dir, task, pre_sha=None, settings_hooks=_marker_hook(marker))

    assert outcome.new_state.stop_reason == "killed_by_silent_reaper"
    assert marker.read_text() == "032-hook-race 1 sess-adopt"
