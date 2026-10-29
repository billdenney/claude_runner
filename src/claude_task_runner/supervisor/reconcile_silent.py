"""Silent-orphan reaper — startup and steady-state passes.

Background
----------
The runner's steady-state hung-task detection lives in
:mod:`runner.heartbeat` and is driven by the dispatcher's monitor
loop: each stream-json event updates ``last_heartbeat_at``, and a kill
threshold triggers a SIGTERM. That covers the case where the
supervisor stays alive and only the subprocess goes silent — IF the
subprocess emits events at all.

It does NOT cover two failure modes observed live:

1. **Supervisor restart.** When the supervisor exits ungracefully
   (OOM, SIGKILL after ``TimeoutStopSec``, or the bootstrap restart
   of a pre-drain-handler supervisor), every per-dispatch monitor
   thread dies with the parent process. Child ``claude --print``
   subprocesses survive (they get re-parented to init), but with no
   monitor thread no one is updating heartbeats and no one is
   enforcing the kill threshold.
2. **Silent-but-alive subprocess during a live supervisor.** The
   dispatcher's in-process silence check is event-driven: the loop
   blocks on ``parse_lines(process.stdout)`` reads and only re-
   evaluates the heartbeat threshold when a new event arrives. A
   subprocess that emits no stream-json events at all (the
   2026-06-12 ``frompeople-680-yu_2017_acta_pharmacologica_sinica``
   zombie: ~29h alive at 0.8% CPU, zero file modifications, ``end_turn``
   on SIGTERM) wedges the dispatcher's loop indefinitely. The
   in-process kill threshold never fires because the check is gated
   on event arrival.

What this module does
---------------------
Two entry points share one classification helper:

* :func:`reconcile_silent_orphans` — runs ONCE at supervisor start,
  BEFORE :func:`supervisor.reconcile.reconcile_orphans`. Walks every
  state YAML with ``status="running"`` and grades each by heartbeat
  silence: SILENT → flip to ``possibly_hung``, with the worker on record
  when it still runs (see below), for operator inspection otherwise;
  KILL → terminate the worker's process group and flip to ``failed``.
  Without this pass, the broad demotion sweep in ``reconcile_orphans``
  auto-redispatches genuinely-hung tasks, burning slots on a re-hang.
* :func:`reap_silent_orphans_tick` — runs on EVERY supervisor tick,
  alongside the existing dispatch/reap. Covers the steady-state
  silent-but-alive case: the dispatcher's loop is wedged but the
  supervisor is alive and ticking. Terminating the recorded worker
  causes the wedged subprocess to exit, which lets the dispatcher's
  loop drain and the orchestrator's reaper to free the slot on the
  next tick.

Both wrap the per-record classification in :func:`_classify_and_act`
so silence semantics stay identical: SILENT iff silence >
``heartbeat_silence_alert_s``; KILL iff silence >
``heartbeat_silence_kill_s`` (when set). HEALTHY records produce no
result and no state change. :func:`liveness_verdict` gives the verdict
on the heartbeat and the signs of life without acting, for startup
adoption (:mod:`supervisor.adoption`). It leaves out the Layer-3
filesystem check and a task it cannot grade, so a worker the reaper
keeps running only for those reasons is adopted only if its recorded
heartbeat is within the alert window; ``reconcile_orphans`` parks the
rest whose worker runs.

Neither pass signals a pid, or sets a task whose worker is still running
to ``failed``, before :func:`runner.dispatcher.probe_worker` says the pid
is still the recorded worker. A SILENT worker that still runs, and a
worker that survives a kill, are parked ``possibly_hung`` with the worker
on record (:func:`runner.dispatcher.park_live_worker`): the worker holds
its account's slot, and its run is recorded once it exits
(``supervisor.adoption.ParkedWorkerTracker``). A ``failed`` task is
dispatched again, and two workers would share its worktree. The SILENT
park of a task whose worker is gone keeps the pass's own stop reason, and
its ``error`` gives each sign of life's age.

Steady-state TOCTOU guard
-------------------------
:func:`reap_silent_orphans_tick` runs concurrently with live dispatch
threads — the dispatcher could finalize a task between the reaper's
``load_state`` (verdict computation) and ``write_state_atomic``
(demotion). If the reaper's stale demotion clobbered the dispatcher's
authoritative finalize, the run record / attempt count / cost
attribution would be lost.

The per-tick wrapper re-reads the state YAML immediately before the
write and skips the demotion if ``status`` is no longer ``"running"``
— the dispatcher's finalize wins. The window between this re-read
and the ``os.replace`` is small but non-zero; the trade-off is
explicit in the docstring of :func:`_demote_if_still_running`. The
startup pass does not need the guard because no dispatch threads run
before the daemon completes its bootstrap sequence.

Heartbeat baseline correction
-----------------------------
``last_heartbeat_at`` may be from a PRIOR (finished) run — it sits
before ``last_started_at`` until the dispatcher's first per-event
persist lands. Naively passing that into
:func:`runner.heartbeat.evaluate` would inflate the silence window
(``now - last_heartbeat_at`` includes the entire current run plus
all the time between the prior run and this one). This module guards
against that by treating any ``last_heartbeat_at`` older than
``last_started_at`` as if no heartbeat had landed yet — the evaluator
then falls back to ``last_started_at`` as the baseline, which is the
correct conservative answer for "what's the most recent confirmed
liveness signal."

The same correction is applied to ``dispatcher_alive_at`` for the same
reason.

Dual-heartbeat classification (Layer 2)
---------------------------------------
PR #57 wired ``last_heartbeat_at`` writes into the dispatcher loop;
those writes only fire when the agent emits stream-json events. A
healthy run that's mid-Bash-subprocess (R package check, large
download, OAuth refresh) can be silent for tens of minutes despite
the supervisor and dispatcher being alive and well.

This module's classifier now consults a second field,
``dispatcher_alive_at``, which the dispatcher monitor thread ticks
every ``[task_caps].dispatcher_alive_write_interval_s`` *regardless*
of whether the agent emitted anything. A fresh ``dispatcher_alive_at``
proves the monitor thread is pumping the subprocess pipe; the task is
HEALTHY even when ``last_heartbeat_at`` is stale.

The last write to a file-backed worker's stream log (ADR-0025) is a
second sign of life, counted like ``dispatcher_alive_at``: whichever is
later decides. The log grows with every event the worker writes,
including ``tool_progress`` and the other events the agent heartbeat does
not count (see :mod:`runner.stream`), and it is the only sign of life a
worker keeps up while no supervisor runs. Only when the heartbeat and
every sign of life are stale does the classifier fall through to the
filesystem verification step.

State YAMLs from the pre-Layer-2 supervisor have ``dispatcher_alive_at
= None``; the classifier treats that as "old format" and falls back to
``last_heartbeat_at`` alone (the pre-Layer-2 behaviour), so an upgrade
doesn't reap every running task.

Stuck-sleep-loop pierce (Layer 2.5)
-----------------------------------
The Layer-2 short-circuit ("monitor alive ⇒ HEALTHY") is exactly what a
stuck poll-sleep loop exploits: ``claude --print`` blocks forever on a
bash subprocess that ``sleep``s in a loop whose exit condition never
trips, so the agent emits nothing (``last_heartbeat_at`` stale) while
the monitor keeps pumping the empty pipe (``dispatcher_alive_at``
fresh). Five live incidents wedged 2h-24h+ each before the duration cap
fired (``until ! pgrep ...`` on dong/van/hoglund/yu_2015; the
``while ! [ -e <marker> ]; do sleep 5; done`` marker-wait on
wojciechowski_2015, 2026-06-19).

Before short-circuiting to HEALTHY, the classifier therefore checks
three conditions (operator design, 2026-06-19): (1) the agent has been
heartbeat-silent longer than ``stuck_sleep_loop_kill_threshold_s``
(default 600s); (2) the monitor is still alive (the REUSED Layer-2
dual-heartbeat gate — so the /proc walk runs only when heartbeat is
already stale, never every tick for every task); (3) a live descendant
of the worker is a recurring ``sleep`` inside a polling loop
(:func:`_detect_stuck_sleep_loop`). All three ⇒ terminate the process
group immediately (SIGTERM → SIGKILL → verify) with ``stop_reason``
:data:`STUCK_SLEEP_LOOP_STOP_REASON`, rather than waiting the 4h cap.
Detection is by BEHAVIOR + TIME, not loop syntax: a correctly-bounded
poll self-terminates and lets the agent resume emitting events well
before 600s, so it never trips; an unbounded one stays silent and is
killed. Gated entirely behind ``[task_caps].bash_poll_antipattern_kill``.

Filesystem activity verification (Layer 3)
------------------------------------------
When the cheap signals say a task is silent, the classifier walks the
task's ``working_dir`` for the most recent file ``st_mtime`` before
acting. If any file has been modified within
``[task_caps].zombie_verify_fs_activity_window_s`` (default 600s),
the task is treated as HEALTHY: a long-running Bash subprocess is
clearly doing useful work even when no stream-json events have
escaped through the pipe. ``last_heartbeat_at`` is refreshed from the
mtime so the next pass starts from a fresh baseline.

The walk is bounded — depth-limited, with well-known noisy directories
skipped (``.git/``, ``node_modules/``, ``__pycache__/``, ...) — and
runs at most once per in-flight task per reaper pass, only when the
cheap signals already suggest a hang. Zero overhead when everything is
healthy.
"""

from __future__ import annotations

import functools
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from claude_task_runner import process_signals
from claude_task_runner.clock import Clock, TimestampOutOfRange, utc_from_timestamp
from claude_task_runner.config.schema import TaskCapsSettings
from claude_task_runner.queue.schema import TaskState
from claude_task_runner.queue.store import (
    list_state_files,
    load_state,
    load_task,
    task_path_for,
    write_state_atomic,
)
from claude_task_runner.runner import dispatcher as dispatcher_mod
from claude_task_runner.runner.dispatcher import WorkerProbe, park_live_worker
from claude_task_runner.runner.heartbeat import (
    HeartbeatVerdict,
    evaluate,
)

logger = logging.getLogger(__name__)

SILENT_STOP_REASON = "silent_on_restart"
"""``stop_reason`` written on tasks demoted to ``possibly_hung`` by
the startup reaper. Operators can grep journals (and the
``queue states --json`` output) for this string to find restart-
orphaned tasks that need human inspection."""

KILL_STOP_REASON = "killed_by_silent_reaper"
"""``stop_reason`` written on tasks demoted to ``failed`` after either
reaper pass exceeded ``heartbeat_silence_kill_s``."""

STEADY_SILENT_STOP_REASON = "silent_steady_state"
"""``stop_reason`` written on tasks the per-tick reaper demotes to
``possibly_hung`` while the supervisor is live. Distinct from
:data:`SILENT_STOP_REASON` so the operator can tell at a glance
whether the orphan came from a supervisor restart (the original
PR #55 case) or from a silent-but-alive subprocess inside a live
supervisor (the 2026-06-12 ``frompeople-680-yu_2017`` case). Both
demote to ``possibly_hung``; only the audit trail differs."""

STUCK_SLEEP_LOOP_STOP_REASON = "killed_stuck_sleep_loop"
"""``stop_reason`` written on tasks killed because a live descendant of
the worker was found in a stuck poll-sleep loop (any of the forms in
:data:`_STUCK_SLEEP_LOOP_FAST_PATHS` or the broad fallback). Distinct
from :data:`KILL_STOP_REASON` so the operator can grep journals for the
specific zombie class and correlate across tasks.

Generalizes the narrower ``killed_bash_poll_antipattern`` reason from
PR #65 (the ``until ! pgrep ...; do sleep N; done`` case): that is now
one fast-path among several. The recurring incident history that
motivated the detector:

* frompeople-919-dong_2014, -948-van_2015, -937-hoglund_2015,
  -950-yu_2015 — ``until ! pgrep ...; do sleep N; done`` (2h-24h+ each)
* frompeople-949-wojciechowski_2015 (2026-06-19) — the Claude Code
  Bash-tool background-marker wait
  ``while ! [ -e <task>.output.exit_code ]; do sleep 5; done`` (2.5h),
  which the pgrep-only regex would have MISSED.

In every case the extraction work had already committed/pushed before
the wedge; the kill only reclaims the held slot. See
:func:`_detect_stuck_sleep_loop`."""

STUCK_SLEEP_LOOP_EVENT = "stuck_sleep_loop_zombie_killed"
"""Structured event name emitted via the supervisor's
``event_callback`` when the stuck-sleep-loop detection fires and the
worker is killed. Payload: ``task_id``, ``claude_pid``, ``bash_pid``,
``matched_argv`` (truncated), ``heartbeat_staleness_s``, ``sigtermed``.
Operators / doctor surfaces subscribe to this to correlate kills with
the matched bash argv."""


DemoteOutcome = Literal["demoted", "toctou_skipped", "recheck_failed", "write_failed"]
"""Outcome of an attempted state-YAML write by :func:`_demote_if_still_running`.

Callers branch on this deliberately rather than collapsing every
non-write into a single boolean ``False`` (the pre-audit shape, which
conflated "the dispatcher legitimately finalized" with "we couldn't
re-read the state to check"):

* ``"demoted"`` — the write landed; the transition is authoritative.
* ``"toctou_skipped"`` — the recheck saw ``status != "running"``; a
  concurrent dispatcher finalize won the race and we correctly stood
  down. Expected, benign.
* ``"recheck_failed"`` — the pre-write recheck-load itself raised
  (corrupt / partially-written state file). We did NOT write. Distinct
  from ``"toctou_skipped"`` because the cause is an I/O / parse fault,
  not a benign race — the FS-refresh path logs it differently so a
  recurring corruption doesn't masquerade as a steady stream of
  dispatcher finalizes.
* ``"write_failed"`` — the recheck (if any) passed but
  ``write_state_atomic`` raised. Logged at ERROR.

Only ``"demoted"`` represents a state change; the other three leave the
on-disk state untouched and the caller produces no :class:`ReapResult`.
"""


@dataclass(frozen=True)
class ReapResult:
    """One per state YAML the reaper acted on.

    The supervisor's daemon logs / notifies based on these so the
    operator sees what changed during the reap. Tests assert on the
    structured form rather than chasing log strings.
    """

    task_id: str
    verdict: HeartbeatVerdict
    silence_s: float
    pid: int | None
    sigtermed: bool
    """``True`` only on KILL when the recorded worker was running, was
    terminated, and is confirmed gone. ``False`` otherwise: KILL with no
    recorded pid, a worker already gone or whose pid another process now
    has (neither is signalled), a worker that survived the terminate (the
    task is then parked ``possibly_hung``, not ``failed``), or a non-KILL
    verdict (SILENT never signals)."""

    stuck_loop_bash_pid: int | None = None
    """Pid of the bash/sh descendant whose argv matched a stuck sleep
    loop (or the loop-bash parent of a bare ``sleep`` child), when the
    kill was triggered by :func:`_detect_stuck_sleep_loop`. ``None`` for
    every other reap path. Daemons key the :data:`STUCK_SLEEP_LOOP_EVENT`
    emission off this being set."""

    stuck_loop_matched_argv: str | None = None
    """Truncated (≤200 chars) bash argv that matched a stuck-sleep-loop
    rule. Goes into the structured event payload so operators can
    correlate the kill with the exact buggy bash invocation. ``None``
    when the kill was not stuck-sleep-loop-triggered."""


_STARTUP_ERROR_PREFIX = "orphaned-restart-reap"
"""Error-message prefix on KILL-verdict demotions from the startup
pass. Preserved verbatim because operators grep historical journals
and the existing alerting rules match on this exact string."""

_STEADY_ERROR_PREFIX = "silent-steady-state-reap"
"""Error-message prefix on KILL-verdict demotions from the per-tick
pass. Distinct from :data:`_STARTUP_ERROR_PREFIX` so the audit trail
distinguishes restart-orphans from supervisor-live wedges."""


_FS_WALK_SKIP_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "target",  # rust/maven build output
        "dist",
        "build",
    }
)
"""Directory names skipped by the bounded filesystem-activity walk.

These are the well-known noisy build / VCS / cache trees: spinning
through them just to find the freshest ``st_mtime`` would dominate the
walk cost without telling us anything about whether the dispatched
agent is doing useful work. The agent's deliverables (code, reports,
sidecar JSON) all live outside these trees."""


_FS_WALK_MAX_DEPTH = 4
"""Worktree directory depth past which the bounded walk stops. Empirical
observation: typical task worktrees keep deliverables within
``<repo>/<package>/<file>`` — 2-3 levels deep. A depth-4 cap leaves
headroom for nested R/Python subpackages while keeping the worst-case
walk bounded even on a pathological tree."""


def _latest_mtime_in_tree(
    root: Path,
    *,
    max_depth: int = _FS_WALK_MAX_DEPTH,
    skip_names: frozenset[str] = _FS_WALK_SKIP_NAMES,
) -> float | None:
    """Return the most recent ``st_mtime`` (as a unix timestamp) inside
    ``root``, or ``None`` if the tree is empty / unreachable.

    Walks at most ``max_depth`` levels below ``root`` and skips entries
    whose ``name`` is in ``skip_names``. Skipped directories don't
    contribute to the answer at all — their internal mtimes are
    invisible to the caller. This is intentional: the build / VCS /
    cache trees we skip have noisy mtimes that don't correlate with
    the dispatched agent's activity.

    Failures (permission errors, lost symlink targets, races against
    file deletion) are swallowed silently — the caller should treat
    ``None`` as "no observable activity" and proceed accordingly.
    """
    try:
        if not root.exists() or not root.is_dir():
            return None
    except OSError:
        return None

    latest: float | None = None

    def _walk(current: Path, depth: int) -> None:
        nonlocal latest
        if depth > max_depth:
            return
        try:
            entries = list(os.scandir(current))
        except OSError:
            return
        for entry in entries:
            try:
                if entry.name in skip_names:
                    continue
                # follow_symlinks=False to avoid loops + so a symlink's
                # mtime is the link's, not the target's.
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            mtime = stat.st_mtime
            if latest is None or mtime > latest:
                latest = mtime
            if entry.is_dir(follow_symlinks=False):
                _walk(Path(entry.path), depth + 1)

    _walk(root, depth=0)
    return latest


_BASH_POLL_ANTIPATTERN_RE = re.compile(
    r"until\s+!\s+pgrep\s+-f\b[^;\n]*;\s*do\s+sleep\s+\d+\s*;\s*done",
)
"""Fast-path regex for the original worker-side bash poll-forever
antipattern: ``until ! pgrep -f <X>; do sleep <N>; done``.

The bug: an agent issues a paired Bash tool call sequence like ::

    Rscript -e '...' &
    until ! pgrep -f buildModelDb > /dev/null; do sleep 5; done

If the background command finishes before the polling wait starts, the
``until`` condition is already false and the loop ``sleep``s forever.
``claude --print`` is blocked waiting on the bash subprocess; the
dispatcher is happily pumping the (empty) pipe so ``dispatcher_alive_at``
stays fresh; the agent emits nothing so ``last_heartbeat_at`` goes
stale.  Without an explicit detector the task waits the full
``max_duration_s_per_task`` cap (default 4h) to recover.

The regex is intentionally permissive on the ``pgrep -f`` argument
(``[^;\\n]*``) so it covers all four observed real-world shapes from
the 2026-06-13..06-19 incidents on the nlmixr2lib_ingestion queue:

* unquoted pattern + ``> /dev/null``
* quoted pattern (no bracket-trick) + ``> /dev/null 2>&1``
* quoted bracket-trick (``"[b]uildModelDb"``) + redirection
* quoted bracket-trick + escaped parens (``"[b]uildModelDb\\(\\)"``)

This is now ONE entry in :data:`_STUCK_SLEEP_LOOP_FAST_PATHS`; the broad
fallback (:data:`_STUCK_SLEEP_LOOP_FALLBACK_RE`) catches everything the
named fast paths don't. See :func:`_match_stuck_sleep_loop_argv`."""


_MARKER_WAIT_RE = re.compile(
    r"while\s+!\s+\[\s+-e\b[^;\n]*\]\s*;\s*do\s+sleep\s+\d+\s*;\s*done",
)
"""Fast-path regex for the Claude Code Bash-tool background-marker wait:
``while ! [ -e <marker> ]; do sleep N; done``.

The 2026-06-19 ``frompeople-949-wojciechowski_2015`` zombie (2.5h held
slot): the agent launched a background command and waited on its
``.output.exit_code`` marker file, which never appeared, so the loop
spun forever. Same failure class as the pgrep poll but a syntax the
pgrep regex does not match — the motivating case for this
generalization."""


_FILE_WAIT_RE = re.compile(
    r"while\s+\[\s+!\s+-f\b[^;\n]*\]\s*;\s*do\s+sleep\s+\d+\s*;\s*done",
)
"""Fast-path regex for the ``while [ ! -f <path> ]; do sleep N; done``
file-existence wait — the bracketed-negation sibling of
:data:`_MARKER_WAIT_RE` (``[ ! -f ]`` vs ``! [ -e ]``)."""


_FOR_SLEEP_RE = re.compile(
    r"for\s+[^;\n]*;\s*do\b[^\n]*\bsleep\s+[\d.]+",
)
"""Fast-path regex for a counted ``for ...; do ... sleep N ... done``
poll loop. A bounded ``for`` loop is still a zombie when the bound is
effectively infinite (``$(seq 1 100000)`` at ``sleep 5`` is ~138h) or
when the loop never breaks early; the heartbeat-staleness gate, not the
regex, is what distinguishes a stuck loop from a correctly-bounded one
that finishes and lets the agent resume emitting events."""


_STUCK_SLEEP_LOOP_FAST_PATHS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pgrep_poll", _BASH_POLL_ANTIPATTERN_RE),
    ("marker_wait", _MARKER_WAIT_RE),
    ("file_wait", _FILE_WAIT_RE),
    ("for_sleep", _FOR_SLEEP_RE),
)
"""Named fast-path regexes, tried in order before the broad fallback.

The name is purely for the structured log / event so the operator sees
WHICH known incident shape matched. A match by any of these is
equivalent to a match by :data:`_STUCK_SLEEP_LOOP_FALLBACK_RE`; the
fast paths exist to document the known forms and give precise
telemetry, not to change the kill decision."""


_STUCK_SLEEP_LOOP_FALLBACK_RE = re.compile(
    r"(?:while|until|for)\b.*\bsleep\b\s*[\d.]+",
)
"""Broad fallback: any loop keyword (``while`` / ``until`` / ``for``)
followed somewhere by a ``sleep <number>``. This — together with the
``stuck_sleep_loop_kill_threshold_s`` heartbeat-staleness gate — IS the
generalization the operator asked for; the named fast paths are merely
confirmations of known shapes.

A bare ``sleep 30`` with NO enclosing loop keyword does NOT match (a
one-shot pause between steps is legitimate). Requiring the loop keyword
in the SAME argv is the discriminator; the separate
:data:`_BARE_SLEEP_RE` / :data:`_LOOP_KEYWORD_RE` pair in
:func:`_detect_stuck_sleep_loop` handles the case where the ``sleep`` is
a *child process* of a loop bash rather than a token in its argv."""


_BARE_SLEEP_RE = re.compile(r"^\s*(?:\S*/)?sleep\s+[\d.]+\s*$")
"""Matches a cmdline that is ITSELF just a ``sleep <number>`` invocation
(optionally a fully-qualified ``/bin/sleep``), with nothing else on the
line. Used to recognise a bare ``sleep`` *child process* of a loop bash
— NOT a ``bash -c '... sleep N ...'`` (which the argv matcher handles
directly). The trailing ``\\s*$`` anchor is what keeps it from matching
a shell that merely mentions ``sleep``."""


_LOOP_KEYWORD_RE = re.compile(r"\b(?:while|until|for)\b")
"""Bare loop-keyword presence test, used only for the bare-``sleep``-
child case: a live ``sleep`` whose PARENT cmdline contains a loop
keyword is treated as a stuck loop even when the parent's own argv does
not literally contain ``sleep`` (e.g. the loop body calls a function
that shells out to ``sleep``, or the parent is ``bash <script>`` whose
loop lives in the script file)."""


def _match_stuck_sleep_loop_argv(cmdline: str) -> str | None:
    """Return the matched rule name if ``cmdline`` is a stuck sleep loop.

    Tries the named fast paths first (returning their name for precise
    telemetry), then the broad fallback (returning ``"fallback"``).
    Returns ``None`` when nothing matches. This is the (a) "descendant
    IS a loop+sleep" arm of :func:`_detect_stuck_sleep_loop`."""
    for name, pattern in _STUCK_SLEEP_LOOP_FAST_PATHS:
        if pattern.search(cmdline):
            return name
    if _STUCK_SLEEP_LOOP_FALLBACK_RE.search(cmdline):
        return "fallback"
    return None


_PROC_ROOT = Path("/proc")
"""Linux ``/proc`` root used by :func:`_detect_stuck_sleep_loop`.
A constant (not a default argument) so tests can monkey-patch at the
module level when simulating non-Linux."""


_MAX_BASH_ARGV_LOG_CHARS = 200
"""Cap on the bash argv string included in the structured event payload
and the INFO log line. The argv shouldn't ever exceed this for a real
stuck sleep loop (the loop skeleton is ~70 chars; the inner condition is
typically a single symbol / marker path plus optional redirection), but
a pathological agent could in principle paste arbitrary code, and we
don't want one task's bug to fill journald with a single 8KB log line."""


def _truncate_argv_for_log(argv: str) -> str:
    """Cap ``argv`` for the structured log / event payload."""
    if len(argv) <= _MAX_BASH_ARGV_LOG_CHARS:
        return argv
    return argv[: _MAX_BASH_ARGV_LOG_CHARS - 3] + "..."


def _read_proc_children(pid: int) -> list[int]:
    """Return the immediate-children pid list from ``/proc/<pid>/task/<pid>/children``.

    Linux only. The ``children`` file is a space-separated list of pids,
    populated by the kernel's CONFIG_PROC_CHILDREN. Returns ``[]`` on any
    read failure (file missing — pid gone, kernel without
    CONFIG_PROC_CHILDREN, race against process exit, permission denied).
    """
    try:
        text = (_PROC_ROOT / str(pid) / "task" / str(pid) / "children").read_text()
    except OSError:
        return []
    children: list[int] = []
    for token in text.split():
        try:
            children.append(int(token))
        except ValueError:
            continue
    return children


def _read_proc_cmdline(pid: int) -> str | None:
    """Return ``/proc/<pid>/cmdline`` with NULs replaced by spaces.

    Returns ``None`` on read failure (pid gone, permission denied,
    /proc not mounted). The trailing NUL the kernel emits after the last
    argv element is stripped before the split so the returned string
    has no trailing space.
    """
    try:
        raw = (_PROC_ROOT / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    # cmdline ends with a trailing NUL byte; strip before splitting so
    # the final element doesn't become an empty string.
    if raw.endswith(b"\x00"):
        raw = raw[:-1]
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return None
    return text.replace("\x00", " ")


def _detect_stuck_sleep_loop(
    pid: int,
    *,
    max_descendants: int = 256,
) -> tuple[int, str] | None:
    """Walk the process tree under ``pid`` looking for a stuck sleep loop.

    Linux-only. Returns ``(bash_descendant_pid, matched_argv_truncated)``
    on a match, or ``None`` when no match is found / the platform has no
    ``/proc`` / the root pid has no accessible ``task/<pid>/children``
    file.

    Two detection arms (per the operator's 2026-06-19 design):

    (a) **argv mode** — a descendant whose ``/proc/<pid>/cmdline``
        matches :func:`_match_stuck_sleep_loop_argv` (any named fast path
        in :data:`_STUCK_SLEEP_LOOP_FAST_PATHS` or the broad
        :data:`_STUCK_SLEEP_LOOP_FALLBACK_RE`). This is the common case:
        the loop is an inline ``bash -c '... while/until/for … sleep N …
        done'`` so the whole loop body is in the argv. Returns that
        descendant's pid + argv.

    (b) **bare-sleep mode** — a descendant that is itself just a
        ``sleep N`` process (:data:`_BARE_SLEEP_RE`) whose PARENT cmdline
        contains a loop keyword (:data:`_LOOP_KEYWORD_RE`). Catches the
        case where the loop bash's own argv does not literally contain
        ``sleep`` (the body shells out to ``sleep``, or the loop lives in
        a script file). Returns the loop-bash PARENT's pid + argv so the
        operator sees the loop, not the ephemeral ``sleep`` child. A bare
        ``sleep`` whose parent is NOT a loop bash is deliberately ignored
        — a one-shot ``sleep 30`` between steps is legitimate.

    The walk is breadth-first and capped at ``max_descendants`` visited
    pids — a defensive cap against a runaway process tree. In practice
    ``claude --print`` has 2-5 descendants (the worker, the bash for
    the current Bash tool call, any subprocesses bash spawned), so the
    cap never bites; it exists so a future regression that, say, fork-
    bombs through this code cannot wedge the supervisor.

    On match, INFO-logs the detection (with the matched rule name) so the
    operator sees the correlation in journald immediately. The caller is
    responsible for the actual terminate / state transition.

    A ``pid`` the runner must never signal (1 or less, or the
    supervisor's own) raises
    :class:`~claude_task_runner.process_signals.UnsafeSignalTarget` before
    the walk. Every process descends from pid 1, so a walk from there
    matches any stuck loop on the machine, and on 2026-09-26 the caller's
    terminate then signalled process group 1: every process the user
    owns.
    """
    process_signals.refuse_unsafe_pid(pid, "scan the process tree of")
    if not _PROC_ROOT.exists():
        # Non-Linux (macOS test runner, container with /proc masked, etc.).
        # The detector is a Linux-only optimisation; the duration cap
        # still backstops the zombie on other platforms.
        return None

    # cmdline is read once per pid and cached: each non-root pid is read
    # once as a child (arms a/b) and possibly again as a parent (arm b
    # parent-keyword check). The cache keeps the walk to one read per pid.
    cmdline_cache: dict[int, str | None] = {}

    def _cmdline(p: int) -> str | None:
        if p not in cmdline_cache:
            cmdline_cache[p] = _read_proc_cmdline(p)
        return cmdline_cache[p]

    visited: set[int] = set()
    queue: list[int] = [pid]
    while queue and len(visited) < max_descendants:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        current_cmdline = _cmdline(current)
        for child in _read_proc_children(current):
            if child in visited:
                continue
            child_cmdline = _cmdline(child)
            if child_cmdline is not None:
                # (a) the descendant IS a loop+sleep argv.
                rule = _match_stuck_sleep_loop_argv(child_cmdline)
                if rule is not None:
                    truncated = _truncate_argv_for_log(child_cmdline)
                    logger.info(
                        "stuck sleep loop detected (rule=%s): root_pid=%d bash_pid=%d argv=%r",
                        rule,
                        pid,
                        child,
                        truncated,
                    )
                    return child, truncated
                # (b) the descendant is a bare ``sleep N`` whose parent
                # (``current``) is a loop bash. Report the parent loop.
                if (
                    _BARE_SLEEP_RE.match(child_cmdline)
                    and current_cmdline is not None
                    and _LOOP_KEYWORD_RE.search(current_cmdline)
                ):
                    truncated = _truncate_argv_for_log(current_cmdline)
                    logger.info(
                        "stuck sleep loop detected (rule=bare_sleep_child): "
                        "root_pid=%d loop_bash_pid=%d sleep_pid=%d argv=%r",
                        pid,
                        current,
                        child,
                        truncated,
                    )
                    return current, truncated
            queue.append(child)
    return None


def reconcile_silent_orphans(
    queue_dir: Path,
    *,
    settings: TaskCapsSettings,
    clock: Clock,
    fs_mtime_fn: Callable[[Path], float | None] | None = None,
    stuck_loop_detect_fn: Callable[[int], tuple[int, str] | None] | None = None,
    terminate_fn: Callable[[int], bool] | None = None,
    worker_probe_fn: Callable[[TaskState], WorkerProbe] | None = None,
) -> list[ReapResult]:
    """Walk in-flight state YAMLs and surface silent / hung subprocesses
    at supervisor startup.

    Called by :func:`supervisor.daemon.start_daemon` once at startup,
    immediately before :func:`supervisor.reconcile.reconcile_orphans`.

    See module docstring for the failure mode this covers (supervisor
    restart) vs. :func:`reap_silent_orphans_tick` (steady-state silent-
    but-alive). Both use the same SILENT / KILL semantics; only the
    ``stop_reason`` differs (:data:`SILENT_STOP_REASON` here vs.
    :data:`STEADY_SILENT_STOP_REASON` in the per-tick wrapper).

    Parameters
    ----------
    queue_dir
        The queue root (same path the daemon receives).
    settings
        The ``[task_caps]`` settings block. Pulls
        ``heartbeat_silence_alert_s`` and ``heartbeat_silence_kill_s``.
    clock
        Used for ``now`` via ``clock.now()``.
    terminate_fn
        Terminates the worker's process group (SIGTERM, then SIGKILL)
        and returns ``True`` iff the worker is confirmed gone; used on a
        KILL verdict and on a stuck sleep loop. Defaults to
        :func:`terminate_worker`. Tests inject a recorder.
    worker_probe_fn
        Whether a state's recorded pid is still its worker. Defaults to
        :func:`runner.dispatcher.probe_recorded_worker` for ``queue_dir``.

    Returns
    -------
    list[ReapResult]
        One entry per state YAML where the verdict was SILENT or KILL.
        HEALTHY tasks (and tasks where evaluation failed) are skipped
        silently. Order matches :func:`queue.store.list_state_files`
        (sorted by filename) so callers get deterministic ordering.
    """
    if fs_mtime_fn is None:
        fs_mtime_fn = _latest_mtime_in_tree
    if stuck_loop_detect_fn is None:
        stuck_loop_detect_fn = _detect_stuck_sleep_loop
    if terminate_fn is None:
        terminate_fn = terminate_worker
    if worker_probe_fn is None:
        worker_probe_fn = functools.partial(
            dispatcher_mod.probe_recorded_worker, queue_dir=queue_dir
        )

    results: list[ReapResult] = []
    now = clock.now()

    for state_path in list_state_files(queue_dir):
        result = _classify_and_act(
            state_path,
            queue_dir=queue_dir,
            settings=settings,
            now=now,
            silent_stop_reason=SILENT_STOP_REASON,
            kill_error_prefix=_STARTUP_ERROR_PREFIX,
            recheck_running_before_write=False,
            fs_mtime_fn=fs_mtime_fn,
            stuck_loop_detect_fn=stuck_loop_detect_fn,
            terminate_fn=terminate_fn,
            worker_probe_fn=worker_probe_fn,
        )
        if result is not None:
            results.append(result)

    return results


def reap_silent_orphans_tick(
    queue_dir: Path,
    in_flight_task_ids: set[str],
    *,
    settings: TaskCapsSettings,
    clock: Clock,
    fs_mtime_fn: Callable[[Path], float | None] | None = None,
    stuck_loop_detect_fn: Callable[[int], tuple[int, str] | None] | None = None,
    terminate_fn: Callable[[int], bool] | None = None,
    worker_probe_fn: Callable[[TaskState], WorkerProbe] | None = None,
) -> list[ReapResult]:
    """Per-tick steady-state pass over live in-flight tasks.

    Called from :func:`supervisor.daemon.start_daemon`'s tick loop,
    alongside the orchestrator's reap+dispatch. Covers the silent-but-
    alive case: the dispatcher's in-process loop is blocked on a
    stdout read (the subprocess emits no events) so its own kill
    threshold never fires. The supervisor's tick reads the state YAML
    directly and can act on silence the dispatcher cannot see.

    Differs from :func:`reconcile_silent_orphans` in three ways:

    1. **Scope**: only considers tasks in ``in_flight_task_ids`` — the
       orchestrator's live slot map. State YAMLs from tasks that have
       since been demoted by other code paths (or never picked up by
       this supervisor) are skipped.
    2. **TOCTOU guard**: re-reads each state immediately before
       writing the demotion and skips if ``status`` is no longer
       ``"running"``. Prevents clobbering a dispatch thread's
       concurrent finalize.
    3. **stop_reason**: SILENT-verdict demotions write
       :data:`STEADY_SILENT_STOP_REASON` instead of
       :data:`SILENT_STOP_REASON` so the operator can distinguish
       restart-orphans from in-supervisor wedges. KILL-verdict
       demotions share :data:`KILL_STOP_REASON` with the startup pass
       since both paths used the same evaluation and signal.

    Parameters
    ----------
    queue_dir
        The queue root.
    in_flight_task_ids
        The set of task IDs the orchestrator currently has live
        dispatch slots for. Tasks not in this set are skipped — only
        the orchestrator can know whether a given state YAML
        represents a slot this supervisor owns vs. a stale leftover.
    settings, clock, fs_mtime_fn, stuck_loop_detect_fn, terminate_fn, worker_probe_fn
        As :func:`reconcile_silent_orphans`.

    Returns
    -------
    list[ReapResult]
        One entry per task the pass acted on. Empty list when nothing
        crossed the alert / kill threshold or every in-flight task had
        finalized between read and write.
    """
    if fs_mtime_fn is None:
        fs_mtime_fn = _latest_mtime_in_tree
    if stuck_loop_detect_fn is None:
        stuck_loop_detect_fn = _detect_stuck_sleep_loop
    if terminate_fn is None:
        terminate_fn = terminate_worker
    if worker_probe_fn is None:
        worker_probe_fn = functools.partial(
            dispatcher_mod.probe_recorded_worker, queue_dir=queue_dir
        )

    results: list[ReapResult] = []
    now = clock.now()

    for state_path in list_state_files(queue_dir):
        # Cheap filename-based filter so we don't load every state YAML
        # in the queue. ``state_path_for`` uses ``<task_id>.yaml``, so
        # the stem IS the task id.
        if state_path.stem not in in_flight_task_ids:
            continue

        result = _classify_and_act(
            state_path,
            queue_dir=queue_dir,
            settings=settings,
            now=now,
            silent_stop_reason=STEADY_SILENT_STOP_REASON,
            kill_error_prefix=_STEADY_ERROR_PREFIX,
            recheck_running_before_write=True,
            fs_mtime_fn=fs_mtime_fn,
            stuck_loop_detect_fn=stuck_loop_detect_fn,
            terminate_fn=terminate_fn,
            worker_probe_fn=worker_probe_fn,
        )
        if result is not None:
            results.append(result)

    return results


@dataclass(frozen=True)
class _Liveness:
    """A running state's liveness timestamps for its current attempt.

    A timestamp from before ``last_started_at`` belongs to an earlier
    attempt and reads as ``None`` (see Heartbeat baseline correction).
    """

    started_at: datetime
    agent_heartbeat_at: datetime | None
    """``last_heartbeat_at``: the last event the dispatcher counts as the
    agent's own (see :mod:`runner.stream`)."""
    monitor_at: datetime | None
    """``dispatcher_alive_at``: the last monitor-thread tick."""
    log_written_at: datetime | None
    """The last write to the worker's stream log."""

    @property
    def sign_of_life_at(self) -> datetime | None:
        """The later of the monitor tick and the log write."""
        signs = [at for at in (self.monitor_at, self.log_written_at) if at is not None]
        return max(signs, default=None)

    def describe(self, now: datetime) -> str:
        """Each liveness timestamp's age at ``now``, for an operator."""

        def age(at: datetime | None) -> str:
            return "none this attempt" if at is None else f"{(now - at).total_seconds():.0f}s ago"

        return (
            f"agent heartbeat {age(self.agent_heartbeat_at)}, monitor tick "
            f"{age(self.monitor_at)}, worker log write {age(self.log_written_at)}"
        )


def _read_liveness(state: TaskState) -> _Liveness | None:
    """``state``'s liveness timestamps, or ``None`` without a recorded start."""
    started_at = state.last_started_at
    if started_at is None:
        return None

    def _this_attempt(at: datetime | None) -> datetime | None:
        return at if at is not None and at >= started_at else None

    return _Liveness(
        started_at=started_at,
        agent_heartbeat_at=_this_attempt(state.last_heartbeat_at),
        monitor_at=_this_attempt(state.dispatcher_alive_at),
        log_written_at=_this_attempt(_log_written_at(state)),
    )


def _log_written_at(state: TaskState) -> datetime | None:
    """When the worker last wrote its stream log, or ``None`` with no log."""
    if state.log_path is None:
        return None
    try:
        return datetime.fromtimestamp(os.stat(state.log_path).st_mtime, tz=UTC)
    except OSError:
        return None


def _sign_of_life_fresh(liveness: _Liveness, settings: TaskCapsSettings, now: datetime) -> bool:
    """Whether a sign of life is within ``heartbeat_silence_alert_s`` of ``now``."""
    at = liveness.sign_of_life_at
    return at is not None and (now - at).total_seconds() <= settings.heartbeat_silence_alert_s


def liveness_verdict(
    state: TaskState, *, settings: TaskCapsSettings, now: datetime
) -> HeartbeatVerdict | None:
    """The verdict both reaper passes grade a running ``state`` by, without acting.

    HEALTHY when a sign of life (``dispatcher_alive_at`` or the worker's
    stream log) is within ``heartbeat_silence_alert_s``; otherwise
    :func:`runner.heartbeat.evaluate` on ``last_heartbeat_at``. ``None``
    when the state cannot be graded: no ``last_started_at``, or a
    timestamp in the future. Startup adoption adopts only a HEALTHY
    worker. Neither the filesystem walk (Layer 3) nor the stuck-sleep-loop
    pierce runs here; the startup reaper, which runs before adoption, has
    already applied both. A worker the reaper kept running for recent
    filesystem activity has that activity's time as its heartbeat, and is
    HEALTHY here only if it is within ``heartbeat_silence_alert_s``.
    """
    liveness = _read_liveness(state)
    if liveness is None:
        return None
    if _sign_of_life_fresh(liveness, settings, now):
        return HeartbeatVerdict.HEALTHY
    try:
        return evaluate(
            settings=settings,
            last_heartbeat_at=liveness.agent_heartbeat_at,
            started_at=liveness.started_at,
            now=now,
        ).verdict
    except ValueError:
        return None


def _classify_and_act(
    state_path: Path,
    *,
    queue_dir: Path,
    settings: TaskCapsSettings,
    now: datetime,
    silent_stop_reason: str,
    kill_error_prefix: str,
    recheck_running_before_write: bool,
    fs_mtime_fn: Callable[[Path], float | None],
    stuck_loop_detect_fn: Callable[[int], tuple[int, str] | None],
    terminate_fn: Callable[[int], bool],
    worker_probe_fn: Callable[[TaskState], WorkerProbe],
) -> ReapResult | None:
    """Classify a single state YAML and (when warranted) demote it.

    Shared between the startup and per-tick wrappers so the silence
    semantics — including the baseline-correction trick that treats a
    pre-``last_started_at`` heartbeat as "no heartbeat this attempt",
    the Layer-2 signs of life (``dispatcher_alive_at`` and the worker's
    stream log), and the Layer-3 filesystem activity verification — stay
    identical across the two passes, and match :func:`liveness_verdict`.

    ``silent_stop_reason`` lets the caller distinguish the two paths
    in the audit trail (``SILENT_STOP_REASON`` for startup,
    ``STEADY_SILENT_STOP_REASON`` for per-tick).

    ``recheck_running_before_write`` enables the per-tick TOCTOU guard:
    just before ``write_state_atomic``, re-read the state and skip the
    demotion if ``status`` is no longer ``"running"`` — the dispatcher
    has already finalized this task and we'd be clobbering its
    authoritative run record. The startup pass disables the guard
    because no dispatch threads run before the daemon's bootstrap
    completes.

    ``fs_mtime_fn`` is the bounded filesystem walk that powers the
    Layer-3 verification — injected so tests can stub it. The default
    is :func:`_latest_mtime_in_tree`.

    On KILL, the worker is terminated only when ``worker_probe_fn`` says
    its pid is still the recorded worker, and the task goes to ``failed``
    only once the worker is gone; a worker that survives is parked
    ``possibly_hung``.

    Returns ``None`` for HEALTHY tasks (including the signs-of-life
    short-circuit and the filesystem-confirmed-activity refresh),
    unparseable state files, ``status != "running"`` rows, and rows
    where the TOCTOU guard fired. Otherwise returns the
    :class:`ReapResult` describing the state transition just performed.
    """
    try:
        state = load_state(state_path)
    except Exception as exc:
        logger.warning(
            "silent-orphan reaper: skipping unparseable state file %s: %s",
            state_path,
            exc,
        )
        return None

    if state.status != "running":
        return None

    # A running state without a recorded start is anomalous; leave it for
    # reconcile_orphans's broad demotion sweep (the startup case) or the
    # orchestrator's natural reap (the per-tick case).
    liveness = _read_liveness(state)
    if liveness is None:
        return None
    started_at = liveness.started_at
    last_hb = liveness.agent_heartbeat_at

    # Layer 2: signs of life. A fresh dispatcher_alive_at means the monitor
    # thread is pumping the worker's output; a fresh stream log means the
    # worker itself is writing. Either way the task is HEALTHY however quiet
    # the agent has been: a long Bash call emits only events the agent
    # heartbeat does not count. Neither field (pre-Layer-2 state YAMLs, a
    # pipe-backed worker) falls back to the last_heartbeat_at-only path below.
    if _sign_of_life_fresh(liveness, settings, now):
        # Before short-circuiting to HEALTHY, peek at the agent's silence:
        # when the agent has been heartbeat-silent past
        # ``stuck_sleep_loop_kill_threshold_s`` (condition 1) *and* we have
        # a recorded pid, a live worker with a silent agent is the exact
        # signature of a stuck poll-sleep loop (condition 2 is this
        # sign-of-life gate). Walk the process tree once (condition 3) for
        # a descendant ``while/until/for … sleep N`` loop; on a match,
        # terminate the process group immediately rather than waiting the
        # full duration cap.
        #
        # Gating: this is the ONLY place the /proc walk runs. Healthy
        # chatty tasks (agent heartbeat fresh) never compute a silence past
        # the threshold and never call ``stuck_loop_detect_fn``. Per-tick
        # cost for healthy tasks is zero (matching the operator's
        # 2026-06-13 directive: don't run the FS walk continuously).
        #
        # The agent-silence gate uses ``stuck_sleep_loop_kill_threshold_s``
        # (default 600s), intentionally LONGER than
        # ``heartbeat_silence_alert_s`` (default 300s): the broad
        # loop-detection fallback has a high silence bar to clear, so a
        # correctly-bounded poll (which self-terminates and lets the agent
        # resume emitting events) is never near the kill boundary.
        agent_baseline = last_hb if last_hb is not None else started_at
        agent_silence_s = (now - agent_baseline).total_seconds()
        if (
            settings.bash_poll_antipattern_kill
            and state.pid is not None
            and agent_silence_s > settings.stuck_sleep_loop_kill_threshold_s
        ):
            zombie_result = _maybe_kill_stuck_sleep_loop(
                state=state,
                state_path=state_path,
                now=now,
                agent_silence_s=agent_silence_s,
                settings=settings,
                stuck_loop_detect_fn=stuck_loop_detect_fn,
                terminate_fn=terminate_fn,
                worker_probe_fn=worker_probe_fn,
                recheck_running_before_write=recheck_running_before_write,
            )
            if zombie_result is not None:
                return zombie_result
        return None

    try:
        status = evaluate(
            settings=settings,
            last_heartbeat_at=last_hb,
            started_at=started_at,
            now=now,
        )
    except ValueError as exc:
        # Clock skew or future-dated timestamps; defer to the broad
        # demotion sweep (which doesn't consult timestamps).
        logger.warning(
            "silent-orphan reaper: %s evaluate() raised %s; deferring",
            state.task_id,
            exc,
        )
        return None

    if status.verdict is HeartbeatVerdict.HEALTHY:
        return None

    # Layer 3: filesystem activity verification.
    # Before acting on a SILENT/KILL verdict, peek at the working_dir
    # for recent file mtimes. A subprocess doing useful work via a
    # long-running Bash invocation (R check, file generation, web
    # download) won't emit stream-json events but will be writing
    # files. Treat that as HEALTHY and refresh last_heartbeat_at from
    # the mtime so the next pass starts from a fresh baseline.
    fs_refreshed = _maybe_refresh_from_filesystem(
        state_path=state_path,
        queue_dir=queue_dir,
        state=state,
        settings=settings,
        now=now,
        fs_mtime_fn=fs_mtime_fn,
        require_recheck=recheck_running_before_write,
    )
    if fs_refreshed:
        return None

    sigtermed = False
    survived = False
    # On KILL, a pid that is gone, or that another process now has, is not
    # signalled: there is no worker left to stop.
    if (
        status.verdict is HeartbeatVerdict.KILL
        and state.pid is not None
        and worker_probe_fn(state) is WorkerProbe.ALIVE
    ):
        try:
            sigtermed = terminate_fn(state.pid)
        except Exception as exc:
            logger.warning(
                "silent-orphan reaper: terminate of pid=%s for task %s raised %s",
                state.pid,
                state.task_id,
                exc,
            )
        survived = not sigtermed and worker_probe_fn(state) is WorkerProbe.ALIVE

    silence = (
        f"no sign of life within heartbeat_silence_alert_s="
        f"{settings.heartbeat_silence_alert_s:.0f} ({liveness.describe(now)})"
    )
    if (
        status.verdict is HeartbeatVerdict.SILENT
        and state.pid is not None
        and worker_probe_fn(state) is WorkerProbe.ALIVE
    ):
        # The worker runs, however quiet: parked with it on record, it
        # holds its account's slot and its run is recorded once it exits.
        demoted = park_live_worker(state, why=f"but showed {silence}")
    elif status.verdict is HeartbeatVerdict.SILENT:
        demoted = state.model_copy(
            update={
                "status": "possibly_hung",
                "stop_reason": silent_stop_reason,
                "error": (
                    f"{silent_stop_reason}: {silence}; "
                    + (
                        f"its worker pid={state.pid} is gone"
                        if state.pid is not None
                        else "no worker pid on record"
                    )
                ),
                # Clear pid so a subsequent reaper pass (or downstream
                # tooling that surfaces "live" pids) doesn't think the
                # process is still tracked; the error names it. session_id
                # is preserved for operator-driven recovery.
                "pid": None,
                "log_path": None,
            }
        )
    elif survived:
        demoted = park_live_worker(
            state,
            why=(
                f"after {kill_error_prefix} terminated it for {status.silence_s:.0f}s "
                f"silence (heartbeat_silence_kill_s={settings.heartbeat_silence_kill_s:.0f})"
            ),
        )
    else:
        demoted = state.model_copy(
            update={
                "status": "failed",
                "stop_reason": KILL_STOP_REASON,
                "error": (
                    f"{kill_error_prefix}: {status.silence_s:.0f}s silence "
                    f"exceeded heartbeat_silence_kill_s="
                    f"{settings.heartbeat_silence_kill_s:.0f}"
                ),
                # The worker is gone. session_id is preserved so the
                # re-dispatch can resume.
                "pid": None,
                "log_path": None,
            }
        )

    outcome = _demote_if_still_running(
        state_path,
        demoted,
        require_recheck=recheck_running_before_write,
    )
    if outcome != "demoted":
        # "toctou_skipped" (dispatcher finalized first), "recheck_failed"
        # (corrupt state — we conservatively don't clobber), or
        # "write_failed" (already logged at ERROR). None of these wrote
        # the demotion, so produce no ReapResult.
        return None

    logger.warning(
        "reaped silent orphan task %s: verdict=%s silence=%.0fs pid=%s "
        "sigtermed=%s new_status=%s stop_reason=%s",
        state.task_id,
        status.verdict.value,
        status.silence_s,
        state.pid,
        sigtermed,
        demoted.status,
        demoted.stop_reason,
    )
    return ReapResult(
        task_id=state.task_id,
        verdict=status.verdict,
        silence_s=status.silence_s,
        pid=state.pid,
        sigtermed=sigtermed,
    )


def _maybe_refresh_from_filesystem(
    *,
    state_path: Path,
    queue_dir: Path,
    state: TaskState,
    settings: TaskCapsSettings,
    now: datetime,
    fs_mtime_fn: Callable[[Path], float | None],
    require_recheck: bool,
) -> bool:
    """If ``state``'s working_dir has been touched recently, refresh
    ``last_heartbeat_at`` from the mtime and return ``True``.

    The Task YAML carries the ``working_dir`` (TaskState does not). If
    the Task can't be loaded (missing, unparseable, no working_dir set),
    the filesystem check is skipped — we can't verify activity for
    something we can't locate. The caller treats a ``False`` return as
    "no FS-confirmed activity, proceed to act on the SILENT/KILL verdict."

    The window is governed by ``zombie_verify_fs_activity_window_s``.
    When a recent mtime is found we write a refreshed TaskState with
    the mtime persisted as ``last_heartbeat_at``; that timestamp is
    what the next reaper pass will read, restarting the clock from the
    most-recent confirmed activity.

    The refresh write honours the same TOCTOU recheck as a demote
    write: if the dispatcher finalized between our verdict computation
    and the refresh write, skip — the authoritative state wins.
    """
    try:
        task = load_task(task_path_for(queue_dir, state.task_id))
    except Exception as exc:
        logger.debug(
            "silent-orphan reaper: %s: cannot load Task YAML (%s); skipping FS check",
            state.task_id,
            exc,
        )
        return False

    working_dir = task.working_dir
    if working_dir is None:
        # Research/analysis tasks intentionally run without a worktree
        # (mirror of the dispatcher's output-evidence gate). There's
        # nothing to walk; act on the heartbeat verdict.
        return False

    try:
        latest_mtime = fs_mtime_fn(working_dir)
    except Exception as exc:
        logger.warning(
            "silent-orphan reaper: %s: fs_mtime_fn raised %s; skipping FS check",
            state.task_id,
            exc,
        )
        return False

    if latest_mtime is None:
        return False

    fs_silence_s = now.timestamp() - latest_mtime
    if fs_silence_s > settings.zombie_verify_fs_activity_window_s:
        return False

    # FS-confirmed activity. Refresh last_heartbeat_at from the mtime
    # so the next reaper pass measures silence from the most-recent
    # confirmed activity (NOT from the prior stale stream-json event).
    try:
        mtime_dt = utc_from_timestamp(latest_mtime)
    except TimestampOutOfRange as exc:
        # An mtime no datetime can hold (past year 9999, say) is no
        # evidence of activity: act on the heartbeat verdict instead.
        logger.warning(
            "silent-orphan reaper: %s: newest file mtime under %s is out of range (%s); "
            "skipping FS check",
            state.task_id,
            working_dir,
            exc,
        )
        return False
    refreshed = state.model_copy(update={"last_heartbeat_at": mtime_dt})

    outcome = _demote_if_still_running(state_path, refreshed, require_recheck=require_recheck)
    if outcome != "demoted":
        # The refresh write didn't land, but the FS check already
        # proved recent activity — so the task is HEALTHY for THIS pass
        # regardless: return True (no reap result). We still log per
        # outcome so a corrupt-state recheck fault doesn't masquerade as
        # a benign dispatcher finalize.
        if outcome == "recheck_failed":
            logger.warning(
                "silent-orphan reaper: %s: FS activity within %.0fs but recheck-load "
                "failed; skipping last_heartbeat_at refresh, treating as HEALTHY this pass",
                state.task_id,
                settings.zombie_verify_fs_activity_window_s,
            )
        else:
            # "toctou_skipped" (dispatcher finalized first) or
            # "write_failed" (already logged at ERROR). Either way the
            # refresh is moot; treat as HEALTHY.
            logger.debug(
                "silent-orphan reaper: %s: FS-refresh write skipped (%s); treating as HEALTHY",
                state.task_id,
                outcome,
            )
        return True

    logger.info(
        "silent-orphan reaper: %s: filesystem activity within %.0fs "
        "(latest mtime %.0fs ago); refreshed last_heartbeat_at, treating as HEALTHY",
        state.task_id,
        settings.zombie_verify_fs_activity_window_s,
        fs_silence_s,
    )
    return True


def _demote_if_still_running(
    state_path: Path,
    demoted: TaskState,
    *,
    require_recheck: bool,
) -> DemoteOutcome:
    """Persist ``demoted`` to ``state_path`` if safe.

    When ``require_recheck`` is True (per-tick path), re-load the state
    and only write if ``status == "running"`` — the dispatcher may
    have finalized between our verdict computation and this write.
    The recheck window is small but non-zero; an atomically-locked
    write would close it entirely but at the cost of a queue-wide
    lock primitive the rest of the runner doesn't need. The recheck
    is the cheapest defensible mitigation.

    When ``require_recheck`` is False (startup path), no concurrent
    dispatcher exists, so we write unconditionally — matching the
    pre-existing reconcile_silent_orphans semantics.

    Returns a :data:`DemoteOutcome` so callers can branch deliberately
    instead of collapsing a benign TOCTOU race, a corrupt-state recheck
    fault, and a failed write into one ambiguous ``False`` (the
    pre-audit shape). ``"demoted"`` is the only outcome that wrote.
    """
    if require_recheck:
        try:
            current = load_state(state_path)
        except Exception as exc:
            logger.warning(
                "silent-orphan reaper: recheck-load of %s failed: %s; skipping demotion",
                state_path,
                exc,
            )
            return "recheck_failed"
        if current.status != "running":
            logger.info(
                "silent-orphan reaper: %s status changed to %s between "
                "verdict and write; skipping demotion",
                demoted.task_id,
                current.status,
            )
            return "toctou_skipped"

    try:
        write_state_atomic(demoted, state_path)
    except Exception as exc:
        logger.error(
            "silent-orphan reaper: failed to update state for %s at %s: %s",
            demoted.task_id,
            state_path,
            exc,
        )
        return "write_failed"
    return "demoted"


def _maybe_kill_stuck_sleep_loop(
    *,
    state: TaskState,
    state_path: Path,
    now: datetime,
    agent_silence_s: float,
    settings: TaskCapsSettings,
    stuck_loop_detect_fn: Callable[[int], tuple[int, str] | None],
    terminate_fn: Callable[[int], bool],
    worker_probe_fn: Callable[[TaskState], WorkerProbe],
    recheck_running_before_write: bool,
) -> ReapResult | None:
    """Run the stuck-sleep-loop detector on ``state.pid``; on match,
    terminate the worker's process group and flip the state YAML to
    ``failed``, or park it ``possibly_hung`` if the worker survives.

    Returns a :class:`ReapResult` with the stuck-loop fields populated on
    a kill+demote, or ``None`` when the pid is no longer the recorded
    worker (its tree is not scanned), the detector finds no match (the
    dispatcher just happens to be quiet for a legitimate reason), the
    recheck TOCTOU guard fires, or the state write fails.

    The caller has already verified that ``settings.bash_poll_antipattern_kill``
    is true, ``state.pid`` is not None, and the agent silence has crossed
    ``stuck_sleep_loop_kill_threshold_s`` — so the only remaining decision
    is whether the process tree actually contains a stuck sleep loop.
    """
    assert state.pid is not None  # narrowed by caller
    if worker_probe_fn(state) is not WorkerProbe.ALIVE:
        # Another process's tree is not this worker's, and a gone worker
        # has none; its monitor thread finalizes the task.
        return None
    try:
        detected = stuck_loop_detect_fn(state.pid)
    except Exception as exc:
        # /proc race / permission error / corrupt cmdline. The detector
        # is a best-effort kill-fast path; don't let an FS hiccup
        # propagate up and abort the entire reap pass. The duration cap
        # still backstops the zombie even when this path fails.
        logger.warning(
            "stuck-sleep-loop detector raised on task %s pid=%s: %s; "
            "deferring to existing reaper paths",
            state.task_id,
            state.pid,
            exc,
        )
        return None
    if detected is None:
        return None

    bash_pid, matched_argv = detected

    # Terminate the worker's whole process group: SIGTERM → grace →
    # SIGKILL → post-verify (the same escalation the dispatcher's cap-kill
    # uses; see :func:`terminate_worker`). ``state.pid`` is the
    # worker (claude --print), spawned ``start_new_session=True`` so it
    # leads its own group; signalling the group reaps the bash + sleep
    # descendants that a bare ``os.kill(state.pid, …)`` would orphan. The
    # SIGKILL escalation guarantees the slot is freed even if the bash
    # ignores SIGTERM.
    try:
        sigtermed = terminate_fn(state.pid)
    except Exception as exc:
        logger.warning(
            "stuck-sleep-loop: terminate of pid=%s for task %s raised %s; still demoting state",
            state.pid,
            state.task_id,
            exc,
        )
        sigtermed = False

    survived = not sigtermed and worker_probe_fn(state) is WorkerProbe.ALIVE
    if survived:
        demoted = park_live_worker(
            state,
            why=f"after the stuck-sleep-loop reaper terminated it (bash_pid={bash_pid})",
        )
    else:
        demoted = state.model_copy(
            update={
                "status": "failed",
                "stop_reason": STUCK_SLEEP_LOOP_STOP_REASON,
                "error": (
                    f"stuck-sleep-loop: bash_pid={bash_pid} after "
                    f"{agent_silence_s:.0f}s heartbeat staleness; argv={matched_argv!r}"
                ),
                "pid": None,
                "log_path": None,
            }
        )

    outcome = _demote_if_still_running(
        state_path,
        demoted,
        require_recheck=recheck_running_before_write,
    )
    if outcome != "demoted":
        # toctou_skipped / recheck_failed / write_failed — log already
        # emitted by the helper or above. No ReapResult to emit.
        return None

    (logger.warning if survived else logger.info)(
        "killed stuck sleep loop: task=%s claude_pid=%s "
        "bash_pid=%s heartbeat_staleness=%.0fs argv=%r new_status=%s",
        state.task_id,
        state.pid,
        bash_pid,
        agent_silence_s,
        matched_argv,
        demoted.status,
    )
    return ReapResult(
        task_id=state.task_id,
        verdict=HeartbeatVerdict.KILL,
        silence_s=agent_silence_s,
        pid=state.pid,
        sigtermed=sigtermed,
        stuck_loop_bash_pid=bash_pid,
        stuck_loop_matched_argv=matched_argv,
    )


def terminate_worker(pid: int) -> bool:
    """SIGTERM → grace → SIGKILL → post-verify the process group led by
    ``pid``; return ``True`` iff the worker is confirmed gone afterward.

    The kill path of both reaper passes, for a KILL verdict and for a
    stuck sleep loop. Rather than a bare one-shot SIGTERM (which leaves
    the worker's shell and tool processes running), it reuses the
    dispatcher's escalation, :func:`runner.dispatcher._terminate_by_pid`:
    SIGTERM the worker's process group, poll for exit, escalate to
    SIGKILL, then verify the process actually died. The bash sleep-loop
    descendants share the worker's process group (the worker is spawned
    ``start_new_session=True``), so a group signal reaches them; the
    SIGKILL escalation guarantees the slot is freed even if the bash
    ignores SIGTERM (or is wedged in a way that defers signal delivery).

    Liveness during and after the escalation is
    :func:`runner.dispatcher.probe_worker`, so a worker that died but is
    not yet reaped (a zombie) counts as gone, as it does for an adoption
    monitor's terminate. The caller confirmed the pid was the recorded
    worker just before the signal, and a pid is not given to another
    process until its own process is reaped, so the probe needs no
    identity hints here.

    Returns ``True`` iff the worker is gone after the sequence, recorded
    as :attr:`ReapResult.sigtermed` so the operator can tell a confirmed
    kill from a could-not-confirm one (EPERM under a Linux-user dispatch,
    or a ``TASK_UNINTERRUPTIBLE`` D-state). The escalation runs in the
    supervisor tick; in practice a ``sleep`` loop dies on the first
    SIGTERM within a fraction of the grace period, so the tick stall is
    sub-second — and kills are rare.
    """

    def _alive() -> bool:
        return dispatcher_mod.probe_worker(pid, log_path=None, started_at=None) is WorkerProbe.ALIVE

    try:
        dispatcher_mod._terminate_by_pid(pid, alive=_alive, sleep_fn=time.sleep)
    except Exception as exc:
        logger.warning(
            "silent-orphan reaper: _terminate_by_pid(pid=%s) raised %s; reporting unconfirmed kill",
            pid,
            exc,
        )
        return False
    return not _alive()


__all__ = [
    "KILL_STOP_REASON",
    "SILENT_STOP_REASON",
    "STEADY_SILENT_STOP_REASON",
    "STUCK_SLEEP_LOOP_EVENT",
    "STUCK_SLEEP_LOOP_STOP_REASON",
    "ReapResult",
    "liveness_verdict",
    "reap_silent_orphans_tick",
    "reconcile_silent_orphans",
    "terminate_worker",
]
