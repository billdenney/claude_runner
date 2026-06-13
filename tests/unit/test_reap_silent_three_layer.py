"""Tests for the three-layer silent-orphan reaper classification.

Layer 1 (in PR #57, here for context) — the dispatcher persists
``last_heartbeat_at`` per stream-json event, rate-limited.

Layer 2 — the dispatcher's monitor thread persists
``dispatcher_alive_at`` on a fixed cadence regardless of events. The
reaper consults BOTH fields: a fresh ``dispatcher_alive_at`` is
sufficient evidence that the dispatcher is alive, so the task is
HEALTHY even when ``last_heartbeat_at`` is stale (the "agent quiet
during a long Bash subprocess" case).

Layer 3 — when both fields are stale, the reaper performs a one-shot
bounded filesystem walk of the task's ``working_dir``. Recent
``st_mtime`` activity within ``zombie_verify_fs_activity_window_s``
proves the subprocess is doing useful work; the reaper refreshes
``last_heartbeat_at`` from the mtime and treats the task as HEALTHY.
The walk runs ONLY when the cheap signals already suggest a hang —
zero filesystem overhead when everything is healthy.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.schema import TaskCapsSettings
from claude_task_runner.queue.schema import Task, TaskState
from claude_task_runner.queue.store import (
    load_state,
    queue_runtime_dir,
    state_path_for,
    task_path_for,
    todo_dir,
    write_state_atomic,
    write_task_atomic,
)
from claude_task_runner.runner.heartbeat import HeartbeatVerdict
from claude_task_runner.supervisor.reconcile_silent import (
    KILL_STOP_REASON,
    STEADY_SILENT_STOP_REASON,
    _latest_mtime_in_tree,
    reap_silent_orphans_tick,
)


def _queue(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


def _settings(
    *,
    alert: float = 300.0,
    kill: float = 0.0,
    fs_window: float = 600.0,
) -> TaskCapsSettings:
    return TaskCapsSettings(
        max_tokens_per_task=0,
        max_duration_s_per_task=0,
        heartbeat_silence_alert_s=alert,
        heartbeat_silence_kill_s=kill,
        zombie_verify_fs_activity_window_s=fs_window,
    )


def _now() -> datetime:
    return datetime(2026, 6, 13, 12, 0, tzinfo=UTC)


def _seed_task_and_state(
    qd: Path,
    task_id: str,
    *,
    started_at: datetime,
    last_heartbeat_at: datetime | None = None,
    dispatcher_alive_at: datetime | None = None,
    pid: int | None = None,
    working_dir: Path | None = None,
) -> None:
    """Seed both the Task YAML (with working_dir) AND the state YAML
    so the FS verification step can find the worktree."""
    task = Task(
        id=task_id,
        title=task_id,
        prompt="p",
        working_dir=working_dir,
    )
    write_task_atomic(task, task_path_for(qd, task_id))
    state = TaskState(
        task_id=task_id,
        status="running",
        last_started_at=started_at,
        last_heartbeat_at=last_heartbeat_at,
        dispatcher_alive_at=dispatcher_alive_at,
        pid=pid,
    )
    write_state_atomic(state, state_path_for(qd, task_id))


# ---------------------------------------------------------------------------
# Layer 2: dispatcher_alive_at short-circuits SILENT/KILL on a stale last_hb
# ---------------------------------------------------------------------------


def test_dispatcher_alive_fresh_makes_silent_hb_healthy(tmp_path: Path) -> None:
    """Agent emitted nothing for a long time (last_heartbeat_at stale)
    but the dispatcher monitor thread is alive and writing
    ``dispatcher_alive_at`` regularly. The reaper must NOT classify
    this as SILENT — it's a healthy run that happens to be in a long
    Bash subprocess or OAuth refresh, not a zombie."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=3600)
    # 1000s since the last stream-json event — would be KILL alone.
    stale_hb = _now() - timedelta(seconds=1000)
    # Dispatcher monitor wrote 10s ago — task is clearly alive.
    fresh_alive = _now() - timedelta(seconds=10)
    _seed_task_and_state(
        qd,
        "t-quiet-agent",
        started_at=started,
        last_heartbeat_at=stale_hb,
        dispatcher_alive_at=fresh_alive,
        pid=1234,
    )

    results = reap_silent_orphans_tick(
        qd,
        {"t-quiet-agent"},
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
    )

    assert results == []
    assert load_state(state_path_for(qd, "t-quiet-agent")).status == "running"


def test_dispatcher_alive_stale_falls_through_to_hb(tmp_path: Path) -> None:
    """When dispatcher_alive_at is older than the alert threshold, the
    Layer-2 short-circuit doesn't fire — the reaper falls through to
    the normal last_heartbeat_at evaluation (and Layer 3 verification
    when both are stale)."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=3600)
    stale_hb = _now() - timedelta(seconds=1000)
    stale_alive = _now() - timedelta(seconds=900)
    _seed_task_and_state(
        qd,
        "t-both-stale",
        started_at=started,
        last_heartbeat_at=stale_hb,
        dispatcher_alive_at=stale_alive,
        pid=5,
        working_dir=None,  # forces Layer 3 to skip
    )

    results = reap_silent_orphans_tick(
        qd,
        {"t-both-stale"},
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
        sigterm_fn=lambda _pid: True,
    )

    # Both stale + no working_dir = no FS check = act on KILL verdict.
    assert len(results) == 1
    assert results[0].verdict is HeartbeatVerdict.KILL


def test_dispatcher_alive_predates_started_treated_as_none(tmp_path: Path) -> None:
    """A dispatcher_alive_at older than the current attempt's
    last_started_at belongs to a previous (finished) run. The same
    baseline-correction trick used for last_heartbeat_at must apply to
    dispatcher_alive_at, otherwise the Layer-2 short-circuit would
    erroneously fire on a prior-run stale value."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=1000)
    # dispatcher_alive_at from a previous attempt — older than started_at.
    prior_run_alive = _now() - timedelta(days=2)
    _seed_task_and_state(
        qd,
        "t-stale-alive",
        started_at=started,
        last_heartbeat_at=None,
        dispatcher_alive_at=prior_run_alive,
        pid=8,
        working_dir=None,
    )

    results = reap_silent_orphans_tick(
        qd,
        {"t-stale-alive"},
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
        sigterm_fn=lambda _pid: True,
    )

    # Silence = now - started_at = 1000s > kill=900 → KILL.
    # If the prior-run dispatcher_alive_at had been honoured we would
    # have falsely treated this as HEALTHY.
    assert len(results) == 1
    assert results[0].verdict is HeartbeatVerdict.KILL
    assert results[0].silence_s == 1000.0


def test_legacy_state_yaml_without_dispatcher_alive_at(tmp_path: Path) -> None:
    """Pre-Layer-2 state YAMLs don't carry dispatcher_alive_at. The
    reaper falls back to the last_heartbeat_at-only path so an upgrade
    doesn't reap every running task."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=3600)
    fresh_hb = _now() - timedelta(seconds=10)
    _seed_task_and_state(
        qd,
        "t-legacy",
        started_at=started,
        last_heartbeat_at=fresh_hb,
        dispatcher_alive_at=None,  # legacy
        pid=1,
        working_dir=None,
    )

    results = reap_silent_orphans_tick(
        qd,
        {"t-legacy"},
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
    )

    assert results == []
    assert load_state(state_path_for(qd, "t-legacy")).status == "running"


# ---------------------------------------------------------------------------
# Layer 3: filesystem activity verification
# ---------------------------------------------------------------------------


def test_fs_recent_mtime_makes_silent_healthy(tmp_path: Path) -> None:
    """Both heartbeat fields are stale, but the working_dir has fresh
    file modifications — a long-running Bash subprocess writing files
    without emitting stream-json events. The reaper must treat as
    HEALTHY and refresh ``last_heartbeat_at`` from the mtime so the
    next pass starts from a fresh baseline."""
    qd = _queue(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    started = _now() - timedelta(seconds=3600)
    stale_hb = _now() - timedelta(seconds=2000)

    _seed_task_and_state(
        qd,
        "t-busy-fs",
        started_at=started,
        last_heartbeat_at=stale_hb,
        dispatcher_alive_at=None,
        pid=42,
        working_dir=work,
    )

    # Synthetic recent file modification (60s ago — well inside the
    # default 600s FS window).
    fresh_mtime_unix = (_now() - timedelta(seconds=60)).timestamp()

    def stub_mtime(_path: Path) -> float | None:
        return fresh_mtime_unix

    results = reap_silent_orphans_tick(
        qd,
        {"t-busy-fs"},
        settings=_settings(alert=300, kill=900, fs_window=600),
        clock=FakeClock(_now()),
        fs_mtime_fn=stub_mtime,
    )

    assert results == []
    reloaded = load_state(state_path_for(qd, "t-busy-fs"))
    assert reloaded.status == "running"
    # last_heartbeat_at refreshed from the mtime.
    assert reloaded.last_heartbeat_at is not None
    assert reloaded.last_heartbeat_at == datetime.fromtimestamp(fresh_mtime_unix, tz=UTC)


def test_fs_stale_mtime_still_kills(tmp_path: Path) -> None:
    """Both heartbeat fields stale, and the working_dir's latest mtime
    is also outside the FS window — that's a real zombie. The reaper
    proceeds with the SILENT/KILL action."""
    qd = _queue(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    started = _now() - timedelta(seconds=3600)
    stale_hb = _now() - timedelta(seconds=2000)
    _seed_task_and_state(
        qd,
        "t-zombie",
        started_at=started,
        last_heartbeat_at=stale_hb,
        dispatcher_alive_at=None,
        pid=99,
        working_dir=work,
    )

    # Latest mtime is 1h ago — well past the default 600s window.
    stale_mtime_unix = (_now() - timedelta(seconds=3600)).timestamp()

    def stub_mtime(_path: Path) -> float | None:
        return stale_mtime_unix

    calls: list[int] = []

    def recorder(pid: int) -> bool:
        calls.append(pid)
        return True

    results = reap_silent_orphans_tick(
        qd,
        {"t-zombie"},
        settings=_settings(alert=300, kill=900, fs_window=600),
        clock=FakeClock(_now()),
        sigterm_fn=recorder,
        fs_mtime_fn=stub_mtime,
    )

    assert len(results) == 1
    assert results[0].verdict is HeartbeatVerdict.KILL
    assert calls == [99]
    reloaded = load_state(state_path_for(qd, "t-zombie"))
    assert reloaded.status == "failed"
    assert reloaded.stop_reason == KILL_STOP_REASON


def test_fs_check_skipped_when_no_working_dir(tmp_path: Path) -> None:
    """Tasks without a working_dir (research/analysis) skip the FS
    check entirely and proceed with the heartbeat verdict."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=3600)
    stale_hb = _now() - timedelta(seconds=500)
    _seed_task_and_state(
        qd,
        "t-no-workdir",
        started_at=started,
        last_heartbeat_at=stale_hb,
        pid=7,
        working_dir=None,
    )

    fs_calls = {"n": 0}

    def stub_mtime(_path: Path) -> float | None:
        fs_calls["n"] += 1
        return None

    results = reap_silent_orphans_tick(
        qd,
        {"t-no-workdir"},
        settings=_settings(alert=300, kill=0),
        clock=FakeClock(_now()),
        fs_mtime_fn=stub_mtime,
    )

    # Verdict is SILENT (alert=300 < silence=500, kill=0 disabled).
    assert len(results) == 1
    assert results[0].verdict is HeartbeatVerdict.SILENT
    assert results[0].silence_s == 500.0
    # FS check was NOT invoked — no working_dir to walk.
    assert fs_calls["n"] == 0


def test_fs_check_skipped_when_task_yaml_missing(tmp_path: Path) -> None:
    """If the Task YAML is missing (the supervisor's state map outran
    the on-disk todo dir, or the operator deleted it), the FS check
    silently skips and the reaper proceeds with the heartbeat verdict.
    Don't crash on a stale state YAML pointing at no task."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=3600)
    stale_hb = _now() - timedelta(seconds=500)
    # Seed the STATE only; no Task YAML.
    state = TaskState(
        task_id="t-orphan",
        status="running",
        last_started_at=started,
        last_heartbeat_at=stale_hb,
        pid=3,
    )
    write_state_atomic(state, state_path_for(qd, "t-orphan"))

    fs_calls = {"n": 0}

    def stub_mtime(_path: Path) -> float | None:
        fs_calls["n"] += 1
        return None

    results = reap_silent_orphans_tick(
        qd,
        {"t-orphan"},
        settings=_settings(alert=300, kill=0),
        clock=FakeClock(_now()),
        fs_mtime_fn=stub_mtime,
    )

    assert len(results) == 1
    assert results[0].verdict is HeartbeatVerdict.SILENT
    assert fs_calls["n"] == 0


def test_fs_check_skipped_when_function_raises(tmp_path: Path) -> None:
    """An exception from the FS walk (permission error walking a
    foreign worktree, etc.) is swallowed — proceed with the
    heartbeat verdict rather than crashing the whole reaper pass."""
    qd = _queue(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    started = _now() - timedelta(seconds=3600)
    stale_hb = _now() - timedelta(seconds=500)
    _seed_task_and_state(
        qd,
        "t-fs-error",
        started_at=started,
        last_heartbeat_at=stale_hb,
        pid=4,
        working_dir=work,
    )

    def boom(_path: Path) -> float | None:
        raise OSError("permission denied")

    results = reap_silent_orphans_tick(
        qd,
        {"t-fs-error"},
        settings=_settings(alert=300, kill=0),
        clock=FakeClock(_now()),
        fs_mtime_fn=boom,
    )

    assert len(results) == 1
    assert results[0].verdict is HeartbeatVerdict.SILENT


def test_fs_check_not_invoked_when_dispatcher_alive_fresh(tmp_path: Path) -> None:
    """Layer 3 must be gated on the Layer-2 short-circuit failing.
    When ``dispatcher_alive_at`` is fresh, the reaper returns HEALTHY
    without ever running the filesystem walk — zero FS overhead in
    the common case."""
    qd = _queue(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    started = _now() - timedelta(seconds=3600)
    stale_hb = _now() - timedelta(seconds=500)
    fresh_alive = _now() - timedelta(seconds=15)
    _seed_task_and_state(
        qd,
        "t-cheap-healthy",
        started_at=started,
        last_heartbeat_at=stale_hb,
        dispatcher_alive_at=fresh_alive,
        pid=1,
        working_dir=work,
    )

    fs_calls = {"n": 0}

    def stub_mtime(_path: Path) -> float | None:
        fs_calls["n"] += 1
        return None

    results = reap_silent_orphans_tick(
        qd,
        {"t-cheap-healthy"},
        settings=_settings(alert=300, kill=0, fs_window=600),
        clock=FakeClock(_now()),
        fs_mtime_fn=stub_mtime,
    )

    assert results == []
    assert fs_calls["n"] == 0  # FS check never invoked.


# ---------------------------------------------------------------------------
# Helper: _latest_mtime_in_tree on a real filesystem
# ---------------------------------------------------------------------------


def test_latest_mtime_finds_recent_file(tmp_path: Path) -> None:
    """The bounded walk returns the most recent mtime in the tree."""
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.txt").write_text("hello")
    sub = root / "subdir"
    sub.mkdir()
    (sub / "b.txt").write_text("world")

    latest = _latest_mtime_in_tree(root)
    assert latest is not None
    # Both files were just written; the latest mtime should be at least
    # as recent as the last write.
    assert latest > 0


def test_latest_mtime_skips_noisy_dirs(tmp_path: Path) -> None:
    """``.git`` (and friends) must be skipped — their mtimes don't
    correlate with the agent's activity."""
    import os as _os
    import time as _time

    root = tmp_path / "tree"
    root.mkdir()
    real_file = root / "report.md"
    real_file.write_text("real work")
    real_mtime = real_file.stat().st_mtime

    # Simulate a much-newer .git mtime (a VCS-internal write the agent
    # didn't do).
    git = root / ".git"
    git.mkdir()
    noisy = git / "HEAD"
    noisy.write_text("ref: refs/heads/main")
    newer = real_mtime + 1000
    _os.utime(noisy, (newer, newer))
    _ = _time  # quiet pyflakes when run on an older test selector

    latest = _latest_mtime_in_tree(root)
    assert latest is not None
    # The .git mtime is bigger but was skipped; the latest visible
    # mtime is the report's.
    assert latest < newer


def test_latest_mtime_handles_missing_root() -> None:
    """A nonexistent root returns None rather than raising."""
    assert _latest_mtime_in_tree(Path("/no/such/path/exists/here")) is None


def test_latest_mtime_handles_max_depth(tmp_path: Path) -> None:
    """Files beyond ``max_depth`` levels are invisible to the walk."""
    import os as _os

    root = tmp_path / "tree"
    deep = root / "a" / "b" / "c" / "d" / "e" / "f"
    deep.mkdir(parents=True)
    too_deep = deep / "buried.txt"
    too_deep.write_text("very deep")

    shallow = root / "top.txt"
    shallow.write_text("on top")
    shallow_mtime = shallow.stat().st_mtime

    # Set the deep file's mtime far in the future so we can prove it
    # was ignored.
    far_future = shallow_mtime + 1_000_000
    _os.utime(too_deep, (far_future, far_future))

    # With max_depth=2 the deeply-buried file is invisible.
    latest = _latest_mtime_in_tree(root, max_depth=2)
    assert latest is not None
    assert latest < far_future


# ---------------------------------------------------------------------------
# Composition: Layer 2 dispatcher_alive_fresh wins over an old mtime
# ---------------------------------------------------------------------------


def test_dispatcher_alive_wins_even_when_fs_is_stale(tmp_path: Path) -> None:
    """If the cheap signal (dispatcher_alive_at fresh) says HEALTHY,
    no FS walk happens — even when the worktree mtime would have been
    stale. Proves the layered order: cheap signals first."""
    qd = _queue(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    started = _now() - timedelta(seconds=3600)
    stale_hb = _now() - timedelta(seconds=2000)
    fresh_alive = _now() - timedelta(seconds=15)
    _seed_task_and_state(
        qd,
        "t-cheap-wins",
        started_at=started,
        last_heartbeat_at=stale_hb,
        dispatcher_alive_at=fresh_alive,
        pid=1,
        working_dir=work,
    )

    def fail_if_called(_path: Path) -> float | None:
        raise AssertionError("FS check must not run when dispatcher_alive_at is fresh")

    results = reap_silent_orphans_tick(
        qd,
        {"t-cheap-wins"},
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
        fs_mtime_fn=fail_if_called,
    )

    assert results == []


def test_steady_silent_stop_reason_used_when_no_fs_evidence(tmp_path: Path) -> None:
    """Sanity-check that when the FS check finds no evidence, the
    per-tick pass writes ``STEADY_SILENT_STOP_REASON`` (not the startup
    pass's reason)."""
    qd = _queue(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    started = _now() - timedelta(seconds=3600)
    stale_hb = _now() - timedelta(seconds=500)
    _seed_task_and_state(
        qd,
        "t-stale-fs",
        started_at=started,
        last_heartbeat_at=stale_hb,
        pid=2,
        working_dir=work,
    )

    def no_activity(_path: Path) -> float | None:
        return None

    results = reap_silent_orphans_tick(
        qd,
        {"t-stale-fs"},
        settings=_settings(alert=300, kill=0),
        clock=FakeClock(_now()),
        fs_mtime_fn=no_activity,
    )

    assert len(results) == 1
    assert results[0].verdict is HeartbeatVerdict.SILENT
    reloaded = load_state(state_path_for(qd, "t-stale-fs"))
    assert reloaded.status == "possibly_hung"
    assert reloaded.stop_reason == STEADY_SILENT_STOP_REASON


def test_fs_refresh_recheck_failure_treated_as_healthy_not_demoted(tmp_path: Path) -> None:
    """When the Layer-3 FS check confirms recent activity but the
    pre-write recheck-load then fails (the state file got corrupted /
    partially written between the FS walk and the refresh write), the
    reaper must NOT demote — the task was just proven HEALTHY by the
    filesystem, so it's left untouched with no reap result.

    Exercises the ``recheck_failed`` branch of ``DemoteOutcome`` on the
    FS-refresh path: a recheck fault is distinct from a benign
    dispatcher finalize, but both correctly yield "HEALTHY this pass".
    Determinism: the state YAML is corrupted from inside ``fs_mtime_fn``,
    which the reaper calls immediately before the refresh write's
    recheck-load — so the recheck-load is guaranteed to hit invalid
    YAML."""
    qd = _queue(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    started = _now() - timedelta(seconds=3600)
    stale_hb = _now() - timedelta(seconds=2000)  # KILL band absent kill cap
    sp = state_path_for(qd, "t-fs-recheck-fail")
    _seed_task_and_state(
        qd,
        "t-fs-recheck-fail",
        started_at=started,
        last_heartbeat_at=stale_hb,
        dispatcher_alive_at=None,
        pid=55,
        working_dir=work,
    )

    fresh_mtime_unix = (_now() - timedelta(seconds=60)).timestamp()

    def stub_mtime_then_corrupt(_path: Path) -> float | None:
        # Report fresh activity (forces the refresh path), then corrupt
        # the state file so the refresh write's recheck-load raises.
        sp.write_text("not: valid: yaml: ][", encoding="utf-8")
        return fresh_mtime_unix

    calls: list[int] = []

    def recorder(pid: int) -> bool:
        calls.append(pid)
        return True

    results = reap_silent_orphans_tick(
        qd,
        {"t-fs-recheck-fail"},
        settings=_settings(alert=300, kill=900, fs_window=600),
        clock=FakeClock(_now()),
        sigterm_fn=recorder,
        fs_mtime_fn=stub_mtime_then_corrupt,
    )

    # FS proved activity → HEALTHY this pass → no reap result, no SIGTERM.
    assert results == []
    assert calls == []
    # The corrupt file was NOT overwritten with a demotion; the reaper
    # stood down and left it for a later pass / operator inspection.
    assert sp.read_text(encoding="utf-8") == "not: valid: yaml: ]["
