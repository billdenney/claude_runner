"""Startup adoption of restart-survivable workers (ADR-0025).

When ``[supervisor].adopt_workers`` is on, the dispatcher redirects each
worker's stdout/stderr to per-attempt files and records the stdout
``log_path`` on the task's :class:`TaskState`. A worker therefore
survives the supervisor's exit (no pipe to EPIPE) and keeps writing its
log. On the next supervisor start, *before* the broad demotion sweep in
:func:`supervisor.reconcile.reconcile_orphans`, this module re-attaches
to those still-running workers instead of losing the in-flight work.

For each ``status="running"`` state with:

* a live ``pid`` (``os.kill(pid, 0)`` succeeds — ESRCH ⇒ dead),
* a present ``log_path`` whose file exists, and
* a HEALTHY heartbeat verdict from the existing classifier,

we construct a :class:`runner.in_flight.DispatchSlot` whose thread runs
:func:`runner.dispatcher.adopt_worker` (tailing the log to completion
and finalizing via the same output gate as an owned run) and register it
in the supervisor's live ``in_flight_slots`` map. The adopted task ids
are returned so the caller can shield them from
:func:`reconcile.reconcile_orphans` (which would otherwise demote them to
``failed``).

Tasks that do NOT qualify keep today's reaper behaviour:

* dead pid / missing log_path ⇒ left for ``reconcile_orphans`` to demote
  for a session-resume re-dispatch;
* SILENT / KILL verdicts ⇒ already handled by
  :func:`supervisor.reconcile_silent.reconcile_silent_orphans`, which
  runs first; this pass skips any non-HEALTHY survivor as a
  belt-and-braces guard so a reorder of the startup sequence can't make
  it adopt a hung worker.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from claude_task_runner.clock import Clock
from claude_task_runner.config.schema import Settings
from claude_task_runner.queue.schema import Task, TaskState
from claude_task_runner.queue.store import (
    list_state_files,
    load_state,
    load_task,
    task_path_for,
)
from claude_task_runner.runner import dispatcher as dispatcher_mod
from claude_task_runner.runner.heartbeat import HeartbeatVerdict, evaluate
from claude_task_runner.runner.in_flight import DispatchSlot

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdoptionResult:
    """One adopted worker, for the daemon's startup log / notify."""

    task_id: str
    pid: int
    log_path: str


def adopt_running_workers(
    queue_dir: Path,
    *,
    settings: Settings,
    clock: Clock,
    in_flight_slots: dict[str, DispatchSlot],
) -> list[AdoptionResult]:
    """Adopt HEALTHY restart-survivable workers; return what was adopted.

    Mutates ``in_flight_slots`` in place: each adopted task gets a new
    :class:`DispatchSlot` whose thread monitors the orphaned worker to
    completion. Returns one :class:`AdoptionResult` per adopted task so
    the caller can (a) shield them from ``reconcile_orphans`` and (b)
    surface them to the operator.

    No-op (returns ``[]``) when ``[supervisor].adopt_workers`` is off —
    the legacy demote-on-restart path then applies unchanged.
    """
    if not settings.supervisor.adopt_workers:
        return []

    results: list[AdoptionResult] = []
    now = clock.now()

    for state_path in list_state_files(queue_dir):
        try:
            state = load_state(state_path)
        except Exception as exc:
            logger.warning("adoption: skipping unparseable state file %s: %s", state_path, exc)
            continue

        if not _is_adoptable(state, now=now, settings=settings):
            continue

        assert state.pid is not None  # narrowed by _is_adoptable
        assert state.log_path is not None  # narrowed by _is_adoptable

        try:
            task = load_task(task_path_for(queue_dir, state.task_id))
        except Exception as exc:
            # Without the Task we can't run adopt_worker (it needs the
            # output-evidence gate's working_dir / deliverables). Leave
            # the survivor for reconcile_orphans to demote.
            logger.warning(
                "adoption: task %s has a live worker but its Task YAML "
                "could not be loaded (%s); leaving for reconcile_orphans",
                state.task_id,
                exc,
            )
            continue

        account = state.session_host_account()
        thread = threading.Thread(
            target=_adopt_one_safely,
            args=(task, state, queue_dir, clock, settings, account),
            name=f"adopt-{state.task_id}",
            # Adoption only runs when adopt_workers is on, so the monitor
            # thread is always daemon: a fast stop abandons it and the
            # file-backed worker survives for the next supervisor
            # (ADR-0025). The worker is a separate OS process; the daemon
            # thread merely tails its log.
            daemon=True,
        )
        thread.start()
        in_flight_slots[state.task_id] = DispatchSlot(
            task_id=state.task_id,
            account=account or "default",
            started_at=state.last_started_at if state.last_started_at is not None else now,
            thread=thread,
        )
        results.append(
            AdoptionResult(
                task_id=state.task_id,
                pid=state.pid,
                log_path=state.log_path,
            )
        )
        logger.info(
            "adopted running worker for task %s (pid=%s, log=%s)",
            state.task_id,
            state.pid,
            state.log_path,
        )

    return results


def _is_adoptable(state: TaskState, *, now: object, settings: Settings) -> bool:
    """True iff ``state`` is a HEALTHY, file-backed, live-pid running task.

    Mirrors the silent reaper's baseline-correction: a
    ``last_heartbeat_at`` that predates ``last_started_at`` belongs to a
    prior finished run and is treated as "no heartbeat this attempt" so
    the verdict falls back to ``last_started_at``.
    """
    if state.status != "running":
        return False
    if state.pid is None or not dispatcher_mod._pid_alive(state.pid):
        return False
    if state.log_path is None:
        return False
    if not Path(state.log_path).exists():
        return False

    started_at = state.last_started_at
    if started_at is None:
        # No recorded start: can't grade a heartbeat window; defer to the
        # broad demotion sweep rather than adopt blind.
        return False

    last_hb = state.last_heartbeat_at
    if last_hb is not None and last_hb < started_at:
        last_hb = None

    try:
        status = evaluate(
            settings=settings.task_caps,
            last_heartbeat_at=last_hb,
            started_at=started_at,
            now=now,  # type: ignore[arg-type]
        )
    except ValueError:
        # Clock skew / future timestamp: defer to reconcile_orphans.
        return False

    return status.verdict is HeartbeatVerdict.HEALTHY


def _adopt_one_safely(
    task: Task,
    state: TaskState,
    queue_dir: Path,
    clock: Clock,
    settings: Settings,
    account: str | None,
) -> None:
    """Thread entrypoint — monitor an adopted worker, log on error.

    Mirrors :func:`runner.orchestrator._dispatch_one_safely`: a crash in
    one adoption thread must not take the supervisor down.
    """
    try:
        dispatcher_mod.adopt_worker(
            task=task,
            state=state,
            queue_dir=queue_dir,
            clock=clock,
            settings_caps=settings.task_caps,
            settings_failure_classifier=settings.failure_classifier,
            account=account,
        )
    except Exception:
        logger.exception("adopt_worker failed for task %s", task.id)


__all__ = ["AdoptionResult", "adopt_running_workers"]
