"""Bridge between the supervisor's state machine and the runner's dispatcher.

The supervisor's :func:`run_one_tick` decides whether the queue is in a
dispatching state (``DISPATCHING`` / ``SLOWING_DOWN`` / ``IDLE``)
but the original codebase does not wire the supervisor to
:func:`runner.dispatcher.dispatch`. This module fills that gap: each tick
the daemon calls :func:`tick_dispatch`, which:

1. Reaps finished dispatch threads from the in-flight set.
2. Computes the target concurrency for this tick.
3. Picks eligible pending tasks (no ``running`` state file, all
   ``depends_on`` IDs are ``completed``) up to ``target - in_flight``.
4. For each candidate calls :func:`runner.account_dispatch.choose_account`
   to pick the account to dispatch through (equal-priority across
   accounts, least 5h util wins). Skips the task when no account has
   capacity.
5. Spawns one ``threading.Thread`` per dispatched task that calls
   :func:`runner.dispatcher.dispatch` with the chosen account's
   ``config_dir`` / ``linux_user``. Threads are non-daemon so the
   supervisor process won't exit until in-flight tasks finish.

The supervisor's snapshot is read-only here; we never emit ``DispatchTask``
actions back to the state machine. Throttle decisions stay in the state
machine. The orchestrator returns the updated snapshot (with refreshed
:class:`InFlightRecord` list) so the daemon can persist it.
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
from claude_task_runner.runner import account_dispatch as account_dispatch_mod
from claude_task_runner.runner import dispatcher as dispatcher_mod
from claude_task_runner.runner.in_flight import DispatchSlot, to_in_flight_records
from claude_task_runner.runner.session import plan_next_spawn
from claude_task_runner.supervisor.states import SupervisorState

if TYPE_CHECKING:
    from claude_task_runner.clock import Clock
    from claude_task_runner.config.schema import ResolvedAccount, Settings
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
            # Deliberate skip-and-warn (not fail-fast): one corrupt task
            # YAML must not blind the operator to the rest of the pending
            # pool. The path is logged so the bad file is locatable.
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
    in_flight_slots: dict[str, DispatchSlot],
    accounts: list[ResolvedAccount] | None = None,
    claude_executable: str = "claude",
    draining: bool = False,
) -> SupervisorSnapshot:
    """Reap finished threads and dispatch new tasks up to the target.

    Dispatch-thread lifetime (ADR-0025): when
    ``[supervisor].adopt_workers`` is on, dispatch threads are spawned as
    daemon threads so a fast stop (SIGTERM) can exit the supervisor
    promptly without joining them — the file-backed workers keep running
    as independent OS processes and the next supervisor adopts them.
    When adoption is off, threads are non-daemon so the graceful-drain
    stop can join them, preserving the historical behaviour exactly.

    Mutates ``in_flight_slots`` in place: removes finished threads,
    inserts newly-spawned :class:`DispatchSlot` entries (one per
    dispatched task, with account attribution).

    Returns the snapshot with refreshed
    :attr:`SupervisorSnapshot.in_flight` (the live attributed list)
    so the daemon can persist it without recomputing.

    ``accounts`` (when ``None``) is resolved from ``settings`` on the
    spot; pass it explicitly to share one resolution across multiple
    callers per tick.

    ``draining`` (PR 11): when ``True``, the function reaps finished
    threads and refreshes the in_flight snapshot — but does not
    enumerate candidates or dispatch new work. Used by the daemon's
    graceful-restart path so the supervisor can ride out its
    in-flight tasks before exiting.

    Dispatch gating (PR 9): a tick proceeds to candidate enumeration
    if AT LEAST ONE configured account is dispatchable (see
    :func:`_any_account_dispatchable`). The historical top-level
    ``snapshot.state`` gate was retired because, after PR 8's per-
    account capture, ``snapshot.state`` mirrors only the most-recently
    captured account — so a strict top-level gate alternated between
    "open" and "closed" each tick when one account was throttled and
    another was fine, halving effective dispatch throughput.
    """
    _reap_finished(in_flight_slots)

    if draining:
        # Drain mode: skip both the candidate enumeration and
        # choose_account routing. The reap above handled any threads
        # that finished since the last tick; the refresh below
        # surfaces the still-running set into the snapshot so the
        # daemon's persist step records accurate in_flight attribution.
        return _refresh_in_flight(snapshot, in_flight_slots)

    if accounts is None:
        # Local import to avoid a circular at module top: loader
        # imports schema; schema is consumed by the rest of the codebase.
        from claude_task_runner.config.loader import resolve_accounts

        accounts = resolve_accounts(settings)
    accounts_by_name = {a.name: a for a in accounts}

    # Per-account dispatch gate (replaces PR 5's top-level snapshot.state
    # gate). After PR 8's per-account capture, the top-level snapshot.state
    # mirrors whichever account was most recently captured — so a gate
    # that reads it alone alternates between "open" and "closed" each
    # tick, halving effective dispatch throughput in multi-account setups
    # where one account is throttled (e.g. high weekly util) while the
    # other is fine. Instead, gate per-account: if AT LEAST ONE configured
    # account is dispatchable (its AccountState is in _DISPATCHABLE_STATES
    # and it's not operator-paused), let choose_account do the actual
    # per-task routing.
    if not _any_account_dispatchable(snapshot, accounts_by_name):
        return _refresh_in_flight(snapshot, in_flight_slots)

    target = _target_concurrency(queue_dir, settings, snapshot)
    available = max(0, target - len(in_flight_slots))
    if available == 0:
        return _refresh_in_flight(snapshot, in_flight_slots)

    # ADR-0025 thread lifetime: daemon when adoption is on (fast stop need
    # not join), non-daemon otherwise (drain joins). ``getattr`` tolerates
    # the lightweight ``SimpleNamespace`` settings stubs some tests pass
    # (which omit ``supervisor``); the real strict ``Settings`` always
    # carries the field, so production reads the operator's value.
    adopt_on = bool(getattr(getattr(settings, "supervisor", None), "adopt_workers", False))

    completed_ids = _completed_task_ids(queue_dir)
    candidates = _eligible_candidates(queue_dir, in_flight_slots, completed_ids)
    if not candidates:
        return _refresh_in_flight(snapshot, in_flight_slots)

    candidates.sort(key=priority_sort_key)

    dispatched = 0
    for task in candidates:
        if dispatched >= available:
            break
        # Resolve the affined account from the task's state YAML so
        # choose_account can honour session affinity (ADR-0024):
        # multi-account queues must resume a Claude session on the
        # account that created it — sessions are namespaced by
        # CLAUDE_CONFIG_DIR. ``session_host_account`` returns None
        # when the task has no session yet (first attempt) or when
        # state load fails (which falls back to a fresh dispatch on
        # whichever account choose_account picks).
        affined = _affined_account_for_task(queue_dir, task.id)
        # Build the per-tick view of attributed in-flight tasks so
        # choose_account sees each newly-dispatched slot as it lands.
        live_in_flight = to_in_flight_records(in_flight_slots)
        choice = account_dispatch_mod.choose_account(
            task=task,
            accounts=accounts_by_name,
            account_states=dict(snapshot.accounts),
            in_flight=live_in_flight,
            affined_account=affined,
        )
        if choice.account is None:
            logger.info("dispatch skipped task %s: %s", task.id, choice.reason)
            continue
        acct = accounts_by_name[choice.account]
        thread = threading.Thread(
            target=_dispatch_one_safely,
            args=(
                task,
                queue_dir,
                settings,
                clock,
                claude_executable,
                acct.config_dir,
                acct.linux_user,
                choice.account,
            ),
            name=f"dispatch-{task.id}",
            # ADR-0025: daemon when adoption is on so a fast stop need not
            # join the worker thread; the file-backed worker survives as
            # its own process and is adopted by the next supervisor.
            daemon=adopt_on,
        )
        thread.start()
        in_flight_slots[task.id] = DispatchSlot(
            task_id=task.id,
            account=choice.account,
            started_at=clock.now(),
            thread=thread,
        )
        dispatched += 1
        logger.info(
            "dispatched task %s via account=%s (in_flight=%d, %s)",
            task.id,
            choice.account,
            len(in_flight_slots),
            choice.reason,
        )

    return _refresh_in_flight(snapshot, in_flight_slots)


def _refresh_in_flight(
    snapshot: SupervisorSnapshot,
    in_flight_slots: dict[str, DispatchSlot],
) -> SupervisorSnapshot:
    """Return ``snapshot`` with ``in_flight`` rebuilt from ``in_flight_slots``.

    Also keeps the legacy ``in_flight_task_ids`` field in sync so v2
    consumers still see the task-id list. The two fields are
    redundant by design (see :class:`SupervisorSnapshot` docstring).
    """
    records = to_in_flight_records(in_flight_slots)
    return snapshot.model_copy(
        update={
            "in_flight": records,
            "in_flight_task_ids": [r.task_id for r in records],
        }
    )


def _any_account_dispatchable(
    snapshot: SupervisorSnapshot,
    accounts_by_name: dict[str, ResolvedAccount],
) -> bool:
    """Return True iff at least one configured account would let a task land.

    "Would let a task land" means the account's :class:`AccountState`:
      * exists in ``snapshot.accounts`` (so the daemon has captured it),
      * is in :data:`account_dispatch._DISPATCHABLE_STATES`
        (DISPATCHING / SLOWING_DOWN / IDLE), AND
      * is not operator-paused via ``account pause``.

    Cold start: when no account has been captured yet, every entry's
    state is the seeded IDLE — which is in the dispatchable set — so
    this function returns True and ``choose_account`` does the actual
    routing.

    Note: this gate intentionally does NOT check per-account capacity
    (max_concurrency); that's :func:`account_dispatch.choose_account`'s
    job and applies per-task. The gate's job is to short-circuit when
    NOTHING can dispatch this tick so we don't pay the candidate
    enumeration cost.
    """
    if not snapshot.accounts:
        return False
    for name in accounts_by_name:
        state = snapshot.accounts.get(name)
        if state is None:
            continue
        if state.paused:
            continue
        if state.state in _account_dispatch_mod_states():
            return True
    return False


def _account_dispatch_mod_states() -> frozenset[SupervisorState]:
    """Lazy import of ``account_dispatch._DISPATCHABLE_STATES`` to avoid
    a module-load circular and keep the source of truth in one place."""
    return account_dispatch_mod._DISPATCHABLE_STATES


def _reap_finished(in_flight_slots: dict[str, DispatchSlot]) -> None:
    finished = [tid for tid, slot in in_flight_slots.items() if not slot.thread.is_alive()]
    for tid in finished:
        with contextlib.suppress(Exception):
            in_flight_slots[tid].thread.join(timeout=0.1)
        del in_flight_slots[tid]
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
        except Exception as exc:
            logger.warning("skipping unparseable state %s: %s", sp, exc)
            continue
        if state.status == "completed":
            return True
    return False


def _completed_task_ids(queue_dir: Path) -> set[str]:
    ids: set[str] = set()
    for sp in list_state_files(queue_dir):
        try:
            state = load_state(sp)
        except Exception as exc:
            logger.warning("skipping unparseable state %s: %s", sp, exc)
            continue
        if state.status == "completed":
            ids.add(state.task_id)
    return ids


def _affined_account_for_task(queue_dir: Path, task_id: str) -> str | None:
    """Resolve the host account for ``task_id``'s current session.

    Returns ``None`` when there is no state file, the state cannot be
    parsed, the task has no ``session_id``, or no account can be
    derived (legacy state with empty ``runs``). The dispatcher treats
    ``None`` as "no affinity constraint" — the next attempt starts
    fresh on whichever account ``choose_account`` picks.
    """
    sp = state_path_for(queue_dir, task_id)
    if not sp.exists():
        return None
    try:
        state = load_state(sp)
    except Exception:
        return None
    return state.session_host_account()


def _eligible_candidates(
    queue_dir: Path,
    in_flight_slots: dict[str, DispatchSlot],
    completed_ids: set[str],
) -> list[Task]:
    out: list[Task] = []
    in_flight_ids = set(in_flight_slots.keys())

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
            except Exception as exc:
                # Unparseable state file (disk corruption, a partial
                # write from a crashed dispatcher). Do NOT treat this as
                # "not yet dispatched": that silently makes the task
                # eligible, so a *completed* task could be re-dispatched —
                # duplicate work and wasted cap. Skip the task and log so
                # the corruption is visible; an operator (or the doctor
                # check) must delete or rebuild the state file. Surfacing
                # this via doctor is tracked separately.
                logger.error("skipping task %s: unparseable state %s: %s", task.id, sp, exc)
                continue
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
                if state.status == "awaiting_sidecar" and task.id in open_sidecar_task_ids:
                    # A request is still unanswered — keep the task
                    # ineligible until the operator responds.
                    continue
                if state.status != "awaiting_sidecar":
                    # Any other non-dispatchable status (running,
                    # completed, failed_circuit_breaker, ...) stays
                    # skipped.
                    continue
                # awaiting_sidecar with every request answered: fall
                # through to the depends_on check and add to out.

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
    claude_config_dir: str,
    linux_user: str | None,
    account: str,
) -> None:
    """Thread entrypoint — load state, plan spawn, call dispatch, log errors.

    ``claude_config_dir`` / ``linux_user`` / ``account`` are resolved
    per-dispatch by the orchestrator via
    :func:`runner.account_dispatch.choose_account` and routed through to
    :func:`runner.dispatcher.dispatch` so the spawned ``claude``
    subprocess hits the right account's credentials (and uid).
    """
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
            settings_dispatch=settings.dispatch,
            claude_executable=claude_executable,
            claude_config_dir=claude_config_dir,
            linux_user=linux_user,
            account=account,
            # ADR-0025: file-backed, restart-survivable workers when the
            # operator left adoption on (default). When off, dispatch
            # keeps the legacy pipe-backed behaviour bit-for-bit. ``getattr``
            # tolerates the SimpleNamespace settings stubs in some tests;
            # the real strict ``Settings`` always carries the field.
            adopt_workers=bool(
                getattr(getattr(settings, "supervisor", None), "adopt_workers", False)
            ),
        )
    except Exception:
        logger.exception("dispatch failed for task %s", task.id)
