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
  silence: SILENT → flip to ``possibly_hung`` for operator inspection;
  KILL → SIGTERM the recorded pid (best-effort) and flip to ``failed``.
  Without this pass, the broad demotion sweep in ``reconcile_orphans``
  auto-redispatches genuinely-hung tasks, burning slots on a re-hang.
* :func:`reap_silent_orphans_tick` — runs on EVERY supervisor tick,
  alongside the existing dispatch/reap. Covers the steady-state
  silent-but-alive case: the dispatcher's loop is wedged but the
  supervisor is alive and ticking. SIGTERM via the recorded pid
  causes the wedged subprocess to exit, which lets the dispatcher's
  loop drain and the orchestrator's reaper to free the slot on the
  next tick.

Both wrap the per-record classification in :func:`_classify_and_act`
so silence semantics stay identical: SILENT iff silence >
``heartbeat_silence_alert_s``; KILL iff silence >
``heartbeat_silence_kill_s`` (when set). HEALTHY records produce no
result and no state change.

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
HEALTHY even when ``last_heartbeat_at`` is stale. Only when both
fields are stale does the classifier fall through to the filesystem
verification step.

State YAMLs from the pre-Layer-2 supervisor have ``dispatcher_alive_at
= None``; the classifier treats that as "old format" and falls back to
``last_heartbeat_at`` alone (the pre-Layer-2 behaviour), so an upgrade
doesn't reap every running task.

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

import logging
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from claude_task_runner.clock import Clock
from claude_task_runner.config.schema import TaskCapsSettings
from claude_task_runner.queue.schema import TaskState
from claude_task_runner.queue.store import (
    list_state_files,
    load_state,
    load_task,
    task_path_for,
    write_state_atomic,
)
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
    """``True`` only on KILL when ``pid`` was set and the kill call
    succeeded (the subprocess existed and we had permission). ``False``
    otherwise — KILL with no recorded pid, KILL with the process
    already gone, or a non-KILL verdict (SILENT never signals)."""


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


def reconcile_silent_orphans(
    queue_dir: Path,
    *,
    settings: TaskCapsSettings,
    clock: Clock,
    sigterm_fn: Callable[[int], bool] | None = None,
    fs_mtime_fn: Callable[[Path], float | None] | None = None,
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
    sigterm_fn
        Override for the SIGTERM call so tests can record signals
        without actually killing anything. Receives the recorded pid
        and returns ``True`` if the signal was delivered (process
        existed, permission OK). Defaults to a best-effort wrapper
        around :func:`os.kill` that returns ``False`` on
        ``ProcessLookupError`` / ``PermissionError`` / ``OSError``.

    Returns
    -------
    list[ReapResult]
        One entry per state YAML where the verdict was SILENT or KILL.
        HEALTHY tasks (and tasks where evaluation failed) are skipped
        silently. Order matches :func:`queue.store.list_state_files`
        (sorted by filename) so callers get deterministic ordering.
    """
    if sigterm_fn is None:
        sigterm_fn = _default_sigterm
    if fs_mtime_fn is None:
        fs_mtime_fn = _latest_mtime_in_tree

    results: list[ReapResult] = []
    now = clock.now()

    for state_path in list_state_files(queue_dir):
        result = _classify_and_act(
            state_path,
            queue_dir=queue_dir,
            settings=settings,
            now=now,
            sigterm_fn=sigterm_fn,
            silent_stop_reason=SILENT_STOP_REASON,
            kill_error_prefix=_STARTUP_ERROR_PREFIX,
            recheck_running_before_write=False,
            fs_mtime_fn=fs_mtime_fn,
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
    sigterm_fn: Callable[[int], bool] | None = None,
    fs_mtime_fn: Callable[[Path], float | None] | None = None,
) -> list[ReapResult]:
    """Per-tick steady-state pass over live in-flight tasks.

    Called from :func:`supervisor.daemon.run_forever`'s tick loop,
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
    settings, clock, sigterm_fn
        As :func:`reconcile_silent_orphans`.

    Returns
    -------
    list[ReapResult]
        One entry per task the pass acted on. Empty list when nothing
        crossed the alert / kill threshold or every in-flight task had
        finalized between read and write.
    """
    if sigterm_fn is None:
        sigterm_fn = _default_sigterm
    if fs_mtime_fn is None:
        fs_mtime_fn = _latest_mtime_in_tree

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
            sigterm_fn=sigterm_fn,
            silent_stop_reason=STEADY_SILENT_STOP_REASON,
            kill_error_prefix=_STEADY_ERROR_PREFIX,
            recheck_running_before_write=True,
            fs_mtime_fn=fs_mtime_fn,
        )
        if result is not None:
            results.append(result)

    return results


def _classify_and_act(
    state_path: Path,
    *,
    queue_dir: Path,
    settings: TaskCapsSettings,
    now: datetime,
    sigterm_fn: Callable[[int], bool],
    silent_stop_reason: str,
    kill_error_prefix: str,
    recheck_running_before_write: bool,
    fs_mtime_fn: Callable[[Path], float | None],
) -> ReapResult | None:
    """Classify a single state YAML and (when warranted) demote it.

    Shared between the startup and per-tick wrappers so the silence
    semantics — including the baseline-correction trick that treats a
    pre-``last_started_at`` heartbeat as "no heartbeat this attempt",
    the Layer-2 ``dispatcher_alive_at`` short-circuit, and the Layer-3
    filesystem activity verification — stay identical across the two
    passes.

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

    Returns ``None`` for HEALTHY tasks (including the dispatcher-alive
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

    started_at = state.last_started_at
    if started_at is None:
        # status="running" without a recorded start is anomalous; leave
        # it for reconcile_orphans's broad demotion sweep (the startup
        # case) or the orchestrator's natural reap (the per-tick case).
        return None

    # See module docstring (Heartbeat baseline correction): a
    # last_heartbeat_at that predates this attempt's start belongs to
    # the previous (finished) run and would falsely inflate the silence
    # window if passed into evaluate() verbatim. Treat as "no heartbeat
    # this attempt" so evaluate falls back to started_at.
    last_hb = state.last_heartbeat_at
    if last_hb is not None and last_hb < started_at:
        last_hb = None

    # Layer 2: dispatcher_alive_at short-circuit.
    # A fresh dispatcher_alive_at means the monitor thread is pumping
    # the subprocess pipe — the task is HEALTHY regardless of how
    # quiet the agent has been. The same baseline-correction trick
    # applies (a pre-started_at value belongs to a prior attempt).
    # ``None`` is the pre-Layer-2 legacy state YAML and falls back to
    # the last_heartbeat_at-only path below.
    dispatcher_alive_at = state.dispatcher_alive_at
    if dispatcher_alive_at is not None and dispatcher_alive_at < started_at:
        dispatcher_alive_at = None

    if dispatcher_alive_at is not None:
        alive_silence_s = (now - dispatcher_alive_at).total_seconds()
        if alive_silence_s <= settings.heartbeat_silence_alert_s:
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
    if status.verdict is HeartbeatVerdict.KILL and state.pid is not None:
        try:
            sigtermed = sigterm_fn(state.pid)
        except Exception as exc:
            logger.warning(
                "silent-orphan reaper: SIGTERM of pid=%s for task %s raised %s",
                state.pid,
                state.task_id,
                exc,
            )

    if status.verdict is HeartbeatVerdict.SILENT:
        new_status = "possibly_hung"
        stop_reason = silent_stop_reason
        error: str | None = None
    else:
        new_status = "failed"
        stop_reason = KILL_STOP_REASON
        error = (
            f"{kill_error_prefix}: {status.silence_s:.0f}s silence "
            f"exceeded heartbeat_silence_kill_s="
            f"{settings.heartbeat_silence_kill_s:.0f}"
        )

    demoted = state.model_copy(
        update={
            "status": new_status,
            "stop_reason": stop_reason,
            "error": error,
            # Clear pid so a subsequent reaper pass (or downstream
            # tooling that surfaces "live" pids) doesn't think the
            # process is still tracked. session_id is preserved so a
            # KILL-then-re-dispatch path can resume.
            "pid": None,
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

    logger.info(
        "reaped silent orphan task %s: verdict=%s silence=%.0fs pid=%s "
        "sigtermed=%s new_status=%s stop_reason=%s",
        state.task_id,
        status.verdict.value,
        status.silence_s,
        state.pid,
        sigtermed,
        new_status,
        stop_reason,
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
    mtime_dt = datetime.fromtimestamp(latest_mtime, tz=UTC)
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


def _default_sigterm(pid: int) -> bool:
    """Best-effort SIGTERM. Returns ``True`` iff the signal was delivered.

    Caller-facing contract: a ``False`` return means "the supervisor
    could not signal this pid" — it does NOT mean "the process is
    gone". The two failure modes are deliberately distinguished:

    * ``ProcessLookupError`` (ESRCH) — the pid is genuinely gone. A
      ``False`` here is the only case where the process is known-dead.
    * ``PermissionError`` (EPERM) / other ``OSError`` — the supervisor
      lacks permission to signal (e.g. the pid is owned by another
      user after a Linux-user dispatch under sudo, or the supervisor
      dropped privilege). The process state is UNKNOWN and very likely
      still alive; we just couldn't reach it. These are logged at
      WARNING so the operator can see the failed kill on diagnosis.

    Either way the caller flips the state to ``failed`` (see the
    ``ReapResult.sigtermed`` docstring and ``_classify_and_act``):
    the demotion is unconditional, and ``sigtermed`` records whether
    the signal actually landed so the operator can tell a clean kill
    (``sigtermed=True``) from a could-not-signal demotion
    (``sigtermed=False`` — pid may still be running and need a manual
    ``kill``).
    """
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        # ESRCH — pid is genuinely gone; the only known-dead case.
        return False
    except PermissionError as exc:
        # EPERM — could not signal; the process is very likely still
        # alive. Surface at WARNING so the operator sees it.
        logger.warning("SIGTERM of pid=%s denied (EPERM); process may still be alive: %s", pid, exc)
        return False
    except OSError as exc:
        # Any other OSError — state unknown; assume still alive.
        logger.warning("SIGTERM of pid=%s failed; process state unknown: %s", pid, exc)
        return False


__all__ = [
    "KILL_STOP_REASON",
    "SILENT_STOP_REASON",
    "STEADY_SILENT_STOP_REASON",
    "ReapResult",
    "reap_silent_orphans_tick",
    "reconcile_silent_orphans",
]
