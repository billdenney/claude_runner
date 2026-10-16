"""Startup adoption of restart-survivable workers (ADR-0025).

When ``[supervisor].adopt_workers`` is on, the dispatcher redirects each
worker's stdout/stderr to per-attempt files and records the stdout
``log_path`` on the task's :class:`TaskState`. A worker therefore
survives the supervisor's exit (no pipe to EPIPE) and keeps writing its
log. On the next supervisor start, *before* the broad demotion sweep in
:func:`supervisor.reconcile.reconcile_orphans`, this module re-attaches
to those still-running workers instead of losing the in-flight work.

For each ``status="running"`` state with:

* a ``pid`` that is still its worker
  (:func:`runner.dispatcher.probe_worker`: the pid exists, and it is not
  a process that was given the pid after the worker exited),
* a present ``log_path`` whose file exists, and
* a HEALTHY verdict from the silent-orphan reaper's own classifier
  (:func:`supervisor.reconcile_silent.liveness_verdict`), which counts the
  agent heartbeat, the previous supervisor's monitor tick and the
  worker's own writes to its log,

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
  it adopt a hung worker;
* a live worker that is not adopted for any other reason ⇒
  ``reconcile_orphans`` parks it ``possibly_hung``, since a ``failed``
  task would be dispatched while the worker still runs.

The reaper and this pass use one classifier so they cannot disagree: a
worker the reaper leaves ``running`` is one this pass adopts. The agent
heartbeat alone does not do: the dispatcher does not advance it during a
long tool call (see :mod:`runner.stream`).

A worker can also finish in the restart gap, after one supervisor stops
and before the next starts. :func:`finalize_exited_workers` runs first of
all the recovery passes (after the corrupt-state quarantine) and records
such a worker's run from the terminal ``result`` event in its stream log,
as the adoption monitor would have. A dead worker whose log has no result
event crashed; that pass leaves it to the reaper and ``reconcile_orphans``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
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
from claude_task_runner.runner.dispatcher import WorkerProbe
from claude_task_runner.runner.heartbeat import HeartbeatVerdict
from claude_task_runner.runner.in_flight import DispatchSlot
from claude_task_runner.supervisor import reconcile_silent
from claude_task_runner.supervisor.states import InFlightRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdoptionResult:
    """One adopted worker, for the daemon's startup log / notify."""

    task_id: str
    pid: int
    log_path: str


@dataclass(frozen=True)
class ExitedWorkerResult:
    """One worker that exited in the restart gap and was recorded from its log."""

    task_id: str
    pid: int
    log_path: str
    status: str
    """The status the finalize wrote, e.g. ``completed`` or ``failed``."""
    stop_reason: str | None


UNATTRIBUTED_ACCOUNT = "(unknown)"
"""The account an adopted worker's slot names when no record says which
account it runs on. Not a valid account name, so it counts against no
account's ``max_concurrency``, only against an explicit
``[concurrency].max_concurrency``."""


def _prior_accounts(
    settings: Settings, prior_in_flight: Sequence[InFlightRecord]
) -> dict[str, str]:
    """Task id → the account the previous supervisor's in-flight record names,
    for records naming an account the queue declares."""
    declared = {acct.name for acct in settings.accounts}
    return {r.task_id: r.account for r in prior_in_flight if r.account in declared}


def adopt_running_workers(
    queue_dir: Path,
    *,
    settings: Settings,
    clock: Clock,
    in_flight_slots: dict[str, DispatchSlot],
    prior_in_flight: Sequence[InFlightRecord] = (),
) -> list[AdoptionResult]:
    """Adopt HEALTHY restart-survivable workers; return what was adopted.

    Mutates ``in_flight_slots`` in place: each adopted task gets a new
    :class:`DispatchSlot` whose thread monitors the orphaned worker to
    completion. Returns one :class:`AdoptionResult` per adopted task so
    the caller can (a) shield them from ``reconcile_orphans`` and (b)
    surface them to the operator.

    The slot names the account the worker runs on, so the account's own
    ``max_concurrency`` counts it. A running attempt's state YAML does not
    record that account (a run's account is written when it finalizes),
    so the first source is ``prior_in_flight``, the previous supervisor's
    persisted ``in_flight`` list, as for :func:`finalize_exited_workers`.
    Then the state's session host account (a resumed attempt runs on it),
    then the only account of a single-account queue. With none of those,
    the slot names :data:`UNATTRIBUTED_ACCOUNT` and a WARNING says so.

    No-op (returns ``[]``) when ``[supervisor].adopt_workers`` is off —
    the legacy demote-on-restart path then applies unchanged.
    """
    if not settings.supervisor.adopt_workers:
        return []

    results: list[AdoptionResult] = []
    now = clock.now()
    prior_accounts = _prior_accounts(settings, prior_in_flight)
    sole_account = settings.accounts[0].name if len(settings.accounts) == 1 else None

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

        account = prior_accounts.get(state.task_id) or state.session_host_account() or sole_account
        if account is None:
            logger.warning(
                "adoption: no record names the account task %s's worker (pid %s) runs on; "
                "it counts against no account's max_concurrency until it finishes",
                state.task_id,
                state.pid,
            )
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
            account=account or UNATTRIBUTED_ACCOUNT,
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


def _is_adoptable(state: TaskState, *, now: datetime, settings: Settings) -> bool:
    """True iff ``state`` is a HEALTHY, file-backed running task whose worker runs.

    The verdict is :func:`supervisor.reconcile_silent.liveness_verdict`,
    the one the startup reaper acted on just before. A state it cannot
    grade (no ``last_started_at``, a future timestamp) is not adopted.
    """
    if state.status != "running":
        return False
    if state.pid is None or state.log_path is None:
        return False
    if not Path(state.log_path).exists():
        return False
    if dispatcher_mod.probe_recorded_worker(state) is not WorkerProbe.ALIVE:
        return False
    verdict = reconcile_silent.liveness_verdict(state, settings=settings.task_caps, now=now)
    if verdict is not HeartbeatVerdict.HEALTHY:
        logger.info(
            "adoption: not adopting task %s: its worker pid=%s runs but its liveness verdict is %s",
            state.task_id,
            state.pid,
            verdict,
        )
        return False
    return True


def finalize_exited_workers(
    queue_dir: Path,
    *,
    settings: Settings,
    clock: Clock,
    prior_in_flight: Sequence[InFlightRecord] = (),
) -> list[ExitedWorkerResult]:
    """Record the runs of file-backed workers that exited while no supervisor ran.

    For each ``status="running"`` state whose recorded ``pid`` is dead and
    whose ``log_path`` exists, the log goes through
    :func:`runner.dispatcher.finalize_exited_worker`. A log that ends in a
    terminal ``result`` event is finalized as the adoption monitor would
    have finalized it: a RunRecord, the completed/failed classification,
    the owned path's gates, the recheck guard and the post-dispatch hook.
    A log without one is left alone. That worker crashed, and the silent-orphan reaper and
    ``reconcile_orphans`` handle it as before.

    Must run before :func:`supervisor.reconcile_silent.reconcile_silent_orphans`.
    A finished worker's heartbeat is as stale as a hung one's, so the
    reaper would park it ``possibly_hung``, or ``reconcile_orphans`` would
    demote it to ``failed`` and re-dispatch work that is already done.

    ``prior_in_flight`` is the previous supervisor's persisted
    ``in_flight`` list. It names the account each running task was
    dispatched through, which a first attempt's state YAML does not: the
    state records a session's account only when a run finalizes. Records
    naming an account the queue does not declare are ignored, since an
    unknown affinity account blocks dispatch
    (:func:`runner.account_dispatch.choose_account`).
    ``state.session_host_account()`` is the fallback.

    No-op (returns ``[]``) when ``[supervisor].adopt_workers`` is off, the
    kill switch for the rest of ADR-0025.
    """
    if not settings.supervisor.adopt_workers:
        return []

    prior_accounts = _prior_accounts(settings, prior_in_flight)
    results: list[ExitedWorkerResult] = []

    for state_path in list_state_files(queue_dir):
        try:
            state = load_state(state_path)
        except Exception as exc:
            logger.warning(
                "exited-worker finalize: skipping unparseable state file %s: %s",
                state_path,
                exc,
            )
            continue

        if not _has_exited(state):
            continue

        assert state.pid is not None  # narrowed by _has_exited
        assert state.log_path is not None  # narrowed by _has_exited

        try:
            task = load_task(task_path_for(queue_dir, state.task_id))
        except Exception as exc:
            # The finalize needs the Task for the ADR-0020 output gate.
            logger.warning(
                "exited-worker finalize: task %s's worker exited but its Task "
                "YAML could not be loaded (%s); leaving it for reconcile_orphans",
                state.task_id,
                exc,
            )
            continue

        try:
            outcome = dispatcher_mod.finalize_exited_worker(
                task=task,
                state=state,
                queue_dir=queue_dir,
                clock=clock,
                settings_dispatch=settings.dispatch,
                settings_hooks=settings.hooks,
                settings_failure_classifier=settings.failure_classifier,
                account=prior_accounts.get(state.task_id) or state.session_host_account(),
            )
        except Exception:
            # One task must not keep the supervisor from starting. The
            # later passes still demote it, as before this pass existed.
            logger.exception(
                "exited-worker finalize failed for task %s; leaving it for the "
                "silent-orphan reaper and reconcile_orphans",
                state.task_id,
            )
            continue
        if outcome is None:
            logger.info(
                "task %s: worker pid=%s exited with no result event in %s; "
                "leaving it to the silent-orphan reaper and reconcile_orphans",
                state.task_id,
                state.pid,
                state.log_path,
            )
            continue
        if outcome.new_state.runs[-1:] != [outcome.run_record]:
            # The recheck guard found the task already off "running" and
            # stood down, so the new state is the other writer's, not ours.
            continue

        results.append(
            ExitedWorkerResult(
                task_id=state.task_id,
                pid=state.pid,
                log_path=state.log_path,
                status=outcome.new_state.status,
                stop_reason=outcome.new_state.stop_reason,
            )
        )
        logger.info(
            "recorded exited worker for task %s from its log (pid=%s, log=%s): "
            "status=%s stop_reason=%s",
            state.task_id,
            state.pid,
            state.log_path,
            outcome.new_state.status,
            outcome.new_state.stop_reason,
        )

    return results


def _has_exited(state: TaskState) -> bool:
    """True iff ``state`` is a file-backed ``running`` task whose worker is gone.

    Gone means no process has the pid, or another process does
    (:func:`runner.dispatcher.probe_worker`). Needs a recorded ``pid`` to
    probe. Without one there is no telling whether the worker still runs,
    so the task stays with the later passes.
    """
    if state.status != "running":
        return False
    if state.pid is None or state.log_path is None:
        return False
    if not Path(state.log_path).exists():
        return False
    return dispatcher_mod.probe_recorded_worker(state) is not WorkerProbe.ALIVE


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
            settings_dispatch=settings.dispatch,
            settings_hooks=settings.hooks,
            settings_failure_classifier=settings.failure_classifier,
            account=account,
        )
    except Exception:
        logger.exception("adopt_worker failed for task %s", task.id)


__all__ = [
    "AdoptionResult",
    "ExitedWorkerResult",
    "adopt_running_workers",
    "finalize_exited_workers",
]
