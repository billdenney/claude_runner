"""Startup reconciliation of orphaned task state.

When a supervisor exits ungracefully (crash, OOM, SIGKILL after
``TimeoutStopSec``, or — in the bootstrap case where a supervisor
running pre-PR-11 code can't process SIGUSR1 — a forced restart),
its dispatch threads die mid-task. The ``TaskState`` YAMLs that
those threads were updating are stuck at ``status="running"`` because
the dispatcher never got to write the terminal state.

When the next supervisor starts, its in-memory ``in_flight_slots``
dict is empty (it just booted; no threads alive). So every
``"running"`` ``TaskState`` on disk is, by construction, an orphan:
nothing is actually monitoring it. Worse, the orchestrator's
``_DISPATCHABLE_STATUSES = {"pending", "failed"}`` does NOT include
``"running"`` — so the new supervisor SKIPS those tasks forever,
even though they're not really running. They're stranded.

Division of labour, so this isn't read as "``running`` tasks are
never reconciled": ``running`` TaskStates are handled by
:mod:`supervisor.reconcile_silent` (a startup pass plus a per-tick
reaper that grades each by heartbeat silence) and by this module's
:func:`reconcile_orphans` (startup only). Both turn a stuck
``running`` into ``failed``; only then does the orchestrator's
steady-state loop — which covers ``pending`` / ``failed`` via
``_DISPATCHABLE_STATUSES`` — pick the task back up.

This module's :func:`reconcile_orphans` demotes those orphan tasks
to ``"failed"`` so the orchestrator's normal re-dispatch flow picks
them up. Crucially, ``TaskState.session_id`` is preserved across the
demotion — so when the dispatcher hits
:func:`runner.session.plan_next_spawn` on the re-dispatch, it sees
a present ``session_id`` and a still-existing
``~/.claude/projects/<dir>/<id>.jsonl`` and plans
``claude --resume <session_id>`` with a continuation nudge.

The end-to-end recovery:

1. Supervisor dies. ``TaskState.status == "running"`` on disk.
   ``TaskState.session_id`` was already written by the dispatcher
   when claude reported the session on its first stream-json event.
2. New supervisor starts. :func:`reconcile_orphans` runs early.
3. Orphan's status is demoted to ``"failed"``,
   ``stop_reason="orphaned_by_supervisor_restart"``.
4. ``snapshot.in_flight`` / ``in_flight_task_ids`` are cleared (the
   old supervisor's last-persisted in-flight list is stale), except the
   records of tasks parked with a live worker, which name the account
   each such worker runs on.
5. First tick's orchestrator sees a ``"failed"`` task that's a
   ``_DISPATCHABLE_STATUS`` — picks it up.
6. ``plan_next_spawn`` sees the persisted ``session_id`` and the
   JSONL — returns ``RESUME``.
7. Dispatcher spawns ``claude --resume <id>`` with the continuation
   nudge. The agent picks up the conversation where it left off;
   tokens for the resumed turn re-use Anthropic's cache where
   possible.

Edge cases:

* ``session_id`` never recorded (claude crashed before first stream-
  json event): ``plan_next_spawn`` falls back to FRESH.
* JSONL missing (Claude Code's projects dir was cleaned up):
  ``plan_next_spawn`` falls back to FRESH.
* The previous supervisor was actually still alive when this one
  ran (lock-file race): :mod:`pidfile` would have refused to start.
  Not our problem to detect here.
* The orphan's worker is still running (``KillMode=process`` keeps
  workers alive across a restart) and was not adopted: adoption is off,
  or the worker showed no sign of life for ``heartbeat_silence_alert_s``,
  or no state recorded its pid (it is then found by its stream log,
  :func:`runner.dispatcher.live_worker_record`).
  A ``failed`` task is dispatched again, and a second worker would then
  share the first one's worktree, so the task is parked ``possibly_hung``
  instead (:func:`park_live_worker`) and a WARNING names the pid.
  :func:`runner.dispatcher.probe_worker` decides whether the pid is
  still the worker, not a process that was given the pid after the
  worker exited.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from claude_task_runner.queue.schema import TaskState
from claude_task_runner.queue.store import (
    list_state_files,
    load_state,
    state_path_for,
    write_state_atomic,
)
from claude_task_runner.runner import dispatcher as dispatcher_mod
from claude_task_runner.runner.dispatcher import LIVE_WORKER_STOP_REASON, park_live_worker
from claude_task_runner.supervisor.states import SupervisorSnapshot

logger = logging.getLogger(__name__)

ORPHAN_STOP_REASON = "orphaned_by_supervisor_restart"
"""Stop reason recorded on demoted orphan tasks. Surfaces in
``account list`` / ``queue states`` so the operator can tell why a
task's previous run is marked failed (and can grep journals for
this specific phrase to find restarts that orphaned work)."""


@dataclass(frozen=True)
class ParkedWorker:
    """A running orphan :func:`reconcile_orphans` parked because its worker
    still runs; the daemon reports each one."""

    task_id: str
    pid: int
    log_path: str | None


def reconcile_orphans(
    queue_dir: Path,
    snapshot: SupervisorSnapshot,
    *,
    adopted_ids: set[str] | None = None,
) -> tuple[SupervisorSnapshot, list[str], list[ParkedWorker]]:
    """Demote orphan ``"running"`` TaskStates to ``"failed"``.

    Called by ``start_daemon`` once at startup, after the snapshot is
    loaded and before the first tick. Returns a tuple of:

    1. The supervisor snapshot with ``in_flight`` /
       ``in_flight_task_ids`` cleared (those records were the
       previous supervisor's view; this supervisor has its own
       empty slot map), except the records of tasks parked with a live
       worker: they name the account each one runs on, which
       ``supervisor.adoption.track_parked_workers`` keeps counting.
    2. The list of task IDs whose state was demoted.
    3. A :class:`ParkedWorker` for each running orphan whose recorded
       worker still runs. Such a task is never demoted: it is parked
       ``possibly_hung`` with :data:`LIVE_WORKER_STOP_REASON` and logged at
       WARNING, and the daemon notifies the operator.

    Writes are atomic per TaskState via
    :func:`queue.store.write_state_atomic`. The snapshot is returned
    not persisted — the caller persists it once on the next normal
    tick or sooner.

    Parameters
    ----------
    queue_dir
        The queue root (the same path ``start_daemon`` receives).
    snapshot
        The just-loaded supervisor snapshot. The function returns a
        copy with stale in-flight fields cleared.
    adopted_ids
        Task ids that a prior :func:`supervisor.adoption.adopt_running_workers`
        pass re-attached to this supervisor (ADR-0025). These have a
        live worker and a monitor thread; they are ``"running"`` for a
        legitimate reason and must NOT be demoted. ``None`` / empty set
        (the default, and the only possibility when
        ``[supervisor].adopt_workers`` is off) preserves the historical
        demote-every-running-orphan behaviour.
    """
    shielded = adopted_ids or set()
    orphan_ids: list[str] = []
    parked: list[ParkedWorker] = []
    for state_path in list_state_files(queue_dir):
        try:
            state = load_state(state_path)
        except Exception as exc:
            logger.warning(
                "reconcile_orphans: skipping unparseable state file %s: %s",
                state_path,
                exc,
            )
            continue
        if state.status != "running":
            continue
        if state.task_id in shielded:
            # Adopted by this supervisor — a live worker + monitor thread
            # own it; demoting would orphan the worker and lose the work.
            logger.info(
                "reconcile_orphans: task %s is adopted (live worker); not demoting",
                state.task_id,
            )
            continue
        live = dispatcher_mod.live_worker_record(state, queue_dir)
        if live is not None:
            if _park_unadopted(live, state_path):
                assert live.pid is not None  # live_worker_record records a pid
                parked.append(ParkedWorker(live.task_id, live.pid, live.log_path))
            continue
        demoted = state.model_copy(
            update={
                "status": "failed",
                "stop_reason": ORPHAN_STOP_REASON,
                # Leave error=None — this isn't a real failure; the
                # task was working fine when the supervisor died. The
                # next dispatch via session-resume continues from where
                # the conversation left off.
                # The worker is gone: drop its pid and log so the state
                # does not point at it.
                "pid": None,
                "log_path": None,
            }
        )
        try:
            write_state_atomic(demoted, state_path)
        except Exception as exc:
            logger.error(
                "reconcile_orphans: failed to demote orphan %s at %s: %s",
                state.task_id,
                state_path,
                exc,
            )
            continue
        orphan_ids.append(state.task_id)
        logger.info(
            "reconciled orphan task %s -> failed (session_id=%s, "
            "will resume via plan_next_spawn on next dispatch)",
            state.task_id,
            state.session_id,
        )

    # Clear the stale in-flight list. The previous supervisor's view
    # of who-was-running-where doesn't apply to this process; our
    # own in_flight_slots will be authoritative going forward, and
    # the orchestrator refreshes the snapshot's in_flight list every
    # tick. A parked live worker's record stays: it is the only record
    # of the account the worker runs on.
    kept = [r for r in snapshot.in_flight if _is_parked(queue_dir, r.task_id)]
    new_snapshot = snapshot.model_copy(
        update={
            "in_flight": kept,
            "in_flight_task_ids": [r.task_id for r in kept],
        }
    )
    return new_snapshot, orphan_ids, parked


def _is_parked(queue_dir: Path, task_id: str) -> bool:
    """Whether ``task_id``'s state is parked with a live worker on record."""
    try:
        return dispatcher_mod.is_parked_live_worker(load_state(state_path_for(queue_dir, task_id)))
    except Exception:
        return False


def _park_unadopted(state: TaskState, state_path: Path) -> bool:
    """Park a running orphan whose worker still runs, with a WARNING.
    ``False`` when the state could not be written."""
    parked = park_live_worker(state, why="and was not adopted by this supervisor")
    try:
        write_state_atomic(parked, state_path)
    except Exception as exc:
        logger.error(
            "reconcile_orphans: failed to park task %s, whose worker pid=%s is still "
            "running, at %s: %s",
            state.task_id,
            state.pid,
            state_path,
            exc,
        )
        return False
    logger.warning(
        "reconcile_orphans: task %s's worker pid=%s is still running and was not "
        "adopted; parked possibly_hung (stop_reason=%s), not failed, so it is not "
        "dispatched a second time (log=%s)",
        state.task_id,
        state.pid,
        LIVE_WORKER_STOP_REASON,
        state.log_path,
    )
    return True


__all__ = [
    "LIVE_WORKER_STOP_REASON",
    "ORPHAN_STOP_REASON",
    "ParkedWorker",
    "park_live_worker",
    "reconcile_orphans",
]
