"""Bridge between the supervisor's state machine and the runner's dispatcher.

The supervisor's :func:`run_one_tick` decides whether the queue is in a
dispatching state (``DISPATCHING`` / ``SLOWING_DOWN`` / ``END_OF_WEEK_PUSH``)
but the original codebase does not wire the supervisor to
:func:`runner.dispatcher.dispatch`. This module fills that gap: each tick
the daemon calls :func:`tick_dispatch`, which:

1. Reaps finished dispatch threads from the in-flight set.
2. Computes the target concurrency for this tick (a simple heuristic
   based on the supervisor state, deliberately conservative — the runner's
   :mod:`runner.concurrency` math is reserved for a future EMA-aware
   policy).
3. Picks eligible pending tasks (no `running` state file, all
   ``depends_on`` IDs are ``completed``) up to ``target - in_flight``.
4. Spawns one ``threading.Thread`` per task that calls
   :func:`runner.dispatcher.dispatch`. Threads are non-daemon so the
   supervisor process won't exit until in-flight tasks finish, even after
   SIGTERM.

The supervisor's snapshot is read-only here; we never emit ``DispatchTask``
actions back to the state machine. Throttle decisions stay in the state
machine.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from claude_task_runner.queue.schema import Task, TaskState
from claude_task_runner.queue.sidecar import list_open_sidecars
from claude_task_runner.queue.store import (
    list_pending_tasks,
    list_state_files,
    load_state,
    load_task,
    state_path_for,
)
from claude_task_runner.runner import dispatcher as dispatcher_mod
from claude_task_runner.runner.session import plan_next_spawn
from claude_task_runner.supervisor.states import SupervisorState

if TYPE_CHECKING:
    from claude_task_runner.clock import Clock
    from claude_task_runner.config.schema import Settings
    from claude_task_runner.supervisor.states import SupervisorSnapshot

logger = logging.getLogger(__name__)


_PRIORITY_ORDER = {"high": 0, "normal": 1, "low": 2}


def priority_sort_key(task: Task) -> tuple[int, str]:
    """Public sort key matching the orchestrator's dispatch order.

    Returns ``(priority_rank, task_id)`` where rank is 0 for ``high``,
    1 for ``normal``, 2 for ``low``, and 99 for any unrecognized value
    (sinks to the back). Exposed so the CLI's
    ``queue list --order-by-dispatch`` flag can show the same ordering
    the supervisor will actually apply.
    """
    return (_PRIORITY_ORDER.get(task.priority, 99), task.id)


def planned_dispatch_order(queue_dir: Path) -> list[Task]:
    """Return all pending tasks sorted in the order the supervisor would dispatch.

    Does NOT consult ``depends_on``, in-flight state, or per-tick
    capacity — it shows the ordering of the pending pool, which is
    what the operator usually wants to verify. A task currently in
    ``running`` / ``awaiting_sidecar`` / ``completed`` is still listed
    if its YAML is in ``todo/``; consumers can cross-reference with
    ``queue states`` if they need state-filtered output.
    """
    tasks: list[Task] = []
    for path in list_pending_tasks(queue_dir):
        try:
            tasks.append(load_task(path))
        except Exception as exc:
            logger.warning("skipping unparseable task at %s: %s", path, exc)
    tasks.sort(key=priority_sort_key)
    return tasks


# Task statuses that ARE eligible for (re-)dispatch.
#
# This is an explicit allow-list: every status defined in
# :data:`claude_task_runner.queue.schema.TaskStatus` defaults to
# NOT-eligible, and a status only becomes eligible by being added to
# this set. The previous design was a deny-list ("skip these statuses,
# everything else is eligible"); that pattern silently treated
# `awaiting_sidecar` as eligible because it had not been added to the
# skip set, producing an infinite re-dispatch loop where each agent
# filed a new sidecar request and the orchestrator immediately picked
# the task up again. An allow-list flips the failure mode: a newly
# introduced status is fail-safe (not dispatched) until someone
# deliberately opts it in, which is the right default for a
# concurrency-burning side effect like dispatch.
#
# Currently dispatchable: tasks that have not yet started (`pending`)
# and tasks whose previous attempt failed for a transient reason
# (`failed`; the dispatcher's circuit breaker upgrades repeated
# `failed`s to `failed_circuit_breaker`, which is not in this set).
#
# Explicitly NOT dispatched (and the rationale):
#   - `running`               — already in flight in another thread.
#   - `awaiting_sidecar`      — waiting for an operator response;
#                               re-dispatch would just file another sidecar.
#   - `possibly_hung`         — heartbeat watchdog territory; the runner
#                               diagnoses, not the orchestrator.
#   - `completed`             — done.
#   - `failed_circuit_breaker`— give-up state; operator intervention required.
#   - `weekly_paused`         — throttled; the supervisor's state machine
#                               will lift this when the window opens.
_DISPATCHABLE_STATUSES = frozenset({"pending", "failed"})


def tick_dispatch(
    *,
    queue_dir: Path,
    settings: Settings,
    clock: Clock,
    snapshot: SupervisorSnapshot,
    in_flight_threads: dict[str, threading.Thread],
    claude_executable: str = "claude",
) -> None:
    """Reap finished threads and dispatch new tasks up to the target.

    Mutates ``in_flight_threads`` in place: removes finished threads,
    inserts newly-spawned ones keyed by ``task.id``.
    """
    _reap_finished(in_flight_threads)

    if snapshot.state not in (
        SupervisorState.DISPATCHING,
        SupervisorState.SLOWING_DOWN,
        SupervisorState.END_OF_WEEK_PUSH,
    ):
        return

    target = _target_concurrency(queue_dir, settings, snapshot)
    available = max(0, target - len(in_flight_threads))
    if available == 0:
        return

    completed_ids = _completed_task_ids(queue_dir)
    candidates = _eligible_candidates(queue_dir, in_flight_threads, completed_ids)
    if not candidates:
        return

    candidates.sort(key=priority_sort_key)

    for task in candidates[:available]:
        thread = threading.Thread(
            target=_dispatch_one_safely,
            args=(task, queue_dir, settings, clock, claude_executable),
            name=f"dispatch-{task.id}",
            daemon=False,
        )
        thread.start()
        in_flight_threads[task.id] = thread
        logger.info("dispatched task %s in thread (in_flight=%d)", task.id, len(in_flight_threads))


def _reap_finished(in_flight_threads: dict[str, threading.Thread]) -> None:
    finished = [tid for tid, th in in_flight_threads.items() if not th.is_alive()]
    for tid in finished:
        with contextlib.suppress(Exception):
            in_flight_threads[tid].join(timeout=0.1)
        del in_flight_threads[tid]
        logger.info("reaped finished dispatch thread for task %s", tid)


def _target_concurrency(
    queue_dir: Path,
    settings: Settings,
    snapshot: SupervisorSnapshot,
) -> int:
    """Conservative target: ``initial_concurrency`` until at least one task
    has completed in this queue, then ``max_concurrency``. Halved while
    SLOWING_DOWN.
    """
    have_warmup = _has_any_completed(queue_dir)
    base = (
        settings.concurrency.max_concurrency
        if have_warmup
        else settings.concurrency.initial_concurrency
    )
    base = max(1, base)
    if snapshot.state is SupervisorState.SLOWING_DOWN:
        return max(1, base // 2)
    return base


def _has_any_completed(queue_dir: Path) -> bool:
    for sp in list_state_files(queue_dir):
        try:
            state = load_state(sp)
        except Exception:
            continue
        if state.status == "completed":
            return True
    return False


def _completed_task_ids(queue_dir: Path) -> set[str]:
    ids: set[str] = set()
    for sp in list_state_files(queue_dir):
        try:
            state = load_state(sp)
        except Exception:
            continue
        if state.status == "completed":
            ids.add(state.task_id)
    return ids


def _eligible_candidates(
    queue_dir: Path,
    in_flight_threads: dict[str, threading.Thread],
    completed_ids: set[str],
) -> list[Task]:
    out: list[Task] = []
    in_flight_ids = set(in_flight_threads.keys())

    # Cache the open-sidecar set once per call rather than scanning the
    # sidecar directory inside every per-task branch. A task is
    # "sidecar-open" if there's at least one request-NNN.json without a
    # matching response-NNN.json.
    open_sidecar_task_ids: set[str] = {tid for tid, _seq, _path in list_open_sidecars(queue_dir)}

    for path in list_pending_tasks(queue_dir):
        try:
            task = load_task(path)
        except Exception as exc:
            logger.warning("skipping unparseable task at %s: %s", path, exc)
            continue
        if task.id in in_flight_ids:
            continue

        sp = state_path_for(queue_dir, task.id)
        if sp.exists():
            try:
                state = load_state(sp)
            except Exception:
                # Unparseable state file — treat as "not yet dispatched".
                # The next attempt will overwrite it cleanly.
                pass
            else:
                if state.status not in _DISPATCHABLE_STATUSES:
                    # Special case: awaiting_sidecar tasks become
                    # dispatchable AGAIN once every sidecar request has
                    # a matching response file. Without this, a task
                    # that stopped to ask an operator question stays
                    # stuck forever — the operator's response file is
                    # written but the orchestrator never re-evaluates.
                    # Fixed 2026-05-15 after live observation that 5
                    # answered sidecars on the popPK queue had stayed
                    # in awaiting_sidecar for >90 minutes despite
                    # response-001.json files being in place.
                    if state.status == "awaiting_sidecar" and task.id not in open_sidecar_task_ids:
                        # All requests answered — fall through to the
                        # depends_on check and add to out.
                        pass
                    else:
                        continue

        unmet = [d for d in task.depends_on if d not in completed_ids]
        if unmet:
            continue

        out.append(task)
    return out


def _dispatch_one_safely(
    task: Task,
    queue_dir: Path,
    settings: Settings,
    clock: Clock,
    claude_executable: str,
) -> None:
    """Thread entrypoint — load state, plan spawn, call dispatch, log errors."""
    sp = state_path_for(queue_dir, task.id)
    if sp.exists():
        try:
            state = load_state(sp)
        except Exception:
            state = TaskState(task_id=task.id)
    else:
        state = TaskState(task_id=task.id)

    try:
        plan = plan_next_spawn(task, state, settings=settings.session)
        dispatcher_mod.dispatch(
            task=task,
            state=state,
            plan=plan,
            queue_dir=queue_dir,
            clock=clock,
            settings_caps=settings.task_caps,
            settings_session=settings.session,
            settings_hooks=settings.hooks,
            settings_failure_classifier=settings.failure_classifier,
            claude_executable=claude_executable,
            claude_config_dir=settings.claude.config_dir,
        )
    except Exception:
        logger.exception("dispatch failed for task %s", task.id)
