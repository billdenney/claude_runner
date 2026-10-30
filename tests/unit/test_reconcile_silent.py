"""Tests for the silent-orphan reaper that runs at supervisor startup.

The reaper differs from :func:`supervisor.reconcile.reconcile_orphans`
in that it consults each in-flight task's heartbeat freshness and
chooses between three outcomes:

* HEALTHY (recent heartbeat / recent start): leave alone for
  ``reconcile_orphans``'s broad demotion sweep.
* SILENT (alert window crossed, kill threshold not exceeded):
  flip to ``possibly_hung``.
* KILL (kill threshold exceeded): terminate the recorded worker's
  process group and flip to ``failed``, or to ``possibly_hung`` when the
  worker survives; a pid that is no longer the worker is not signalled.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.schema import TaskCapsSettings
from claude_task_runner.queue.schema import TaskState
from claude_task_runner.queue.store import (
    load_state,
    queue_runtime_dir,
    state_path_for,
    todo_dir,
    write_state_atomic,
)
from claude_task_runner.runner.dispatcher import WorkerProbe
from claude_task_runner.runner.heartbeat import HeartbeatVerdict
from claude_task_runner.supervisor import reconcile_silent as rs
from claude_task_runner.supervisor.reconcile import LIVE_WORKER_STOP_REASON
from claude_task_runner.supervisor.reconcile_silent import (
    KILL_STOP_REASON,
    SILENT_STOP_REASON,
    reconcile_silent_orphans,
)

from ._worker_probe_stubs import probes


def _queue(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


def _settings(alert: float = 300.0, kill: float = 0.0) -> TaskCapsSettings:
    return TaskCapsSettings(
        max_tokens_per_task=0,
        max_duration_s_per_task=0,
        heartbeat_silence_alert_s=alert,
        heartbeat_silence_kill_s=kill,
    )


def _now() -> datetime:
    return datetime(2026, 6, 9, 12, 0, tzinfo=UTC)


def _seed_running(
    qd: Path,
    task_id: str,
    *,
    started_at: datetime,
    last_heartbeat_at: datetime | None = None,
    pid: int | None = None,
    session_id: str | None = None,
) -> None:
    state = TaskState(
        task_id=task_id,
        status="running",
        last_started_at=started_at,
        last_heartbeat_at=last_heartbeat_at,
        pid=pid,
        session_id=session_id,
    )
    write_state_atomic(state, state_path_for(qd, task_id))


# ---------------------------------------------------------------------------
# SILENT: alert window crossed but kill threshold not exceeded
# ---------------------------------------------------------------------------


def test_silent_running_flipped_to_possibly_hung(tmp_path: Path) -> None:
    qd = _queue(tmp_path)
    # started_at is 600s ago; alert is 300s; kill is 0 (disabled).
    started = _now() - timedelta(seconds=600)
    _seed_running(qd, "t-silent", started_at=started, pid=4321, session_id="sess-1")

    clock = FakeClock(_now())
    results = reconcile_silent_orphans(
        qd, settings=_settings(alert=300), clock=clock, worker_probe_fn=probes(WorkerProbe.DEAD)
    )

    assert len(results) == 1
    r = results[0]
    assert r.task_id == "t-silent"
    assert r.verdict is HeartbeatVerdict.SILENT
    assert r.silence_s == 600.0
    assert r.pid == 4321
    assert r.sigtermed is False

    reloaded = load_state(state_path_for(qd, "t-silent"))
    assert reloaded.status == "possibly_hung"
    assert reloaded.stop_reason == SILENT_STOP_REASON
    assert reloaded.error == (
        "silent_on_restart: no sign of life within heartbeat_silence_alert_s=300 "
        "(agent heartbeat none this attempt, monitor tick none this attempt, worker "
        "log write none this attempt); its worker pid=4321 is gone"
    )
    # pid cleared on demotion; the error names it.
    assert reloaded.pid is None
    # session_id preserved for potential operator-driven recovery.
    assert reloaded.session_id == "sess-1"


def test_silent_does_not_call_sigterm(tmp_path: Path) -> None:
    """SILENT verdict must NOT signal the subprocess — the briefing
    explicitly leaves that decision to the operator."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=600)
    _seed_running(qd, "t-silent", started_at=started, pid=4321)

    calls: list[int] = []

    def recorder(pid: int) -> bool:
        calls.append(pid)
        return True

    reconcile_silent_orphans(
        qd,
        settings=_settings(alert=300, kill=0),
        clock=FakeClock(_now()),
        terminate_fn=recorder,
        worker_probe_fn=probes(WorkerProbe.ALIVE),
    )

    assert calls == []


# ---------------------------------------------------------------------------
# KILL: silence exceeds the kill threshold
# ---------------------------------------------------------------------------


def test_kill_threshold_exceeded_sigterms_and_fails(tmp_path: Path) -> None:
    qd = _queue(tmp_path)
    # Silence of 1000s; alert=300, kill=900 → KILL.
    started = _now() - timedelta(seconds=1000)
    _seed_running(qd, "t-kill", started_at=started, pid=9999, session_id="sess-k")

    calls: list[int] = []

    def recorder(pid: int) -> bool:
        calls.append(pid)
        return True

    results = reconcile_silent_orphans(
        qd,
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
        terminate_fn=recorder,
        worker_probe_fn=probes(WorkerProbe.ALIVE),
    )

    assert calls == [9999]
    assert len(results) == 1
    r = results[0]
    assert r.verdict is HeartbeatVerdict.KILL
    assert r.silence_s == 1000.0
    assert r.pid == 9999
    assert r.sigtermed is True

    reloaded = load_state(state_path_for(qd, "t-kill"))
    assert reloaded.status == "failed"
    assert reloaded.stop_reason == KILL_STOP_REASON
    assert reloaded.error is not None
    assert "orphaned-restart-reap" in reloaded.error
    assert "1000" in reloaded.error
    assert "900" in reloaded.error
    assert reloaded.pid is None
    # session_id preserved so the orchestrator can re-dispatch via
    # session resume.
    assert reloaded.session_id == "sess-k"


def test_kill_with_no_pid_still_fails_but_no_sigterm(tmp_path: Path) -> None:
    """A KILL verdict on a state YAML lacking a pid (pre-PR migration,
    or a state written before Popen) still flips to failed — the kill
    attempt is just skipped."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=1000)
    _seed_running(qd, "t-no-pid", started_at=started, pid=None)

    calls: list[int] = []

    def recorder(pid: int) -> bool:
        calls.append(pid)
        return True

    results = reconcile_silent_orphans(
        qd,
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
        terminate_fn=recorder,
        worker_probe_fn=probes(WorkerProbe.ALIVE),
    )

    assert calls == []
    assert len(results) == 1
    assert results[0].sigtermed is False

    reloaded = load_state(state_path_for(qd, "t-no-pid"))
    assert reloaded.status == "failed"
    assert reloaded.stop_reason == KILL_STOP_REASON


def test_kill_unconfirmed_but_worker_gone_still_fails(tmp_path: Path) -> None:
    """terminate_fn cannot confirm the kill, but the probe after it finds
    the worker gone: the state flips to failed, with sigtermed False."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=1000)
    _seed_running(qd, "t-gone", started_at=started, pid=9999)

    def unconfirmed(_pid: int) -> bool:
        return False

    results = reconcile_silent_orphans(
        qd,
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
        terminate_fn=unconfirmed,
        worker_probe_fn=probes(WorkerProbe.ALIVE, WorkerProbe.DEAD),
    )

    assert len(results) == 1
    assert results[0].sigtermed is False
    assert load_state(state_path_for(qd, "t-gone")).status == "failed"


def test_kill_worker_that_survives_is_parked_not_failed(tmp_path: Path) -> None:
    """A worker still running after the terminate is parked possibly_hung,
    keeping its pid: a failed task would be dispatched again beside it."""
    qd = _queue(tmp_path)
    _seed_running(qd, "t-survivor", started_at=_now() - timedelta(seconds=1000), pid=9999)
    calls: list[int] = []

    def unconfirmed(pid: int) -> bool:
        calls.append(pid)
        return False

    results = reconcile_silent_orphans(
        qd,
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
        terminate_fn=unconfirmed,
        worker_probe_fn=probes(WorkerProbe.ALIVE),
    )

    assert calls == [9999]
    assert [(r.task_id, r.verdict, r.pid, r.sigtermed) for r in results] == [
        ("t-survivor", HeartbeatVerdict.KILL, 9999, False)
    ]
    reloaded = load_state(state_path_for(qd, "t-survivor"))
    assert reloaded.status == "possibly_hung"
    assert reloaded.stop_reason == LIVE_WORKER_STOP_REASON
    assert reloaded.pid == 9999
    assert reloaded.error == (
        "worker pid=9999 is still running after orphaned-restart-reap terminated it "
        "for 1000s silence (heartbeat_silence_kill_s=900); parked so the task is not "
        "dispatched again while it runs"
    )


@pytest.mark.parametrize("probe", [WorkerProbe.DEAD, WorkerProbe.REUSED])
def test_kill_does_not_signal_a_pid_that_is_not_the_worker(
    tmp_path: Path, probe: WorkerProbe
) -> None:
    """A pid with no process, or one another process was given after the
    worker exited, is never signalled; the worker is gone, so the task fails."""
    qd = _queue(tmp_path)
    _seed_running(qd, "t-reused", started_at=_now() - timedelta(seconds=1000), pid=9999)
    calls: list[int] = []

    def recorder(pid: int) -> bool:
        calls.append(pid)
        return True

    results = reconcile_silent_orphans(
        qd,
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
        terminate_fn=recorder,
        worker_probe_fn=probes(probe),
    )

    assert calls == []
    assert [(r.verdict, r.sigtermed) for r in results] == [(HeartbeatVerdict.KILL, False)]
    reloaded = load_state(state_path_for(qd, "t-reused"))
    assert reloaded.status == "failed"
    assert reloaded.stop_reason == KILL_STOP_REASON
    assert reloaded.pid is None


# ---------------------------------------------------------------------------
# HEALTHY: silence within alert window
# ---------------------------------------------------------------------------


def test_healthy_running_left_alone(tmp_path: Path) -> None:
    """A task whose dispatch started within the alert window is
    HEALTHY — the reaper leaves it alone for reconcile_orphans's
    broad demotion sweep."""
    qd = _queue(tmp_path)
    # 60s silence; alert is 300 → HEALTHY.
    started = _now() - timedelta(seconds=60)
    _seed_running(qd, "t-fresh", started_at=started, pid=7777, session_id="sess-h")

    calls: list[int] = []

    def recorder(pid: int) -> bool:
        calls.append(pid)
        return True

    results = reconcile_silent_orphans(
        qd,
        settings=_settings(alert=300, kill=0),
        clock=FakeClock(_now()),
        terminate_fn=recorder,
        worker_probe_fn=probes(WorkerProbe.ALIVE),
    )

    assert results == []
    assert calls == []

    reloaded = load_state(state_path_for(qd, "t-fresh"))
    # Untouched — status is still "running", pid still recorded.
    assert reloaded.status == "running"
    assert reloaded.pid == 7777
    assert reloaded.session_id == "sess-h"


# ---------------------------------------------------------------------------
# Heartbeat baseline correction
# ---------------------------------------------------------------------------


def test_stale_prior_heartbeat_does_not_inflate_silence(
    tmp_path: Path, live_worker_pid: int
) -> None:
    """A last_heartbeat_at that's older than last_started_at is from
    a PREVIOUS run (the dispatcher's finalization write). Naively
    passing it into evaluate() would compute silence from that stale
    timestamp and falsely flag a fresh-started task as SILENT/KILL.

    The reaper guards against this by treating any prior-run
    heartbeat as if no heartbeat had landed this attempt."""
    qd = _queue(tmp_path)
    # Current attempt started 60s ago; PREVIOUS run finished 1 day ago.
    started = _now() - timedelta(seconds=60)
    stale_hb = _now() - timedelta(days=1)
    _seed_running(
        qd, "t-stale-hb", started_at=started, last_heartbeat_at=stale_hb, pid=live_worker_pid
    )

    results = reconcile_silent_orphans(
        qd,
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
    )

    # Should be HEALTHY (silence=60s), so no results.
    assert results == []
    assert load_state(state_path_for(qd, "t-stale-hb")).status == "running"


# ---------------------------------------------------------------------------
# Non-targets
# ---------------------------------------------------------------------------


def test_non_running_states_skipped(tmp_path: Path) -> None:
    qd = _queue(tmp_path)
    # All non-running statuses should be ignored even if their
    # timestamps look ancient.
    ancient_started = _now() - timedelta(days=7)
    for status in ("pending", "failed", "completed", "awaiting_sidecar", "possibly_hung"):
        state = TaskState(
            task_id=f"t-{status}",
            status=status,  # type: ignore[arg-type]
            last_started_at=ancient_started,
        )
        write_state_atomic(state, state_path_for(qd, f"t-{status}"))

    results = reconcile_silent_orphans(
        qd,
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
    )

    assert results == []
    # Each survives unchanged.
    for status in ("pending", "failed", "completed", "awaiting_sidecar", "possibly_hung"):
        reloaded = load_state(state_path_for(qd, f"t-{status}"))
        assert reloaded.status == status


def test_running_without_started_at_skipped(tmp_path: Path) -> None:
    """A status=running with no last_started_at can't be evaluated
    — leave it for reconcile_orphans's broad demotion sweep."""
    qd = _queue(tmp_path)
    state = TaskState(task_id="t-orphan", status="running", last_started_at=None)
    write_state_atomic(state, state_path_for(qd, "t-orphan"))

    results = reconcile_silent_orphans(
        qd,
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
    )

    assert results == []
    assert load_state(state_path_for(qd, "t-orphan")).status == "running"


def test_unparseable_state_file_skipped(tmp_path: Path, live_worker_pid: int) -> None:
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=1000)
    _seed_running(qd, "t-good", started_at=started, pid=live_worker_pid)

    bad = qd / ".claude_task_runner" / "state" / "bad.yaml"
    bad.write_text("not yaml: ][", encoding="utf-8")

    results = reconcile_silent_orphans(
        qd,
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
    )

    # The good orphan got reaped; the bad file was skipped without
    # crashing.
    assert len(results) == 1
    assert results[0].task_id == "t-good"


# ---------------------------------------------------------------------------
# Multiple in-flight tasks: mixed verdicts
# ---------------------------------------------------------------------------


def test_mixed_verdicts_in_one_pass(tmp_path: Path, live_worker_pid: int) -> None:
    qd = _queue(tmp_path)
    _seed_running(
        qd, "t-fresh", started_at=_now() - timedelta(seconds=60), pid=live_worker_pid
    )  # HEALTHY
    _seed_running(qd, "t-silent", started_at=_now() - timedelta(seconds=500), pid=2)  # SILENT
    _seed_running(qd, "t-kill", started_at=_now() - timedelta(seconds=1500), pid=3)  # KILL

    calls: list[int] = []

    def recorder(pid: int) -> bool:
        calls.append(pid)
        return True

    results = reconcile_silent_orphans(
        qd,
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
        terminate_fn=recorder,
        worker_probe_fn=probes(WorkerProbe.ALIVE),
    )

    # HEALTHY produces no result; SILENT and KILL each produce one.
    by_id = {r.task_id: r for r in results}
    assert set(by_id) == {"t-silent", "t-kill"}
    assert by_id["t-silent"].verdict is HeartbeatVerdict.SILENT
    assert by_id["t-kill"].verdict is HeartbeatVerdict.KILL

    # Only the KILL pid was signaled.
    assert calls == [3]

    assert load_state(state_path_for(qd, "t-fresh")).status == "running"
    assert load_state(state_path_for(qd, "t-silent")).status == "possibly_hung"
    assert load_state(state_path_for(qd, "t-kill")).status == "failed"


# ---------------------------------------------------------------------------
# The worker log's mtime as a sign of life
# ---------------------------------------------------------------------------


_LOG_NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def test_log_written_at_is_the_log_mtime(tmp_path: Path) -> None:
    log = tmp_path / "attempt-1.stream.jsonl"
    log.write_text("{}\n")
    os.utime(log, (1_790_000_000, 1_790_000_000))
    state = TaskState(task_id="t-log", status="running", log_path=str(log))

    assert rs._log_written_at(state, _LOG_NOW) == datetime(2026, 9, 21, 14, 13, 20, tzinfo=UTC)


def test_a_log_mtime_dated_after_now_is_no_sign_of_life(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A log dated after now (a bogus touch, a clock step) would keep a hung
    worker looking alive until that date. A minute ahead still counts: the
    log may be written while a reaper pass runs. Only the first read warns."""
    log = tmp_path / "attempt-1.stream.jsonl"
    log.write_text("{}\n")
    state = TaskState(task_id="t-log", status="running", log_path=str(log))
    in_a_minute = _LOG_NOW + timedelta(seconds=60)
    os.utime(log, (in_a_minute.timestamp(), in_a_minute.timestamp()))
    assert rs._log_written_at(state, _LOG_NOW) == in_a_minute

    in_2100 = datetime(2100, 1, 1, tzinfo=UTC)
    os.utime(log, (in_2100.timestamp(), in_2100.timestamp()))
    with caplog.at_level("WARNING", logger=rs.logger.name):
        assert [rs._log_written_at(state, _LOG_NOW), rs._log_written_at(state, _LOG_NOW)] == [
            None,
            None,
        ]

    assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == [
        f"{log} is dated 2100-01-01T00:00:00+00:00, after now (2026-10-30T12:00:00+00:00); "
        "the silent-orphan reaper does not count it as a sign of life for t-log"
    ]


def test_a_log_mtime_no_datetime_can_hold_is_no_sign_of_life(
    tmp_path: Path,
    stub_mtime: Callable[[Path, float], None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """tmpfs stores an mtime past year 9999 as given. No datetime can hold
    it, so it counts as no sign of life. The reaper reads the log on every
    pass, and only the first read warns."""
    log = tmp_path / "attempt-1.stream.jsonl"
    stub_mtime(log, 1e12)
    state = TaskState(task_id="t-log", status="running", log_path=str(log))

    with caplog.at_level("WARNING", logger="claude_task_runner.file_mtime"):
        assert [rs._log_written_at(state, _LOG_NOW), rs._log_written_at(state, _LOG_NOW)] == [
            None,
            None,
        ]

    assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == [
        f"{log} has an mtime no datetime can hold (timestamp 1000000000000.0 is out of range: "
        "year 33658 is out of range); the silent-orphan reaper does not count it as a sign "
        "of life for t-log"
    ]
