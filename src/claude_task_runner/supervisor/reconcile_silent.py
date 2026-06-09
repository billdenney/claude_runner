"""Startup pass that flags silent-but-alive in-flight tasks.

Background
----------
The runner's steady-state hung-task detection lives in
:mod:`runner.heartbeat` and is driven by the dispatcher's monitor
loop: each stream-json event updates ``last_heartbeat_at``, and a kill
threshold triggers a SIGTERM. That covers the case where the
supervisor stays alive and only the subprocess goes silent.

It does NOT cover supervisor restart. When the supervisor exits
ungracefully (OOM, SIGKILL after ``TimeoutStopSec``, or the bootstrap
restart of a pre-drain-handler supervisor), every per-dispatch
monitor thread dies with the parent process. Child ``claude --print``
subprocesses survive (they get re-parented to init), but with no
monitor thread no one is updating heartbeats and no one is enforcing
the kill threshold. The state YAML stays at ``status="running"`` and
the orchestrator's :data:`runner.orchestrator._DISPATCHABLE_STATUSES`
does NOT include ``"running"`` — so the orphan task can't be re-
dispatched until something demotes it.

The existing :func:`supervisor.reconcile.reconcile_orphans` handles
this by unconditionally demoting every ``running`` state to ``failed``
on supervisor startup — fine for the common case but undifferentiated:
a task that had been silent for two days is treated the same as one
that was healthy when the supervisor died, both auto-redispatch via
session resume. For tasks that are genuinely hung (DNS outage stuck
an OAuth refresh, the subprocess is wedged in a syscall), auto-
redispatch just hides the underlying problem and burns slots on a
re-hang.

What this module does
---------------------
:func:`reconcile_silent_orphans` runs ONCE at supervisor start,
BEFORE :func:`supervisor.reconcile.reconcile_orphans`. It walks every
state YAML with ``status="running"`` and, using the same
:func:`runner.heartbeat.evaluate` the dispatcher's monitor loop uses,
classifies each one:

* HEALTHY — silence is within
  ``[task_caps].heartbeat_silence_alert_s``. Leave it alone;
  ``reconcile_orphans`` will demote it to ``failed`` for the normal
  session-resume recovery path.
* SILENT — silence has crossed the alert threshold but not the kill
  threshold (or no kill threshold is set, i.e.
  ``heartbeat_silence_kill_s == 0``). Flip the state to
  ``"possibly_hung"`` with ``stop_reason="silent_on_restart"``. The
  orchestrator does NOT pick up ``possibly_hung`` tasks, so the task
  parks for operator inspection rather than re-hanging on auto-
  redispatch.
* KILL — silence has crossed the kill threshold. If the state YAML
  recorded a pid (the dispatcher writes it right after ``Popen``),
  attempt a SIGTERM (best-effort — the process may already be gone,
  or owned by another user). Flip the state to ``"failed"`` with
  ``stop_reason="killed_by_silent_reaper"`` and an ``error`` value
  noting the silence duration. ``reconcile_orphans``'s subsequent
  sweep is then a no-op for this task (it's no longer ``running``).

Why before reconcile_orphans, not after
---------------------------------------
The design brief originally suggested "after reconcile_orphans
finishes the dead-PID sweep." That ordering presumes
``reconcile_orphans`` is PID-aware and only touches tasks whose pid
is dead. It isn't — the current implementation demotes every
``running`` state without consulting any liveness signal. Running
this pass AFTER would find nothing to do. Running BEFORE gives this
pass first crack at silent tasks (so the operator sees them
distinctly) while the broad demotion sweep handles the rest.

Heartbeat baseline correction
-----------------------------
The dispatcher currently writes ``last_heartbeat_at`` only at
dispatch finalization, not on every stream-json event. So a task
that's been running for hours has a ``last_heartbeat_at`` from a
PRIOR (finished) run — sitting before its current
``last_started_at``. Naively passing that into
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
this reaper. Operators can grep journals (and the
``queue states --json`` output) for this string to find restart-
orphaned tasks that need human inspection."""

KILL_STOP_REASON = "killed_by_silent_reaper"
"""``stop_reason`` written on tasks demoted to ``failed`` after the
reaper exceeded ``heartbeat_silence_kill_s``."""


@dataclass(frozen=True)
class ReapResult:
    """One per state YAML the reaper acted on.

    The supervisor's daemon logs / notifies based on these so the
    operator sees what changed during startup reconciliation. Tests
    assert on the structured form rather than chasing log strings.
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


def reconcile_silent_orphans(
    queue_dir: Path,
    *,
    settings: TaskCapsSettings,
    clock: Clock,
    sigterm_fn: Callable[[int], bool] | None = None,
) -> list[ReapResult]:
    """Walk in-flight state YAMLs and surface silent / hung subprocesses.

    Called by :func:`supervisor.daemon.start_daemon` once at startup,
    immediately before :func:`supervisor.reconcile.reconcile_orphans`.

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
        try:
            state = load_state(state_path)
        except Exception as exc:
            logger.warning(
                "reconcile_silent_orphans: skipping unparseable state file %s: %s",
                state_path,
                exc,
            )
            continue

        if state.status != "running":
            continue

        started_at = state.last_started_at
        if started_at is None:
            # status="running" without a recorded start is anomalous;
            # leave it for reconcile_orphans's broad demotion sweep so
            # the orchestrator can re-dispatch (or the operator can
            # inspect via `account list`).
            continue

        # See module docstring (Heartbeat baseline correction): a
        # last_heartbeat_at that predates this attempt's start belongs
        # to the previous (finished) run and would falsely inflate the
        # silence window if passed into evaluate() verbatim. Treat as
        # "no heartbeat this attempt" so evaluate falls back to
        # started_at.
        last_hb = state.last_heartbeat_at
        if last_hb is not None and last_hb < started_at:
            last_hb = None

        try:
            status = evaluate(
                settings=settings,
                last_heartbeat_at=last_hb,
                started_at=started_at,
                now=now,
            )
        except ValueError as exc:
            # Clock skew or future-dated timestamps; defer to
            # reconcile_orphans (which doesn't consult timestamps).
            logger.warning(
                "reconcile_silent_orphans: %s evaluate() raised %s; deferring to reconcile_orphans",
                state.task_id,
                exc,
            )
            continue

        if status.verdict is HeartbeatVerdict.HEALTHY:
            continue

        sigtermed = False
        if status.verdict is HeartbeatVerdict.KILL and state.pid is not None:
            try:
                sigtermed = sigterm_fn(state.pid)
            except Exception as exc:
                logger.warning(
                    "reconcile_silent_orphans: SIGTERM of pid=%s for task %s raised %s",
                    state.pid,
                    state.task_id,
                    exc,
                )

        if status.verdict is HeartbeatVerdict.SILENT:
            new_status = "possibly_hung"
            stop_reason = SILENT_STOP_REASON
            error: str | None = None
        else:
            new_status = "failed"
            stop_reason = KILL_STOP_REASON
            error = (
                f"orphaned-restart-reap: {status.silence_s:.0f}s silence "
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
                # process is still tracked. session_id is preserved so
                # a KILL-then-re-dispatch path can resume.
                "pid": None,
            }
        )
        try:
            write_state_atomic(demoted, state_path)
        except Exception as exc:
            logger.error(
                "reconcile_silent_orphans: failed to update state for %s at %s: %s",
                state.task_id,
                state_path,
                exc,
            )
            continue

        results.append(
            ReapResult(
                task_id=state.task_id,
                verdict=status.verdict,
                silence_s=status.silence_s,
                pid=state.pid,
                sigtermed=sigtermed,
            )
        )
        logger.info(
            "reaped silent orphan task %s: verdict=%s silence=%.0fs pid=%s "
            "sigtermed=%s new_status=%s",
            state.task_id,
            status.verdict.value,
            status.silence_s,
            state.pid,
            sigtermed,
            new_status,
        )

    return results


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
    "ReapResult",
    "reconcile_silent_orphans",
]
