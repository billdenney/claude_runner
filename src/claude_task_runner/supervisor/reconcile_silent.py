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
"""

from __future__ import annotations

import logging
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from claude_task_runner.clock import Clock
from claude_task_runner.config.schema import TaskCapsSettings
from claude_task_runner.queue.schema import TaskState
from claude_task_runner.queue.store import (
    list_state_files,
    load_state,
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


def reconcile_silent_orphans(
    queue_dir: Path,
    *,
    settings: TaskCapsSettings,
    clock: Clock,
    sigterm_fn: Callable[[int], bool] | None = None,
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

    results: list[ReapResult] = []
    now = clock.now()

    for state_path in list_state_files(queue_dir):
        result = _classify_and_act(
            state_path,
            settings=settings,
            now=now,
            sigterm_fn=sigterm_fn,
            silent_stop_reason=SILENT_STOP_REASON,
            kill_error_prefix=_STARTUP_ERROR_PREFIX,
            recheck_running_before_write=False,
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
            settings=settings,
            now=now,
            sigterm_fn=sigterm_fn,
            silent_stop_reason=STEADY_SILENT_STOP_REASON,
            kill_error_prefix=_STEADY_ERROR_PREFIX,
            recheck_running_before_write=True,
        )
        if result is not None:
            results.append(result)

    return results


def _classify_and_act(
    state_path: Path,
    *,
    settings: TaskCapsSettings,
    now: object,  # datetime; using object to avoid circular import noise
    sigterm_fn: Callable[[int], bool],
    silent_stop_reason: str,
    kill_error_prefix: str,
    recheck_running_before_write: bool,
) -> ReapResult | None:
    """Classify a single state YAML and (when warranted) demote it.

    Shared between the startup and per-tick wrappers so the silence
    semantics — including the baseline-correction trick that treats a
    pre-``last_started_at`` heartbeat as "no heartbeat this attempt"
    — stay identical across the two passes.

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

    Returns ``None`` for HEALTHY tasks, unparseable state files,
    ``status != "running"`` rows, and rows where the TOCTOU guard
    fired. Otherwise returns the :class:`ReapResult` describing the
    state transition just performed.
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

    try:
        status = evaluate(
            settings=settings,
            last_heartbeat_at=last_hb,
            started_at=started_at,
            now=now,  # type: ignore[arg-type]
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

    if not _demote_if_still_running(
        state_path,
        demoted,
        require_recheck=recheck_running_before_write,
    ):
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


def _demote_if_still_running(
    state_path: Path,
    demoted: TaskState,
    *,
    require_recheck: bool,
) -> bool:
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

    Returns ``True`` iff the demotion was written. Logs and returns
    ``False`` on either a TOCTOU skip or a write failure.
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
            return False
        if current.status != "running":
            logger.info(
                "silent-orphan reaper: %s status changed to %s between "
                "verdict and write; skipping demotion",
                demoted.task_id,
                current.status,
            )
            return False

    try:
        write_state_atomic(demoted, state_path)
    except Exception as exc:
        logger.error(
            "silent-orphan reaper: failed to update state for %s at %s: %s",
            demoted.task_id,
            state_path,
            exc,
        )
        return False
    return True


def _default_sigterm(pid: int) -> bool:
    """Best-effort SIGTERM. Returns ``True`` iff the signal was delivered.

    ProcessLookupError (pid already gone) and PermissionError (pid
    owned by another user, e.g. a Linux-user dispatch under sudo)
    both return ``False`` without raising — the reaper logs the kill
    attempt either way, and the state flip happens regardless.
    """
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        logger.warning("SIGTERM of pid=%s denied: %s", pid, exc)
        return False
    except OSError as exc:
        logger.warning("SIGTERM of pid=%s failed: %s", pid, exc)
        return False


__all__ = [
    "KILL_STOP_REASON",
    "SILENT_STOP_REASON",
    "STEADY_SILENT_STOP_REASON",
    "ReapResult",
    "reap_silent_orphans_tick",
    "reconcile_silent_orphans",
]
