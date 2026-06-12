"""Tests for the steady-state per-tick silent-orphan reaper.

Companion to ``test_reconcile_silent.py`` (the startup pass). The
per-tick pass differs in three ways:

* It is scoped to ``in_flight_task_ids`` from the orchestrator's slot
  map — state YAMLs for unrelated tasks are skipped without inspection.
* It writes a different ``stop_reason`` on SILENT-verdict demotions
  (``STEADY_SILENT_STOP_REASON``) so the audit trail distinguishes
  restart-orphans from supervisor-live wedges. KILL keeps
  :data:`KILL_STOP_REASON` (semantic identity).
* It re-reads the state immediately before writing the demotion and
  skips if the dispatcher has finalized between the verdict and the
  write — TOCTOU mitigation for the live-dispatcher case.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

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
from claude_task_runner.runner.heartbeat import HeartbeatVerdict
from claude_task_runner.supervisor.reconcile_silent import (
    KILL_STOP_REASON,
    STEADY_SILENT_STOP_REASON,
    reap_silent_orphans_tick,
)


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
    return datetime(2026, 6, 12, 12, 0, tzinfo=UTC)


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
# HEALTHY: fresh heartbeat → no state change
# ---------------------------------------------------------------------------


def test_fresh_heartbeat_no_change(tmp_path: Path) -> None:
    """A task with a recent ``last_heartbeat_at`` is HEALTHY and the
    per-tick pass leaves it alone. This is the most common case — the
    dispatcher persists heartbeats every ``heartbeat_persist_interval_s``
    so the reaper sees fresh liveness on every tick."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=3600)  # 1h-running task
    fresh_hb = _now() - timedelta(seconds=20)  # last event ~20s ago
    _seed_running(qd, "t-healthy", started_at=started, last_heartbeat_at=fresh_hb, pid=1234)

    calls: list[int] = []

    def recorder(pid: int) -> bool:
        calls.append(pid)
        return True

    results = reap_silent_orphans_tick(
        qd,
        {"t-healthy"},
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
        sigterm_fn=recorder,
    )

    assert results == []
    assert calls == []
    assert load_state(state_path_for(qd, "t-healthy")).status == "running"


# ---------------------------------------------------------------------------
# SILENT: alert window crossed but kill threshold not exceeded
# ---------------------------------------------------------------------------


def test_silent_flips_to_possibly_hung_with_steady_stop_reason(tmp_path: Path) -> None:
    """A subprocess that hasn't emitted an event in longer than the
    alert threshold is SILENT. The per-tick pass marks it
    ``possibly_hung`` with :data:`STEADY_SILENT_STOP_REASON`, distinct
    from the startup pass's :data:`SILENT_STOP_REASON` so operators
    can tell the two failure modes apart in audit logs."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=3600)
    # Last heartbeat 500s ago; alert=300, kill=900 → SILENT.
    stale_hb = _now() - timedelta(seconds=500)
    _seed_running(
        qd,
        "t-silent",
        started_at=started,
        last_heartbeat_at=stale_hb,
        pid=4321,
        session_id="sess-1",
    )

    calls: list[int] = []

    def recorder(pid: int) -> bool:
        calls.append(pid)
        return True

    results = reap_silent_orphans_tick(
        qd,
        {"t-silent"},
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
        sigterm_fn=recorder,
    )

    assert len(results) == 1
    r = results[0]
    assert r.task_id == "t-silent"
    assert r.verdict is HeartbeatVerdict.SILENT
    assert r.silence_s == 500.0
    assert r.pid == 4321
    assert r.sigtermed is False

    reloaded = load_state(state_path_for(qd, "t-silent"))
    assert reloaded.status == "possibly_hung"
    assert reloaded.stop_reason == STEADY_SILENT_STOP_REASON
    assert reloaded.pid is None
    assert reloaded.session_id == "sess-1"

    # SILENT must not signal — same policy as the startup pass.
    assert calls == []


# ---------------------------------------------------------------------------
# KILL: silence exceeds the kill threshold → SIGTERM + failed
# ---------------------------------------------------------------------------


def test_kill_threshold_exceeded_sigterms_and_fails(tmp_path: Path) -> None:
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=3600)
    # 1000s of silence; alert=300, kill=900 → KILL.
    stale_hb = _now() - timedelta(seconds=1000)
    _seed_running(
        qd,
        "t-kill",
        started_at=started,
        last_heartbeat_at=stale_hb,
        pid=9999,
        session_id="sess-k",
    )

    calls: list[int] = []

    def recorder(pid: int) -> bool:
        calls.append(pid)
        return True

    results = reap_silent_orphans_tick(
        qd,
        {"t-kill"},
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
        sigterm_fn=recorder,
    )

    assert calls == [9999]
    assert len(results) == 1
    r = results[0]
    assert r.verdict is HeartbeatVerdict.KILL
    assert r.silence_s == 1000.0
    assert r.sigtermed is True

    reloaded = load_state(state_path_for(qd, "t-kill"))
    assert reloaded.status == "failed"
    assert reloaded.stop_reason == KILL_STOP_REASON
    # Distinct error-prefix from the startup pass keeps the audit trail
    # split between the two failure modes.
    assert reloaded.error is not None
    assert "silent-steady-state-reap" in reloaded.error
    assert reloaded.pid is None
    assert reloaded.session_id == "sess-k"


# ---------------------------------------------------------------------------
# Scope: tasks NOT in the orchestrator's slot map are skipped
# ---------------------------------------------------------------------------


def test_state_yaml_not_in_in_flight_is_skipped(tmp_path: Path) -> None:
    """The per-tick pass only acts on tasks the orchestrator currently
    has live slots for. A state YAML left over from a different
    supervisor run (or a hand-edited test fixture) is ignored even if
    its heartbeat is grossly stale.

    This is the key guard preventing the per-tick pass from re-
    triggering on orphans the startup pass already handled."""
    qd = _queue(tmp_path)
    # Stale enough to trip KILL if it were in scope.
    started = _now() - timedelta(seconds=3600)
    stale_hb = _now() - timedelta(seconds=2000)
    _seed_running(qd, "t-out-of-scope", started_at=started, last_heartbeat_at=stale_hb, pid=1111)

    # in_flight set is empty — the orchestrator doesn't own this slot.
    results = reap_silent_orphans_tick(
        qd,
        set(),
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
    )

    assert results == []
    # Untouched.
    assert load_state(state_path_for(qd, "t-out-of-scope")).status == "running"


def test_only_in_flight_tasks_evaluated(tmp_path: Path) -> None:
    """Two state YAMLs, one in the in-flight set and one not. Only the
    in-flight one is graded — even though both have stale heartbeats."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=3600)
    stale_hb = _now() - timedelta(seconds=1500)
    _seed_running(qd, "t-tracked", started_at=started, last_heartbeat_at=stale_hb, pid=1)
    _seed_running(qd, "t-untracked", started_at=started, last_heartbeat_at=stale_hb, pid=2)

    calls: list[int] = []

    def recorder(pid: int) -> bool:
        calls.append(pid)
        return True

    results = reap_silent_orphans_tick(
        qd,
        {"t-tracked"},
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
        sigterm_fn=recorder,
    )

    assert {r.task_id for r in results} == {"t-tracked"}
    assert calls == [1]
    assert load_state(state_path_for(qd, "t-tracked")).status == "failed"
    assert load_state(state_path_for(qd, "t-untracked")).status == "running"


# ---------------------------------------------------------------------------
# TOCTOU guard: dispatcher finalizes between read and write → no clobber
# ---------------------------------------------------------------------------


def test_recheck_skips_demote_if_dispatcher_finalized(tmp_path: Path) -> None:
    """If the dispatch thread finalized the task between the reaper's
    verdict computation and its write, the reaper must NOT overwrite
    the dispatcher's authoritative state.

    Simulated by injecting a sigterm_fn that, when called, writes a
    "completed" state to the YAML — emulating a dispatcher thread that
    happened to finalize right after the reaper SIGTERM-ed. The
    reaper's pre-write recheck must see status != "running" and skip
    the demotion."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=3600)
    stale_hb = _now() - timedelta(seconds=1500)
    _seed_running(qd, "t-race", started_at=started, last_heartbeat_at=stale_hb, pid=5)

    def racy_sigterm(pid: int) -> bool:
        # Dispatcher finalizes between the reaper's evaluate and write.
        finalized = TaskState(
            task_id="t-race",
            status="completed",
            stop_reason="end_turn",
            last_started_at=started,
            last_heartbeat_at=_now() - timedelta(seconds=5),
        )
        write_state_atomic(finalized, state_path_for(qd, "t-race"))
        return True

    results = reap_silent_orphans_tick(
        qd,
        {"t-race"},
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
        sigterm_fn=racy_sigterm,
    )

    # The sigterm fired (returns True is recorded internally) but the
    # reaper's recheck saw "completed" and bailed out — so no ReapResult
    # is produced for this task.
    assert results == []
    reloaded = load_state(state_path_for(qd, "t-race"))
    assert reloaded.status == "completed"
    assert reloaded.stop_reason == "end_turn"


# ---------------------------------------------------------------------------
# Baseline correction: a prior-run heartbeat (older than last_started_at)
# falls back to last_started_at
# ---------------------------------------------------------------------------


def test_baseline_correction_uses_started_at(tmp_path: Path) -> None:
    """When ``last_heartbeat_at`` predates ``last_started_at`` (the
    stale-prior-run case the 680-yu_2017 zombie exhibited), the reaper
    falls back to ``last_started_at`` for the silence baseline. A task
    started 1000s ago with a heartbeat from yesterday should be graded
    on 1000s of silence, not 86400s."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=1000)
    # Heartbeat is from a previous (finished) run, before this attempt.
    prior_run_hb = _now() - timedelta(days=2)
    _seed_running(
        qd,
        "t-base",
        started_at=started,
        last_heartbeat_at=prior_run_hb,
        pid=7,
    )

    results = reap_silent_orphans_tick(
        qd,
        {"t-base"},
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
        sigterm_fn=lambda _pid: True,
    )

    # Silence is now-started_at = 1000s. With kill=900, this is KILL,
    # not "two days of silence" → still KILL but with the correct
    # silence_s value reflecting the current attempt only.
    assert len(results) == 1
    assert results[0].verdict is HeartbeatVerdict.KILL
    assert results[0].silence_s == 1000.0


def test_no_heartbeat_uses_started_at(tmp_path: Path) -> None:
    """When ``last_heartbeat_at`` is None entirely (a fresh dispatch
    that hasn't yet emitted any event), silence is measured from
    ``last_started_at`` directly. Mirrors the heartbeat module's
    documented baseline behaviour."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=120)
    _seed_running(qd, "t-cold", started_at=started, last_heartbeat_at=None, pid=8)

    results = reap_silent_orphans_tick(
        qd,
        {"t-cold"},
        settings=_settings(alert=300, kill=0),
        clock=FakeClock(_now()),
    )

    # 120s < 300s alert → HEALTHY → no results.
    assert results == []


# ---------------------------------------------------------------------------
# SILENT without a kill threshold should mark possibly_hung, not signal
# ---------------------------------------------------------------------------


def test_silent_without_kill_threshold_marks_only(tmp_path: Path) -> None:
    """``heartbeat_silence_kill_s=0`` disables auto-kill. SILENT
    verdicts still mark ``possibly_hung`` for operator visibility but
    never SIGTERM the recorded pid."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=3600)
    stale_hb = _now() - timedelta(seconds=400)
    _seed_running(qd, "t-no-kill", started_at=started, last_heartbeat_at=stale_hb, pid=42)

    calls: list[int] = []

    def recorder(pid: int) -> bool:
        calls.append(pid)
        return True

    results = reap_silent_orphans_tick(
        qd,
        {"t-no-kill"},
        settings=_settings(alert=300, kill=0),
        clock=FakeClock(_now()),
        sigterm_fn=recorder,
    )

    assert len(results) == 1
    assert results[0].verdict is HeartbeatVerdict.SILENT
    assert calls == []

    reloaded = load_state(state_path_for(qd, "t-no-kill"))
    assert reloaded.status == "possibly_hung"
    assert reloaded.stop_reason == STEADY_SILENT_STOP_REASON


# ---------------------------------------------------------------------------
# Two-tick progression: tick 1 healthy, fake-clock advances, tick 2 flips
# ---------------------------------------------------------------------------


def test_two_tick_progression(tmp_path: Path) -> None:
    """First tick: fresh heartbeat → HEALTHY → no change.
    Advance the clock past the alert threshold.
    Second tick: same state YAML, but silence is now stale → SILENT.

    Verifies the per-tick pass is idempotent on HEALTHY tasks and
    flips state cleanly on the tick that crosses the threshold —
    matching the supervisor's tick cadence."""
    qd = _queue(tmp_path)
    started = datetime(2026, 6, 12, 11, 0, tzinfo=UTC)
    fresh_hb = datetime(2026, 6, 12, 11, 55, tzinfo=UTC)
    _seed_running(qd, "t-progress", started_at=started, last_heartbeat_at=fresh_hb, pid=1)

    clock = FakeClock(datetime(2026, 6, 12, 11, 56, tzinfo=UTC))  # +1min, healthy
    settings = _settings(alert=300, kill=0)

    # Tick 1 — silence is 60s, well under 300s alert.
    results = reap_silent_orphans_tick(qd, {"t-progress"}, settings=settings, clock=clock)
    assert results == []
    assert load_state(state_path_for(qd, "t-progress")).status == "running"

    # Advance the clock past the alert threshold. Heartbeat hasn't moved
    # because the subprocess emitted nothing in between.
    clock.advance(400)  # +400s; new now = 11:56 + 6:40 = 12:02:40

    # Tick 2 — silence is now 60s+400s = 460s > 300s alert.
    results = reap_silent_orphans_tick(qd, {"t-progress"}, settings=settings, clock=clock)
    assert len(results) == 1
    assert results[0].verdict is HeartbeatVerdict.SILENT
    assert load_state(state_path_for(qd, "t-progress")).status == "possibly_hung"


# ---------------------------------------------------------------------------
# Non-running status is skipped even when in the in-flight set
# ---------------------------------------------------------------------------


def test_non_running_status_skipped(tmp_path: Path) -> None:
    """A task in the orchestrator's slot map whose state has been
    flipped (e.g. by a previous reaper pass within the same tick — or
    by a concurrent dispatcher finalize) is not double-acted on."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=1500)
    state = TaskState(
        task_id="t-done",
        status="failed",
        last_started_at=started,
        last_heartbeat_at=started + timedelta(seconds=10),
    )
    write_state_atomic(state, state_path_for(qd, "t-done"))

    results = reap_silent_orphans_tick(
        qd,
        {"t-done"},
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
    )

    assert results == []
    assert load_state(state_path_for(qd, "t-done")).status == "failed"
