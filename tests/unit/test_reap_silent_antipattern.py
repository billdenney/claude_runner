"""Tests for the bash poll-forever antipattern reaper path.

The detector targets a specific recurring worker-side bug:
:func:`claude_task_runner.supervisor.reconcile_silent._detect_bash_poll_antipattern`
walks ``/proc/<pid>/task/<pid>/children`` looking for a bash descendant
whose argv matches the ``until ! pgrep ...; do sleep N; done``
poll-forever skeleton.

Real-world incidents on the nlmixr2lib_ingestion queue
(2026-06-13..06-19):

* frompeople-919-dong_2014   — 24h+ before manual kill
* frompeople-948-van_2015    — 10h+ before manual kill
* frompeople-937-hoglund_2015 — 2h    before manual kill (cost $16.45)
* frompeople-950-yu_2015     — 2h45m before manual kill (cost $12.38)

All four wedged inside a paired Bash tool call where the background R
process finished before the polling wait started; the ``until !
pgrep ...`` then matched no live PID immediately, ``sleep``-and-looped
forever. Per-tick reaper now pierces the Layer-2 dispatcher-alive
short-circuit (the antipattern shows up as "dispatcher monitor fine +
agent silent") and SIGTERMs the process group on detection.

The detection is purely FS-driven (reads ``/proc``) and ONLY runs when
both these conditions hold:

1. ``last_heartbeat_at`` is stale past the alert threshold
   (the agent has been quiet)
2. ``dispatcher_alive_at`` is fresh (the monitor thread is alive —
   the agent is the silent party, not a dead pipe)

That gating is what these tests prove. The full /proc walk runs only
when the symptoms match the antipattern shape.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

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
    _BASH_POLL_ANTIPATTERN_RE,
    BASH_POLL_ANTIPATTERN_STOP_REASON,
    _detect_bash_poll_antipattern,
    reap_silent_orphans_tick,
)

# ---------------------------------------------------------------------------
# Real-world antivirus: the four observed incident shapes must all match
# ---------------------------------------------------------------------------

# These are the four argv shapes observed across the wedged tasks. The
# detector is run against the WHOLE bash cmdline (post-NUL-replacement),
# so the strings include the ``bash -c`` prefix to match what /proc shows.
_REAL_WORLD_ARGVS: list[str] = [
    # frompeople-937-hoglund_2015 form: unquoted pattern + > /dev/null
    "bash -c until ! pgrep -f buildModelDb > /dev/null; do sleep 5; done",
    # frompeople-950-yu_2015 form: quoted pattern, no bracket trick,
    # combined output + error redirection
    'bash -c until ! pgrep -f "buildModelDb" > /dev/null 2>&1; do sleep 5; done',
    # frompeople-948-van_2015 form: quoted bracket-trick + > /dev/null
    'bash -c until ! pgrep -f "[b]uildModelDb" > /dev/null; do sleep 5; done',
    # frompeople-919-dong_2014 form: quoted bracket-trick WITH escaped
    # parens (literal ``\(\)`` survives single-quoted bash -c wrapping)
    'bash -c until ! pgrep -f "[b]uildModelDb\\(\\)" > /dev/null; do sleep 5; done',
]


@pytest.mark.parametrize("argv", _REAL_WORLD_ARGVS)
def test_detect_bash_poll_antipattern_matches_real_argv(argv: str) -> None:
    """All four argvs observed in the 2026-06-13..06-19 incidents must
    match the antipattern regex. A regression that loosens the regex to
    drop any of these is an immediate operator-visible problem (kills
    stop firing, the duration cap is the only backstop again).
    """
    assert _BASH_POLL_ANTIPATTERN_RE.search(argv), (
        f"antipattern regex must match real-world bash argv: {argv!r}"
    )


# Some other plausible variants the operator wants us to keep matching
# defensively. Different sleep durations, no output redirection, leading
# whitespace from a multi-line script — all should be caught.
_ANTIPATTERN_VARIANTS: list[str] = [
    "bash -c 'until ! pgrep -f foo; do sleep 1; done'",
    "bash -c 'until ! pgrep -f foo > /dev/null; do sleep 60; done'",
    "bash -c 'until ! pgrep -f foo 2> /dev/null; do sleep 5; done'",
    "    until ! pgrep -f my_proc; do sleep 10; done",
]


@pytest.mark.parametrize("argv", _ANTIPATTERN_VARIANTS)
def test_detect_bash_poll_antipattern_matches_variants(argv: str) -> None:
    assert _BASH_POLL_ANTIPATTERN_RE.search(argv), (
        f"antipattern regex must match defensive variant: {argv!r}"
    )


# ---------------------------------------------------------------------------
# No false positives: normal bash invocations must not match
# ---------------------------------------------------------------------------

# Things workers actually run that LOOK superficially similar but are
# not the poll-forever antipattern.
_NORMAL_ARGVS: list[str] = [
    # Plain Rscript invocation
    "bash -c 'Rscript -e nlmixr2lib::buildModelDb()'",
    # cd then make
    "bash -c 'cd /tmp && make'",
    # A legitimate pgrep that does NOT loop
    "bash -c 'pgrep -f buildModelDb && echo running'",
    # A while loop on something other than pgrep + sleep
    "bash -c 'while read line; do echo $line; done < input'",
    # The inverse: until a file exists (not pgrep)
    "bash -c 'until test -f /tmp/done; do sleep 1; done'",
    # Has pgrep + sleep but no until/done envelope — a one-shot
    "bash -c 'pgrep -f foo; sleep 5'",
    # A real until ! pgrep but with a meaningful body, not sleep-forever
    "bash -c 'until ! pgrep -f foo; do echo waiting; restart_service; done'",
    # Empty argv-like
    "bash",
    # Background tail (no loop)
    "bash -c 'tail -f /var/log/syslog'",
    # An Rscript that itself contains the word ``buildModelDb`` (no loop)
    'bash -c \'Rscript -e "nlmixr2lib::buildModelDb(); cat(\\"done\\")"\'',
]


@pytest.mark.parametrize("argv", _NORMAL_ARGVS)
def test_detect_bash_poll_antipattern_no_false_positives(argv: str) -> None:
    """Normal bash invocations the worker actually runs (Rscript,
    cd/make, one-shot pgrep, until-file-exists, until-with-useful-body)
    must NOT match. A false positive here would kill productive
    work."""
    assert not _BASH_POLL_ANTIPATTERN_RE.search(argv), (
        f"antipattern regex must NOT match normal argv: {argv!r}"
    )


# ---------------------------------------------------------------------------
# /proc walking: non-Linux gracefully no-ops; missing /proc returns None
# ---------------------------------------------------------------------------


def test_detect_bash_poll_antipattern_non_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``/proc`` doesn't exist (macOS test runner, container with
    ``/proc`` masked, etc.) the detector returns ``None`` cleanly — no
    AttributeError / FileNotFoundError leaks. The duration cap still
    catches the antipattern as a fallback on those platforms."""
    from claude_task_runner.supervisor import reconcile_silent as rs

    monkeypatch.setattr(rs, "_PROC_ROOT", Path("/no/such/proc/here/at/all"))
    assert _detect_bash_poll_antipattern(12345) is None


def test_detect_bash_poll_antipattern_no_children(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pid whose ``/proc/<pid>/task/<pid>/children`` file is empty
    (no descendants) returns ``None``. Common case: the worker hasn't
    spawned any bash subprocess this tick."""
    fake_proc = tmp_path / "proc"
    fake_proc.mkdir()
    self_dir = fake_proc / "self"
    self_dir.mkdir()  # marker for _PROC_ROOT.exists() check

    parent_pid = 9000
    children_dir = fake_proc / str(parent_pid) / "task" / str(parent_pid)
    children_dir.mkdir(parents=True)
    (children_dir / "children").write_text("")  # no descendants

    from claude_task_runner.supervisor import reconcile_silent as rs

    monkeypatch.setattr(rs, "_PROC_ROOT", fake_proc)
    assert _detect_bash_poll_antipattern(parent_pid) is None


def test_detect_bash_poll_antipattern_finds_descendant_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end through the /proc walk with a synthesized tree:

      parent (claude --print, pid=9000)
        └── bash (pid=9001) — innocuous Rscript
              └── bash (pid=9002) — THE ANTIPATTERN, two levels deep

    The breadth-first walk traverses all the way down, so the second-
    level bash is found and returned with its truncated argv. Proves
    the detector doesn't stop at the first child.
    """
    fake_proc = tmp_path / "proc"
    fake_proc.mkdir()
    (fake_proc / "self").mkdir()

    def _seed_proc(pid: int, *, children: list[int], cmdline: str) -> None:
        pdir = fake_proc / str(pid) / "task" / str(pid)
        pdir.mkdir(parents=True)
        (pdir / "children").write_text(" ".join(str(c) for c in children))
        # cmdline lives at /proc/<pid>/cmdline (NUL-terminated)
        cmd_path = fake_proc / str(pid) / "cmdline"
        cmd_path.write_bytes(cmdline.replace(" ", "\x00").encode() + b"\x00")

    _seed_proc(9000, children=[9001], cmdline="claude --print")
    _seed_proc(9001, children=[9002], cmdline="bash -c Rscript -e foo()")
    antipattern_argv = 'bash -c until ! pgrep -f "[b]uildModelDb" > /dev/null; do sleep 5; done'
    _seed_proc(9002, children=[], cmdline=antipattern_argv)

    from claude_task_runner.supervisor import reconcile_silent as rs

    monkeypatch.setattr(rs, "_PROC_ROOT", fake_proc)
    result = _detect_bash_poll_antipattern(9000)
    assert result is not None
    bash_pid, matched = result
    assert bash_pid == 9002
    # The NUL→space replacement in _read_proc_cmdline keeps the argv
    # tokens space-separated.
    assert "until ! pgrep -f" in matched
    assert "do sleep 5; done" in matched


def test_detect_bash_poll_antipattern_descendant_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The walk caps at ``max_descendants`` even on a pathological
    process tree. Prevents a runaway-fork regression from wedging the
    supervisor inside the detector."""
    fake_proc = tmp_path / "proc"
    fake_proc.mkdir()
    (fake_proc / "self").mkdir()

    # Build a long chain of innocuous children: 0 → 1 → 2 → ... → 500.
    # max_descendants=10 means we stop after visiting 10 nodes; the
    # innocent chain never matches, so the result is None.
    for i in range(501):
        pdir = fake_proc / str(i) / "task" / str(i)
        pdir.mkdir(parents=True)
        nxt = i + 1
        if nxt <= 500:
            (pdir / "children").write_text(str(nxt))
        else:
            (pdir / "children").write_text("")
        (fake_proc / str(i) / "cmdline").write_bytes(b"bash\x00-c\x00:\x00")

    from claude_task_runner.supervisor import reconcile_silent as rs

    monkeypatch.setattr(rs, "_PROC_ROOT", fake_proc)
    # Should not hang or visit all 501 — cap stops the walk.
    result = _detect_bash_poll_antipattern(0, max_descendants=10)
    assert result is None


# ---------------------------------------------------------------------------
# Gating: the per-tick reaper only invokes the detector when last_hb is
# stale AND dispatcher_alive_at is fresh
# ---------------------------------------------------------------------------


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
    antipattern: bool = True,
) -> TaskCapsSettings:
    return TaskCapsSettings(
        max_tokens_per_task=0,
        max_duration_s_per_task=0,
        heartbeat_silence_alert_s=alert,
        heartbeat_silence_kill_s=kill,
        zombie_verify_fs_activity_window_s=fs_window,
        bash_poll_antipattern_kill=antipattern,
    )


def _now() -> datetime:
    return datetime(2026, 6, 19, 12, 0, tzinfo=UTC)


def _seed(
    qd: Path,
    task_id: str,
    *,
    started_at: datetime,
    last_heartbeat_at: datetime | None = None,
    dispatcher_alive_at: datetime | None = None,
    pid: int | None = None,
) -> None:
    task = Task(id=task_id, title=task_id, prompt="p")
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


def _record_detector(pid: int, *, return_match: tuple[int, str] | None) -> tuple[Any, list[int]]:
    """Build a stub detector + a list capturing calls. The stub returns
    ``return_match`` on every call so tests can assert on (1) whether it
    was called and (2) the kill path's behaviour with a known match."""
    calls: list[int] = []

    def stub(p: int) -> tuple[int, str] | None:
        calls.append(p)
        return return_match

    return stub, calls


def test_antipattern_fires_when_agent_silent_and_dispatcher_alive(
    tmp_path: Path,
) -> None:
    """The exact case from the four incidents: agent has been silent
    past the alert threshold, dispatcher monitor is still pumping. The
    Layer-2 short-circuit would say HEALTHY (and the duration cap is
    the only backstop) — but the antipattern check runs first, finds
    the buggy bash, kills the process group, and demotes the task to
    failed with the antipattern stop_reason."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=3600)
    # Agent silent for 1000s (past 300s alert).
    stale_hb = _now() - timedelta(seconds=1000)
    # Dispatcher monitor wrote 10s ago (within alert).
    fresh_alive = _now() - timedelta(seconds=10)
    _seed(
        qd,
        "t-antipattern",
        started_at=started,
        last_heartbeat_at=stale_hb,
        dispatcher_alive_at=fresh_alive,
        pid=12345,
    )

    matched_argv = 'bash -c until ! pgrep -f "[b]X"; do sleep 5; done'
    detector, calls = _record_detector(12345, return_match=(99999, matched_argv))
    killpg_calls: list[int] = []

    def killpg_stub(p: int) -> bool:
        killpg_calls.append(p)
        return True

    results = reap_silent_orphans_tick(
        qd,
        {"t-antipattern"},
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
        antipattern_detect_fn=detector,
        killpg_fn=killpg_stub,
    )

    assert calls == [12345]
    assert killpg_calls == [12345]
    assert len(results) == 1
    r = results[0]
    assert r.verdict is HeartbeatVerdict.KILL
    assert r.sigtermed is True
    assert r.antipattern_bash_pid == 99999
    assert r.antipattern_matched_argv == matched_argv
    reloaded = load_state(state_path_for(qd, "t-antipattern"))
    assert reloaded.status == "failed"
    assert reloaded.stop_reason == BASH_POLL_ANTIPATTERN_STOP_REASON
    assert reloaded.error is not None
    assert "bash-poll-antipattern" in reloaded.error
    assert "bash_pid=99999" in reloaded.error


def test_antipattern_skipped_when_dispatcher_alive_also_stale(tmp_path: Path) -> None:
    """When BOTH heartbeats are stale, the existing Layer-3 FS-mtime
    path takes over — the antipattern check stays out of the way and
    the detector is never invoked. (A bash sleep loop produces no FS
    activity either, so the FS path eventually classifies it as a
    zombie and signals via the original SIGTERM path.) Proves the
    antipattern path doesn't shadow the existing reaper."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=3600)
    stale_hb = _now() - timedelta(seconds=1000)
    stale_alive = _now() - timedelta(seconds=1000)
    _seed(
        qd,
        "t-both-stale",
        started_at=started,
        last_heartbeat_at=stale_hb,
        dispatcher_alive_at=stale_alive,
        pid=42,
    )

    detector, calls = _record_detector(42, return_match=(101, "argv"))
    killpg_calls: list[int] = []

    def killpg_stub(p: int) -> bool:
        killpg_calls.append(p)
        return True

    reap_silent_orphans_tick(
        qd,
        {"t-both-stale"},
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
        antipattern_detect_fn=detector,
        killpg_fn=killpg_stub,
        sigterm_fn=lambda _pid: True,
    )

    # Detector was NOT called — Layer-3 owns this case.
    assert calls == []
    assert killpg_calls == []


def test_antipattern_skipped_when_agent_also_fresh(tmp_path: Path) -> None:
    """When the agent is also fresh (chatty task: stream-json events
    just landed), the antipattern check stays out. No /proc walk in the
    common healthy-and-chatty case."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=3600)
    fresh_hb = _now() - timedelta(seconds=10)
    fresh_alive = _now() - timedelta(seconds=5)
    _seed(
        qd,
        "t-chatty",
        started_at=started,
        last_heartbeat_at=fresh_hb,
        dispatcher_alive_at=fresh_alive,
        pid=77,
    )

    detector, calls = _record_detector(77, return_match=(0, ""))

    results = reap_silent_orphans_tick(
        qd,
        {"t-chatty"},
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
        antipattern_detect_fn=detector,
        killpg_fn=lambda _p: True,
    )

    assert results == []
    assert calls == []  # never invoked


def test_antipattern_skipped_when_pid_missing(tmp_path: Path) -> None:
    """Without a recorded pid we can't /proc-walk. Skip cleanly and
    let the existing reaper path own the case."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=3600)
    stale_hb = _now() - timedelta(seconds=1000)
    fresh_alive = _now() - timedelta(seconds=10)
    _seed(
        qd,
        "t-no-pid",
        started_at=started,
        last_heartbeat_at=stale_hb,
        dispatcher_alive_at=fresh_alive,
        pid=None,
    )

    detector, calls = _record_detector(0, return_match=(0, ""))

    reap_silent_orphans_tick(
        qd,
        {"t-no-pid"},
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
        antipattern_detect_fn=detector,
        killpg_fn=lambda _p: True,
    )

    assert calls == []


def test_antipattern_skipped_when_knob_disabled(tmp_path: Path) -> None:
    """``[task_caps].bash_poll_antipattern_kill = false`` reverts to
    the duration-cap-only behaviour. Operators can dial off the new
    behaviour without code changes if it ever misbehaves on their queue."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=3600)
    stale_hb = _now() - timedelta(seconds=1000)
    fresh_alive = _now() - timedelta(seconds=10)
    _seed(
        qd,
        "t-disabled",
        started_at=started,
        last_heartbeat_at=stale_hb,
        dispatcher_alive_at=fresh_alive,
        pid=33,
    )

    detector, calls = _record_detector(33, return_match=(101, "argv"))

    reap_silent_orphans_tick(
        qd,
        {"t-disabled"},
        settings=_settings(alert=300, kill=900, antipattern=False),
        clock=FakeClock(_now()),
        antipattern_detect_fn=detector,
        killpg_fn=lambda _p: True,
    )

    assert calls == []


def test_antipattern_detector_exception_swallowed(tmp_path: Path) -> None:
    """A /proc race or a transient OSError inside the detector must
    not abort the reaper pass — log + skip + let the duration cap
    catch it. Reaper resilience: one task's FS hiccup shouldn't
    starve every other task of reap work this tick."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=3600)
    stale_hb = _now() - timedelta(seconds=1000)
    fresh_alive = _now() - timedelta(seconds=10)
    _seed(
        qd,
        "t-detector-boom",
        started_at=started,
        last_heartbeat_at=stale_hb,
        dispatcher_alive_at=fresh_alive,
        pid=55,
    )

    def boom(_pid: int) -> tuple[int, str] | None:
        raise OSError("transient /proc race")

    results = reap_silent_orphans_tick(
        qd,
        {"t-detector-boom"},
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
        antipattern_detect_fn=boom,
        killpg_fn=lambda _p: True,
    )

    # No kill; task left running for the next tick / duration cap.
    assert results == []
    reloaded = load_state(state_path_for(qd, "t-detector-boom"))
    assert reloaded.status == "running"


def test_antipattern_killpg_failure_still_demotes(tmp_path: Path) -> None:
    """Even when ``killpg`` returns False (permission denied / process
    raced its own exit), the state YAML is still demoted to ``failed``
    with the antipattern stop_reason — but ``sigtermed=False`` so the
    operator can spot the kill that didn't land."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=3600)
    stale_hb = _now() - timedelta(seconds=1000)
    fresh_alive = _now() - timedelta(seconds=10)
    _seed(
        qd,
        "t-killpg-fail",
        started_at=started,
        last_heartbeat_at=stale_hb,
        dispatcher_alive_at=fresh_alive,
        pid=66,
    )

    detector, _ = _record_detector(66, return_match=(67, "argv"))

    results = reap_silent_orphans_tick(
        qd,
        {"t-killpg-fail"},
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
        antipattern_detect_fn=detector,
        killpg_fn=lambda _p: False,
    )

    assert len(results) == 1
    assert results[0].sigtermed is False
    reloaded = load_state(state_path_for(qd, "t-killpg-fail"))
    assert reloaded.status == "failed"
    assert reloaded.stop_reason == BASH_POLL_ANTIPATTERN_STOP_REASON


def test_antipattern_regex_structure() -> None:
    """Smoke-check the compiled regex has the expected high-level shape.
    Guards against an accidental refactor that drops an anchor and lets
    arbitrary text match."""
    assert isinstance(_BASH_POLL_ANTIPATTERN_RE, re.Pattern)
    # Specific tokens that MUST be present in the pattern.
    pat = _BASH_POLL_ANTIPATTERN_RE.pattern
    for required in ("until", "pgrep", "-f", "sleep", "done"):
        assert required in pat, f"antipattern regex lost token {required!r}: {pat!r}"
