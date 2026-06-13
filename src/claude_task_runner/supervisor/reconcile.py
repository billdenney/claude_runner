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
   old supervisor's last-persisted in-flight list is stale).
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
"""

from __future__ import annotations

import logging
from pathlib import Path

from claude_task_runner.queue.store import (
    list_state_files,
    load_state,
    write_state_atomic,
)
from claude_task_runner.supervisor.states import SupervisorSnapshot

logger = logging.getLogger(__name__)

ORPHAN_STOP_REASON = "orphaned_by_supervisor_restart"
"""Stop reason recorded on demoted orphan tasks. Surfaces in
``account list`` / ``queue states`` so the operator can tell why a
task's previous run is marked failed (and can grep journals for
this specific phrase to find restarts that orphaned work)."""


def reconcile_orphans(
    queue_dir: Path,
    snapshot: SupervisorSnapshot,
) -> tuple[SupervisorSnapshot, list[str]]:
    """Demote orphan ``"running"`` TaskStates to ``"failed"``.

    Called by ``start_daemon`` once at startup, after the snapshot is
    loaded and before the first tick. Returns a tuple of:

    1. The supervisor snapshot with ``in_flight`` /
       ``in_flight_task_ids`` cleared (those records were the
       previous supervisor's view; this supervisor has its own
       empty slot map).
    2. The list of task IDs whose state was demoted.

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
    """
    orphan_ids: list[str] = []
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
        demoted = state.model_copy(
            update={
                "status": "failed",
                "stop_reason": ORPHAN_STOP_REASON,
                # Leave error=None — this isn't a real failure; the
                # task was working fine when the supervisor died. The
                # next dispatch via session-resume continues from where
                # the conversation left off.
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
    # tick.
    new_snapshot = snapshot.model_copy(
        update={
            "in_flight": [],
            "in_flight_task_ids": [],
        }
    )
    return new_snapshot, orphan_ids


__all__ = ["ORPHAN_STOP_REASON", "reconcile_orphans"]
