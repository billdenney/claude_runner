"""Tests for the generalized stuck-sleep-loop zombie reaper.

:func:`claude_task_runner.supervisor.reconcile_silent._detect_stuck_sleep_loop`
walks ``/proc/<pid>/task/<pid>/children`` looking for a live descendant
that is a recurring ``sleep`` inside a polling loop — ANY
``while`` / ``until`` / ``for`` loop around a ``sleep``, not just the
original ``until ! pgrep ...; do sleep N; done`` shape. It is the
generalization of the PR #65 detector; that pgrep regex is now one
fast-path among several.

Real-world incidents on the nlmixr2lib_ingestion queue
(2026-06-13..06-19), all the SAME failure class (the agent goes
heartbeat-silent while a descendant ``sleep`` keeps cycling and the
monitor keeps ``dispatcher_alive_at`` fresh):

* frompeople-919-dong_2014    — 24h+   — ``until ! pgrep ...; do sleep N; done``
* frompeople-948-van_2015     — 10h+   — ``until ! pgrep ...``
* frompeople-937-hoglund_2015 — 2h     — ``until ! pgrep ...`` ($16.45)
* frompeople-950-yu_2015      — 2h45m  — ``until ! pgrep ...`` ($12.38)
* frompeople-949-wojciechowski_2015 (2026-06-19) — 2.5h —
  ``while ! [ -e <task>.output.exit_code ]; do sleep 5; done`` — the
  Claude Code Bash-tool background-marker wait, which the pgrep-only
  regex would have MISSED. The motivating case for this generalization.

The detection is purely FS-driven (reads ``/proc``) and gated behind
THREE conditions (operator design, 2026-06-19):

1. ``last_heartbeat_at`` is stale past ``stuck_sleep_loop_kill_threshold_s``
   (default 600s) — the agent has been quiet for >10 min.
2. ``dispatcher_alive_at`` is fresh (the monitor thread is alive — the
   agent is the silent party, not a dead pipe). This REUSES the
   existing dual-heartbeat gate so the /proc walk runs only when
   heartbeat is already stale, never every tick for every task.
3. A live descendant is a recurring ``sleep`` inside a polling loop.

All three ⇒ terminate the process group (SIGTERM → SIGKILL → verify)
and demote to ``failed`` with stop_reason ``killed_stuck_sleep_loop``.
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import subprocess
import sys
import time
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
    _BARE_SLEEP_RE,
    _BASH_POLL_ANTIPATTERN_RE,
    _LOOP_KEYWORD_RE,
    _STUCK_SLEEP_LOOP_FALLBACK_RE,
    STUCK_SLEEP_LOOP_STOP_REASON,
    _detect_stuck_sleep_loop,
    _match_stuck_sleep_loop_argv,
    reap_silent_orphans_tick,
)

_LINUX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="process groups / /proc children / SIGKILL semantics are POSIX-only",
)

# ---------------------------------------------------------------------------
# argv matching — the four pgrep incident shapes AND the wojciechowski
# marker-wait must ALL match (operator test #1)
# ---------------------------------------------------------------------------

# Strings are the WHOLE bash cmdline (post-NUL-replacement), so they
# include the ``bash -c`` prefix to match what /proc shows.
_PGREP_ARGVS: list[str] = [
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

# The 2026-06-19 wojciechowski zombie: the Claude Code Bash-tool
# background-marker wait. The ``.output.exit_code`` marker never
# appeared, so the loop spun forever. Includes the trailing ``&& tail``
# the agent actually wrote — ``.search`` finds the loop as a substring.
_WOJCIECHOWSKI_ARGV = (
    "bash -c while ! [ -e /tmp/q/.claude_task_runner/tasks/b26t87idh.output.exit_code ]; "
    "do sleep 5; done && tail -80 /tmp/q/.claude_task_runner/tasks/b26t87idh.output"
)


@pytest.mark.parametrize("argv", _PGREP_ARGVS)
def test_pgrep_incident_argvs_match(argv: str) -> None:
    """All four pgrep argvs observed in the 2026-06-13..06-19 incidents
    must match — via the ``pgrep_poll`` fast path. A regression that
    drops any of these means the original antipattern stops being
    caught and the duration cap is the only backstop again."""
    assert _match_stuck_sleep_loop_argv(argv) == "pgrep_poll", argv


def test_wojciechowski_marker_wait_matches() -> None:
    """The marker-wait argv that the pgrep-only regex MISSED (and burned
    2.5h on 2026-06-19) must now match — via the ``marker_wait`` fast
    path. This is the whole point of the generalization."""
    assert _match_stuck_sleep_loop_argv(_WOJCIECHOWSKI_ARGV) == "marker_wait"


def test_both_motivating_argvs_covered() -> None:
    """Explicit belt-and-suspenders: the deliverable requires BOTH the
    wojciechowski marker-wait AND a pgrep-poll argv to be covered. Assert
    both in one place so the guarantee is grep-able."""
    assert _match_stuck_sleep_loop_argv(_WOJCIECHOWSKI_ARGV) is not None
    assert _match_stuck_sleep_loop_argv(_PGREP_ARGVS[0]) is not None


# Each named fast path must classify its canonical shape under its own
# rule name (the name only drives telemetry, but pinning it guards the
# ordering in _STUCK_SLEEP_LOOP_FAST_PATHS).
_FAST_PATH_CASES: list[tuple[str, str]] = [
    ("pgrep_poll", "bash -c until ! pgrep -f foo; do sleep 1; done"),
    ("marker_wait", "bash -c while ! [ -e /tmp/marker ]; do sleep 5; done"),
    ("file_wait", "bash -c while [ ! -f /tmp/done ]; do sleep 5; done"),
    ("for_sleep", "bash -c for i in $(seq 1 100000); do sleep 5; done"),
]


@pytest.mark.parametrize("rule, argv", _FAST_PATH_CASES)
def test_named_fast_paths(rule: str, argv: str) -> None:
    assert _match_stuck_sleep_loop_argv(argv) == rule, argv


# Loops the named fast paths do not anchor, caught by the broad fallback.
# (A bracketless ``for ...; do ... sleep`` would hit the ``for_sleep``
# fast path, so the fallback cases below are all ``while``/``until``.)
_FALLBACK_ARGVS: list[str] = [
    # until-file-exists via `test -f` (NOT bracketed) — a genuine
    # file-wait sleep loop, the wojciechowski class in another syntax.
    "bash -c until test -f /tmp/done; do sleep 1; done",
    # while true busy-wait with a sleep
    "bash -c while true; do check_thing; sleep 30; done",
    # until a health endpoint responds (non-pgrep, non-test-f)
    "bash -c until curl -sf http://localhost/health; do sleep 2; done",
    # sub-second sleep
    "bash -c while ! ready; do sleep 0.5; done",
]


@pytest.mark.parametrize("argv", _FALLBACK_ARGVS)
def test_broad_fallback_matches(argv: str) -> None:
    """The broad ``(while|until|for) … sleep N`` fallback — together
    with the heartbeat-staleness gate — IS the generalization. Anything
    a named fast path doesn't anchor still trips it."""
    assert _match_stuck_sleep_loop_argv(argv) == "fallback", argv


# ---------------------------------------------------------------------------
# No false positives (operator test #2)
# ---------------------------------------------------------------------------

_NORMAL_ARGVS: list[str] = [
    # Plain Rscript invocation
    "bash -c Rscript -e nlmixr2lib::buildModelDb()",
    # cd then make
    "bash -c cd /tmp && make",
    # A one-shot ``sleep 30`` with NO enclosing loop keyword — a
    # legitimate pause between steps; MUST NOT trip the detector.
    "bash -c sleep 30",
    "bash -c sleep 30; echo done",
    # pgrep + sleep but no loop envelope (one-shot)
    "bash -c pgrep -f foo; sleep 5",
    # A while loop with NO sleep (consumes input)
    "bash -c while read line; do echo $line; done < input",
    # A real until ! pgrep but with a meaningful body, no sleep
    "bash -c until ! pgrep -f foo; do echo waiting; restart_service; done",
    # Background tail (no loop, no sleep)
    "bash -c tail -f /var/log/syslog",
    # Empty argv-like
    "bash",
    # An Rscript that merely contains the word ``done`` (no loop keyword)
    'bash -c Rscript -e "nlmixr2lib::buildModelDb(); cat(\\"done\\")"',
]


@pytest.mark.parametrize("argv", _NORMAL_ARGVS)
def test_no_false_positives(argv: str) -> None:
    """Normal bash invocations the worker actually runs (Rscript,
    cd/make, a one-shot ``sleep 30`` with no loop, one-shot pgrep,
    while-read, until-with-useful-body) must NOT match. A false positive
    here would kill productive work."""
    assert _match_stuck_sleep_loop_argv(argv) is None, argv


def test_one_shot_sleep_needs_no_loop_keyword() -> None:
    """The discriminator for the broad fallback is the loop keyword in
    the same argv: a bare ``sleep`` without ``while``/``until``/``for``
    is legitimate and the fallback regex must reject it."""
    assert _STUCK_SLEEP_LOOP_FALLBACK_RE.search("sleep 600") is None
    assert _STUCK_SLEEP_LOOP_FALLBACK_RE.search("sleep 30; echo hi") is None
    assert _STUCK_SLEEP_LOOP_FALLBACK_RE.search("while x; do sleep 5; done") is not None


def test_pgrep_fast_path_regex_structure() -> None:
    """Smoke-check the kept pgrep fast-path regex still has its anchors.
    Guards against a refactor that drops a token and lets arbitrary text
    match."""
    assert isinstance(_BASH_POLL_ANTIPATTERN_RE, re.Pattern)
    for required in ("until", "pgrep", "-f", "sleep", "done"):
        assert required in _BASH_POLL_ANTIPATTERN_RE.pattern, required


# ---------------------------------------------------------------------------
# Bare-``sleep``-child helpers (arm (b) of the detector)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmdline, expected",
    [
        ("sleep 5", True),
        ("sleep 0.5", True),
        ("/bin/sleep 5", True),
        ("/usr/bin/sleep 10", True),
        ("sleep", False),  # no numeric arg
        ("bash -c sleep 5", False),  # a shell, not a bare sleep
        ("sleep 5 && echo", False),  # trailing tokens
        ("sleeper 5", False),  # not sleep
    ],
)
def test_bare_sleep_re(cmdline: str, expected: bool) -> None:
    assert bool(_BARE_SLEEP_RE.match(cmdline)) is expected, cmdline


@pytest.mark.parametrize(
    "cmdline, expected",
    [
        ("bash -c while ! check; do f; done", True),
        ("bash -c until done; do x; done", True),
        ("bash -c for i in x; do f; done", True),
        ("bash loop.sh", False),  # loop is in the script, not the argv
        ("claude --print", False),
        ("python -m worker", False),
    ],
)
def test_loop_keyword_re(cmdline: str, expected: bool) -> None:
    assert bool(_LOOP_KEYWORD_RE.search(cmdline)) is expected, cmdline


# ---------------------------------------------------------------------------
# /proc walk: non-Linux gracefully no-ops; missing /proc returns None
# (operator test #4)
# ---------------------------------------------------------------------------


def test_detect_non_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``/proc`` doesn't exist (macOS test runner, container with
    ``/proc`` masked, etc.) the detector returns ``None`` cleanly — no
    AttributeError / FileNotFoundError leaks. The duration cap still
    catches the zombie as a fallback on those platforms."""
    from claude_task_runner.supervisor import reconcile_silent as rs

    monkeypatch.setattr(rs, "_PROC_ROOT", Path("/no/such/proc/here/at/all"))
    assert _detect_stuck_sleep_loop(12345) is None


def _seed_proc(fake_proc: Path, pid: int, *, children: list[int], cmdline: str) -> None:
    pdir = fake_proc / str(pid) / "task" / str(pid)
    pdir.mkdir(parents=True)
    (pdir / "children").write_text(" ".join(str(c) for c in children))
    # cmdline lives at /proc/<pid>/cmdline (NUL-separated, NUL-terminated)
    (fake_proc / str(pid) / "cmdline").write_bytes(cmdline.replace(" ", "\x00").encode() + b"\x00")


def test_detect_no_children(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A pid whose children file is empty returns ``None``. Common case:
    the worker hasn't spawned any bash subprocess this tick."""
    fake_proc = tmp_path / "proc"
    fake_proc.mkdir()
    (fake_proc / "self").mkdir()  # marker for _PROC_ROOT.exists()
    _seed_proc(fake_proc, 9000, children=[], cmdline="claude --print")

    from claude_task_runner.supervisor import reconcile_silent as rs

    monkeypatch.setattr(rs, "_PROC_ROOT", fake_proc)
    assert _detect_stuck_sleep_loop(9000) is None


def test_detect_finds_descendant_argv_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end through the BFS walk (arm a):

      claude (9000) → bash (9001, innocuous Rscript) → bash (9002, MARKER WAIT)

    The breadth-first walk traverses two levels down and returns the
    second-level loop bash with its truncated argv — proving the detector
    doesn't stop at the first child, and that the generalized matcher
    catches the marker-wait the pgrep regex would miss."""
    fake_proc = tmp_path / "proc"
    fake_proc.mkdir()
    (fake_proc / "self").mkdir()
    _seed_proc(fake_proc, 9000, children=[9001], cmdline="claude --print")
    _seed_proc(fake_proc, 9001, children=[9002], cmdline="bash -c Rscript -e foo()")
    loop_argv = "bash -c while ! [ -e /tmp/marker ]; do sleep 5; done"
    _seed_proc(fake_proc, 9002, children=[], cmdline=loop_argv)

    from claude_task_runner.supervisor import reconcile_silent as rs

    monkeypatch.setattr(rs, "_PROC_ROOT", fake_proc)
    result = _detect_stuck_sleep_loop(9000)
    assert result is not None
    bash_pid, matched = result
    assert bash_pid == 9002
    assert "while ! [ -e /tmp/marker ]" in matched
    assert "do sleep 5; done" in matched


def test_detect_finds_bare_sleep_child_of_loop_bash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Arm (b): the loop bash's OWN argv has a loop keyword but no
    literal ``sleep`` (the body shells out to ``sleep``), and a bare
    ``sleep N`` is its live child:

      claude (9000) → bash (9001, ``while ! ready; do my_wait; done``)
                        → sleep (9002, ``sleep 5``)

    The argv arm misses 9001 (no ``sleep`` token in its argv); the
    bare-sleep arm catches it via the ``sleep`` child + loop-keyword
    parent, and reports the loop BASH (9001), not the ephemeral sleep."""
    fake_proc = tmp_path / "proc"
    fake_proc.mkdir()
    (fake_proc / "self").mkdir()
    _seed_proc(fake_proc, 9000, children=[9001], cmdline="claude --print")
    # Loop keyword present, but no "sleep" token in this argv:
    _seed_proc(fake_proc, 9001, children=[9002], cmdline="bash -c while ! ready; do my_wait; done")
    _seed_proc(fake_proc, 9002, children=[], cmdline="sleep 5")

    from claude_task_runner.supervisor import reconcile_silent as rs

    monkeypatch.setattr(rs, "_PROC_ROOT", fake_proc)
    result = _detect_stuck_sleep_loop(9000)
    assert result is not None
    loop_pid, matched = result
    assert loop_pid == 9001  # the loop bash, not the sleep child
    assert "while ! ready" in matched


def test_detect_bare_sleep_without_loop_parent_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A bare ``sleep`` whose parent is NOT a loop bash is deliberately
    ignored — a one-shot ``sleep 30`` between steps is legitimate."""
    fake_proc = tmp_path / "proc"
    fake_proc.mkdir()
    (fake_proc / "self").mkdir()
    _seed_proc(fake_proc, 9000, children=[9001], cmdline="claude --print")
    # Parent runs a one-shot command, no loop keyword:
    _seed_proc(fake_proc, 9001, children=[9002], cmdline="bash -c sleep 30 && deploy")
    _seed_proc(fake_proc, 9002, children=[], cmdline="sleep 30")

    from claude_task_runner.supervisor import reconcile_silent as rs

    monkeypatch.setattr(rs, "_PROC_ROOT", fake_proc)
    assert _detect_stuck_sleep_loop(9000) is None


def test_detect_descendant_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The walk caps at ``max_descendants`` even on a pathological
    process tree. Prevents a runaway-fork regression from wedging the
    supervisor inside the detector."""
    fake_proc = tmp_path / "proc"
    fake_proc.mkdir()
    (fake_proc / "self").mkdir()
    for i in range(501):
        nxt = i + 1
        _seed_proc(fake_proc, i, children=[nxt] if nxt <= 500 else [], cmdline="bash -c :")

    from claude_task_runner.supervisor import reconcile_silent as rs

    monkeypatch.setattr(rs, "_PROC_ROOT", fake_proc)
    # Innocent chain never matches; the cap stops the walk after 10 nodes.
    assert _detect_stuck_sleep_loop(0, max_descendants=10) is None


# ---------------------------------------------------------------------------
# Per-tick gating + kill path (operator test #3 + resilience)
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
    stuck_threshold: int = 600,
    enabled: bool = True,
) -> TaskCapsSettings:
    return TaskCapsSettings(
        max_tokens_per_task=0,
        max_duration_s_per_task=0,
        heartbeat_silence_alert_s=alert,
        heartbeat_silence_kill_s=kill,
        zombie_verify_fs_activity_window_s=fs_window,
        stuck_sleep_loop_kill_threshold_s=stuck_threshold,
        bash_poll_antipattern_kill=enabled,
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


def _record_detector(*, return_match: tuple[int, str] | None) -> tuple[Any, list[int]]:
    """Stub detector + a list capturing the pids it was called with."""
    calls: list[int] = []

    def stub(p: int) -> tuple[int, str] | None:
        calls.append(p)
        return return_match

    return stub, calls


def test_fires_when_agent_silent_and_dispatcher_alive(tmp_path: Path) -> None:
    """The exact incident signature: the agent has been heartbeat-silent
    past the kill threshold, the dispatcher monitor is still pumping. The
    Layer-2 short-circuit would say HEALTHY (duration cap the only
    backstop) — but the stuck-sleep-loop check runs first, finds the
    buggy loop, terminates the process group, and demotes to failed with
    the stuck-sleep-loop stop_reason."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=3600)
    stale_hb = _now() - timedelta(seconds=1000)  # > 600s threshold
    fresh_alive = _now() - timedelta(seconds=10)  # within 300s alert
    _seed(
        qd,
        "t-zombie",
        started_at=started,
        last_heartbeat_at=stale_hb,
        dispatcher_alive_at=fresh_alive,
        pid=12345,
    )

    argv = "bash -c while ! [ -e /tmp/marker ]; do sleep 5; done"
    detector, calls = _record_detector(return_match=(99999, argv))
    term_calls: list[int] = []

    def terminate_stub(p: int) -> bool:
        term_calls.append(p)
        return True

    results = reap_silent_orphans_tick(
        qd,
        {"t-zombie"},
        settings=_settings(),
        clock=FakeClock(_now()),
        stuck_loop_detect_fn=detector,
        terminate_fn=terminate_stub,
    )

    assert calls == [12345]
    assert term_calls == [12345]
    assert len(results) == 1
    r = results[0]
    assert r.verdict is HeartbeatVerdict.KILL
    assert r.sigtermed is True
    assert r.stuck_loop_bash_pid == 99999
    assert r.stuck_loop_matched_argv == argv
    reloaded = load_state(state_path_for(qd, "t-zombie"))
    assert reloaded.status == "failed"
    assert reloaded.stop_reason == STUCK_SLEEP_LOOP_STOP_REASON
    assert reloaded.error is not None
    assert "stuck-sleep-loop" in reloaded.error
    assert "bash_pid=99999" in reloaded.error
    assert reloaded.pid is None


def test_skipped_when_heartbeat_fresh(tmp_path: Path) -> None:
    """Operator test #3 — the heartbeat gate. A matching sleep-loop
    descendant is present, but the agent's heartbeat is FRESH: the
    detector must never be invoked and nothing is killed. (Chatty task:
    stream-json events just landed.)"""
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

    detector, calls = _record_detector(return_match=(1, "argv"))
    results = reap_silent_orphans_tick(
        qd,
        {"t-chatty"},
        settings=_settings(),
        clock=FakeClock(_now()),
        stuck_loop_detect_fn=detector,
        terminate_fn=lambda _p: True,
    )
    assert results == []
    assert calls == []  # /proc walk never runs for a fresh task


def test_skipped_when_silence_below_kill_threshold(tmp_path: Path) -> None:
    """Agent silence between the monitor-alert (300s) and the
    stuck-sleep-loop kill threshold (600s) must NOT trigger the detector.
    Pins the NEW threshold knob: the kill bar is intentionally higher
    than the alert bar so the broad fallback has a long silence to clear."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=3600)
    stale_hb = _now() - timedelta(seconds=500)  # 300 < 500 < 600
    fresh_alive = _now() - timedelta(seconds=10)
    _seed(
        qd,
        "t-midband",
        started_at=started,
        last_heartbeat_at=stale_hb,
        dispatcher_alive_at=fresh_alive,
        pid=88,
    )

    detector, calls = _record_detector(return_match=(1, "argv"))
    results = reap_silent_orphans_tick(
        qd,
        {"t-midband"},
        settings=_settings(alert=300, stuck_threshold=600),
        clock=FakeClock(_now()),
        stuck_loop_detect_fn=detector,
        terminate_fn=lambda _p: True,
    )
    assert results == []
    assert calls == []


def test_skipped_when_dispatcher_also_stale(tmp_path: Path) -> None:
    """When BOTH heartbeats are stale, the monitor is presumed dead and
    the existing Layer-3 FS-mtime path owns the case — the stuck-loop
    check stays out of the way and the detector is never invoked."""
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

    detector, calls = _record_detector(return_match=(101, "argv"))
    term_calls: list[int] = []
    reap_silent_orphans_tick(
        qd,
        {"t-both-stale"},
        settings=_settings(alert=300, kill=900),
        clock=FakeClock(_now()),
        stuck_loop_detect_fn=detector,
        terminate_fn=lambda p: term_calls.append(p) or True,
        sigterm_fn=lambda _pid: True,
    )
    assert calls == []
    assert term_calls == []


def test_skipped_when_pid_missing(tmp_path: Path) -> None:
    """Without a recorded pid we can't /proc-walk. Skip cleanly and let
    the existing reaper path own the case."""
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

    detector, calls = _record_detector(return_match=(1, "argv"))
    reap_silent_orphans_tick(
        qd,
        {"t-no-pid"},
        settings=_settings(),
        clock=FakeClock(_now()),
        stuck_loop_detect_fn=detector,
        terminate_fn=lambda _p: True,
    )
    assert calls == []


def test_skipped_when_knob_disabled(tmp_path: Path) -> None:
    """``[task_caps].bash_poll_antipattern_kill = false`` reverts to the
    duration-cap-only behaviour. The single flag gates the generalized
    detector too (operator requirement)."""
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

    detector, calls = _record_detector(return_match=(101, "argv"))
    reap_silent_orphans_tick(
        qd,
        {"t-disabled"},
        settings=_settings(enabled=False),
        clock=FakeClock(_now()),
        stuck_loop_detect_fn=detector,
        terminate_fn=lambda _p: True,
    )
    assert calls == []


def test_detector_exception_swallowed(tmp_path: Path) -> None:
    """A /proc race or transient OSError inside the detector must not
    abort the reaper pass — log + skip + let the duration cap catch it.
    One task's FS hiccup shouldn't starve every other task of reap work."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=3600)
    stale_hb = _now() - timedelta(seconds=1000)
    fresh_alive = _now() - timedelta(seconds=10)
    _seed(
        qd,
        "t-boom",
        started_at=started,
        last_heartbeat_at=stale_hb,
        dispatcher_alive_at=fresh_alive,
        pid=55,
    )

    def boom(_pid: int) -> tuple[int, str] | None:
        raise OSError("transient /proc race")

    results = reap_silent_orphans_tick(
        qd,
        {"t-boom"},
        settings=_settings(),
        clock=FakeClock(_now()),
        stuck_loop_detect_fn=boom,
        terminate_fn=lambda _p: True,
    )
    assert results == []
    assert load_state(state_path_for(qd, "t-boom")).status == "running"


def test_terminate_failure_still_demotes(tmp_path: Path) -> None:
    """Even when ``terminate_fn`` returns False (could not confirm the
    kill — EPERM under a Linux-user dispatch, or a D-state), the state
    YAML is still demoted to failed with the stuck-sleep-loop stop_reason,
    but ``sigtermed=False`` so the operator can spot the kill that didn't
    confirm."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=3600)
    stale_hb = _now() - timedelta(seconds=1000)
    fresh_alive = _now() - timedelta(seconds=10)
    _seed(
        qd,
        "t-term-fail",
        started_at=started,
        last_heartbeat_at=stale_hb,
        dispatcher_alive_at=fresh_alive,
        pid=66,
    )

    detector, _ = _record_detector(return_match=(67, "argv"))
    results = reap_silent_orphans_tick(
        qd,
        {"t-term-fail"},
        settings=_settings(),
        clock=FakeClock(_now()),
        stuck_loop_detect_fn=detector,
        terminate_fn=lambda _p: False,
    )
    assert len(results) == 1
    assert results[0].sigtermed is False
    reloaded = load_state(state_path_for(qd, "t-term-fail"))
    assert reloaded.status == "failed"
    assert reloaded.stop_reason == STUCK_SLEEP_LOOP_STOP_REASON


def test_no_match_leaves_task_running(tmp_path: Path) -> None:
    """Monitor alive + agent silent past threshold, but the detector
    finds NO stuck loop (the agent is legitimately quiet — long
    single-shot compute). The task is left running for the next tick /
    duration cap; nothing is killed."""
    qd = _queue(tmp_path)
    started = _now() - timedelta(seconds=3600)
    stale_hb = _now() - timedelta(seconds=1000)
    fresh_alive = _now() - timedelta(seconds=10)
    _seed(
        qd,
        "t-quiet",
        started_at=started,
        last_heartbeat_at=stale_hb,
        dispatcher_alive_at=fresh_alive,
        pid=44,
    )

    detector, calls = _record_detector(return_match=None)
    results = reap_silent_orphans_tick(
        qd,
        {"t-quiet"},
        settings=_settings(),
        clock=FakeClock(_now()),
        stuck_loop_detect_fn=detector,
        terminate_fn=lambda _p: True,
    )
    assert calls == [44]  # the walk DID run
    assert results == []
    assert load_state(state_path_for(qd, "t-quiet")).status == "running"


# ---------------------------------------------------------------------------
# Slow integration: a real stuck loop under a fake claude parent is killed
# through the real detector + real terminate path (operator test #5)
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _spawn_detached_worker(marker: Path, pidfile: Path) -> int:
    """Spawn a fake-``claude`` worker whose CHILD runs a marker-wait loop,
    re-parented to init, and return the worker (outer) pid.

    The worker tree must NOT be a child of this pytest process: a killed
    direct child lingers as a zombie that ``os.kill(pid, 0)`` still
    reports as alive, which would defeat the real terminate path's
    post-SIGKILL verify. So we double-fork — a launcher backgrounds a
    ``setsid`` worker (new session leader) and exits, leaving the worker
    re-parented to init (which reaps it after the kill, exactly like the
    adopted-worker case the dispatcher's ``_terminate_by_pid`` targets).

    The worker writes its own pid to ``pidfile``, then backgrounds the
    marker-wait loop and ``wait``s — so the loop is a descendant the
    detector finds by walking ``/proc/<worker>/task/<worker>/children``.
    """
    loop = f"while ! [ -e {marker} ]; do sleep 1; done"
    worker = f"echo $$ > {pidfile}; bash -c '{loop}' & wait"
    launcher = f"setsid bash -c {shlex.quote(worker)} </dev/null >/dev/null 2>&1 &"
    subprocess.run(["bash", "-c", launcher], check=True, timeout=10)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if pidfile.exists() and pidfile.read_text().strip():
            return int(pidfile.read_text().strip())
        time.sleep(0.05)
    raise AssertionError("detached worker never wrote its pidfile")


@_LINUX_ONLY
@pytest.mark.slow
def test_integration_real_marker_wait_killed(tmp_path: Path) -> None:
    """End-to-end: a real ``while ! [ -e <never> ]; do sleep 1; done``
    marker-wait running as a descendant of a fake ``claude`` worker, with
    a stale heartbeat + fresh dispatcher_alive, is detected by the REAL
    /proc walk and killed by the REAL terminate path (SIGTERM PG → SIGKILL
    → verify) within one reaper tick — not after the 4h cap."""
    marker = tmp_path / "never_appears"
    pidfile = tmp_path / "worker.pid"
    pid = _spawn_detached_worker(marker, pidfile)
    try:
        # Give the loop child + its first `sleep` time to register.
        time.sleep(0.4)
        assert _pid_alive(pid), "fixture failed to start"

        children_path = Path(f"/proc/{pid}/task/{pid}/children")
        if not children_path.exists() or not children_path.read_text().strip():
            pytest.skip("/proc children unavailable (non-Linux /proc layout / kernel config)")

        # Sanity: the real detector locates the loop descendant.
        from claude_task_runner.supervisor import reconcile_silent as rs

        assert rs._detect_stuck_sleep_loop(pid) is not None, "detector missed the live loop"

        qd = _queue(tmp_path)
        started = _now() - timedelta(seconds=3600)
        _seed(
            qd,
            "t-real",
            started_at=started,
            last_heartbeat_at=_now() - timedelta(seconds=1000),
            dispatcher_alive_at=_now() - timedelta(seconds=10),
            pid=pid,
        )

        # Real detector, real terminate path — no stubs.
        results = reap_silent_orphans_tick(
            qd,
            {"t-real"},
            settings=_settings(),
            clock=FakeClock(_now()),
        )

        assert len(results) == 1
        assert results[0].verdict is HeartbeatVerdict.KILL
        assert results[0].stuck_loop_bash_pid is not None
        assert results[0].sigtermed is True
        assert load_state(state_path_for(qd, "t-real")).stop_reason == STUCK_SLEEP_LOOP_STOP_REASON
        # The whole process group is dead (no orphaned sleep loop).
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _pid_alive(pid):
            time.sleep(0.05)
        assert not _pid_alive(pid), "worker process group survived the terminate"
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(pid), 9)
