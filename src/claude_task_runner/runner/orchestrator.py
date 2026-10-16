"""Bridge between the supervisor's state machine and the runner's dispatcher.

The supervisor's :func:`run_one_tick` decides whether the queue is in a
dispatching state (``DISPATCHING`` / ``SLOWING_DOWN`` / ``IDLE``)
but the original codebase does not wire the supervisor to
:func:`runner.dispatcher.dispatch`. This module fills that gap: each tick
the daemon calls :func:`tick_dispatch`, which:

1. Reaps finished dispatch threads from the in-flight set.
2. Computes how many tasks this tick may start: each account's free
   slots under its own cap, within ``[concurrency]`` when the queue TOML
   sets it.
3. Picks eligible pending tasks (no ``running`` state file, all
   ``depends_on`` IDs are ``completed``), up to that number.
4. For each candidate calls :func:`runner.account_dispatch.choose_account`
   to pick the account to dispatch through (equal-priority across
   accounts, least 5h util wins). Skips the task when no account has
   capacity. Each account's capacity includes the ``target_concurrency``
   of its last throttle decision, so a SLOWING_DOWN account runs at
   most the ramp target the supervisor announced (ADR-0022).
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
import json
import logging
import threading
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_task_runner.queue.schema import RunRecord, Task, TaskState
from claude_task_runner.queue.sidecar import list_open_sidecars
from claude_task_runner.queue.store import (
    list_pending_tasks,
    list_state_files,
    load_state,
    load_task,
    state_path_for,
    write_state_atomic,
)
from claude_task_runner.runner import account_dispatch as account_dispatch_mod
from claude_task_runner.runner import dispatcher as dispatcher_mod
from claude_task_runner.runner import effort_levels as effort_levels_mod
from claude_task_runner.runner import readiness as readiness_mod
from claude_task_runner.runner.in_flight import DispatchSlot, to_in_flight_records
from claude_task_runner.runner.session import plan_next_spawn
from claude_task_runner.runner.spawn_gate import SpawnGate
from claude_task_runner.supervisor import adoption as adoption_mod
from claude_task_runner.supervisor import reconcile as reconcile_mod
from claude_task_runner.supervisor.states import InFlightRecord, SupervisorState

if TYPE_CHECKING:
    from claude_task_runner.clock import Clock
    from claude_task_runner.config.schema import ResolvedAccount, Settings
    from claude_task_runner.supervisor.states import SupervisorSnapshot

logger = logging.getLogger(__name__)

NotifyCallback = Callable[[str, str], None]
EventCallback = Callable[[str, dict[str, Any]], None]
ParkCallback = Callable[[TaskState, str], None]
"""Receives a task just parked with its live worker on record, and the words
that complete "worker pid=N is still running ..." for it."""


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

# Categories the selector counts a skipped task under (see
# :func:`_skip_category`); they are the names the "no eligible tasks" line
# prints. Every category is one snake_case word, so the line reads as
# ``name count`` pairs and greps cleanly: the task-state or register name
# where one exists (a status that is never dispatched is reported as the
# status itself, ``running``, ``possibly_hung``, ...; ``block_dispatch``;
# ``depends_on``), a snake_case phrase otherwise. A task held by readiness
# is ``readiness_`` plus the categories of its unmet requirements, sorted
# and joined by ``+`` (``readiness_missing_file``,
# ``readiness_awaiting_sidecar_response+missing_file``).
_SKIP_UNPARSEABLE_TASK = "unparseable_task"
_SKIP_BLOCK_DISPATCH = "block_dispatch"
_SKIP_IN_FLIGHT = "in_flight"
_SKIP_UNPARSEABLE_STATE = "unparseable_state"
_SKIP_UNANSWERED_SIDECAR = "unanswered_sidecar"
_SKIP_DEFERRED_COOLDOWN = "deferred_cooldown"
_SKIP_INVALID_EFFORT = "invalid_effort"
_SKIP_DEPENDS_ON = "depends_on"
_SKIP_READINESS_PREFIX = "readiness_"
_SKIP_LIVE_WORKER = "live_worker"
# Finished, not held: left out of the summary's unfinished count.
_COMPLETED = "completed"


def _unfinished_skips(skips: Mapping[str, int]) -> dict[str, int]:
    """``skips`` (category -> count, as :func:`_eligible_candidates` fills it)
    without the ``completed`` count."""
    return {name: n for name, n in skips.items() if name != _COMPLETED}


def _idle_summary(breakdown: Mapping[str, int]) -> str:
    """The log line for a tick whose selector admitted no task.

    ``breakdown`` is :func:`_unfinished_skips` of the selector's counts.
    Categories are listed largest first, ties by name, so the same breakdown
    always prints the same line.
    """
    if not breakdown:
        return "no eligible tasks: no unfinished tasks in todo/"
    parts = ", ".join(
        f"{name} {n}" for name, n in sorted(breakdown.items(), key=lambda item: (-item[1], item[0]))
    )
    return f"no eligible tasks: {sum(breakdown.values())} unfinished held ({parts})"


@dataclass
class IdleDispatchLog:
    """Says in the log why a dispatch tick found nothing to dispatch.

    An idle queue and a stalled supervisor otherwise look the same: both
    leave the log silent. The supervisor keeps one of these for its lifetime
    and passes it to every :func:`tick_dispatch`. When a tick gets as far as
    choosing candidates (some account can dispatch, a slot is free) and the
    selector admits none, one INFO line (:func:`_idle_summary`) breaks the
    unfinished tasks down by what holds them. Later empty ticks log nothing
    until that breakdown changes, so a queue that stays drained costs one
    line, not one per tick. The ``in_flight`` count is printed but is not a
    change on its own: it moves whenever a running task starts or finishes,
    which says nothing new about why the rest are held. The first tick that
    finds candidates after an empty spell logs that it did.
    """

    last_breakdown: dict[str, int] | None = None
    """The unfinished-task breakdown last logged; ``None`` when the last
    tick that chose candidates found some (or none has run yet)."""

    def observe(self, n_eligible: int, skips: Mapping[str, int]) -> None:
        """Record one selector pass: ``n_eligible`` admitted, the rest in ``skips``."""
        if n_eligible > 0:
            if self.last_breakdown is not None:
                logger.info("eligible tasks available again: %d", n_eligible)
                self.last_breakdown = None
            return
        breakdown = _unfinished_skips(skips)
        last = self.last_breakdown
        if last is not None and _without_in_flight(breakdown) == _without_in_flight(last):
            return
        self.last_breakdown = breakdown
        logger.info("%s", _idle_summary(breakdown))


def _without_in_flight(breakdown: Mapping[str, int]) -> dict[str, int]:
    """``breakdown`` without the ``in_flight`` count, for change detection."""
    return {name: n for name, n in breakdown.items() if name != _SKIP_IN_FLIGHT}


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
    notify_callback: NotifyCallback | None = None,
    event_callback: EventCallback | None = None,
    spawn_gate: SpawnGate | None = None,
    idle_log: IdleDispatchLog | None = None,
    parked_tracker: adoption_mod.ParkedWorkerTracker | None = None,
) -> SupervisorSnapshot:
    """Reap finished threads and dispatch new tasks up to the target.

    Dispatch-thread lifetime (ADR-0025): when
    ``[supervisor].adopt_workers`` is on, dispatch threads are spawned as
    daemon threads so a fast stop (SIGTERM) can exit the supervisor
    promptly without joining them — the file-backed workers keep running
    as independent OS processes and the next supervisor adopts them.
    When adoption is off, threads are non-daemon so the graceful-drain
    stop can join them, preserving the historical behaviour exactly.
    ``spawn_gate`` goes to each dispatch thread, so the stop still waits
    for a thread that has started its worker but not yet recorded its pid
    (see :mod:`runner.spawn_gate`).

    Mutates ``in_flight_slots`` in place: removes finished threads,
    inserts newly-spawned :class:`DispatchSlot` entries (one per
    dispatched task, with account attribution).

    Returns the snapshot with refreshed
    :attr:`SupervisorSnapshot.in_flight` (the live attributed list)
    so the daemon can persist it without recomputing.

    ``accounts`` (when ``None``) is resolved from ``settings`` on the
    spot, without the accounts whose ``runner-account.toml`` does not
    load; pass it explicitly to share one resolution across multiple
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

    ``idle_log`` is the supervisor's :class:`IdleDispatchLog`. A tick that
    reaches candidate selection reports to it how many tasks the selector
    admitted and what held the rest, and it logs a summary when none were
    admitted. ``None`` skips that report; dispatch is the same either way.

    ``parked_tracker`` is the supervisor's
    :class:`supervisor.adoption.ParkedWorkerTracker`, kept across ticks for
    its retry delays and the parks and recorded runs it queues between
    ticks; ``None`` uses a fresh one. Each parked worker that still runs
    counts against its account (``held``); a parked worker that has exited,
    or was stopped at its duration cap, has its run recorded on a thread
    that holds its task's slot. A task the selector parks this tick counts
    from this tick on. Every park made here, or by a dispatch thread since
    the previous tick, is reported (a ``warning`` notify and a
    ``live_worker_parked`` event).
    """
    _reap_finished(
        in_flight_slots,
        queue_dir=queue_dir,
        clock=clock,
        notify_callback=notify_callback,
        event_callback=event_callback,
    )

    # ADR-0025 thread lifetime: daemon when adoption is on (fast stop need
    # not join), non-daemon otherwise (drain joins). ``getattr`` tolerates
    # the lightweight ``SimpleNamespace`` settings stubs some tests pass
    # (which omit ``supervisor``); the real strict ``Settings`` always
    # carries the field, so production reads the operator's value.
    adopt_on = bool(getattr(getattr(settings, "supervisor", None), "adopt_workers", False))

    # A worker parked with no monitor thread (runner.dispatcher
    # .park_live_worker) still runs on an account: count it against that
    # account like a slot, and record its run once it exits.
    tracker = parked_tracker if parked_tracker is not None else adoption_mod.ParkedWorkerTracker()
    parked = tracker.tick(
        queue_dir,
        settings=settings,
        clock=clock,
        prior_in_flight=snapshot.in_flight,
        owned_ids=in_flight_slots.keys(),
    )
    _report_finished_parked(parked.finished, notify_callback, event_callback)
    _report_parks(tracker.drain_parks(), notify_callback, event_callback)
    for job in parked.to_finalize:
        _start_parked_finalize(job, tracker, queue_dir, settings, clock, in_flight_slots, adopt_on)
    held = list(parked.in_flight)

    if draining:
        # Drain mode: skip both the candidate enumeration and
        # choose_account routing. The reap above handled any threads
        # that finished since the last tick; the refresh below
        # surfaces the still-running set into the snapshot so the
        # daemon's persist step records accurate in_flight attribution.
        return _refresh_in_flight(snapshot, in_flight_slots, held)

    broken: dict[str, str] = {}
    if accounts is None:
        # Local import to avoid a circular at module top: loader
        # imports schema; schema is consumed by the rest of the codebase.
        from claude_task_runner.config.loader import resolve_accounts

        # An account whose runner-account.toml does not load is left out,
        # so it takes no tasks; the daemon logs it (ADR-0035). Its error
        # goes to choose_account so a decline names it.
        resolution = resolve_accounts(settings)
        accounts = list(resolution.accounts)
        broken = {b.name: b.error for b in resolution.broken}
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
        return _refresh_in_flight(snapshot, in_flight_slots, held)

    available = account_dispatch_mod.dispatch_headroom(
        accounts,
        [*to_in_flight_records(in_flight_slots), *held],
        _target_concurrency(queue_dir, settings),
    )
    if available == 0:
        return _refresh_in_flight(snapshot, in_flight_slots, held)

    completed_ids = _completed_task_ids(queue_dir)
    # Opt-in dispatch block-list (ADR-0029): a queue-relative JSONL of
    # `block_dispatch: true` task ids the selector skips outright.
    # ``getattr`` tolerates the SimpleNamespace settings stubs some
    # tests pass; the real strict ``Settings`` always carries the field.
    block_file = getattr(getattr(settings, "dispatch", None), "dispatch_block_file", None)
    skips: Counter[str] = Counter()
    candidates = _eligible_candidates(
        queue_dir,
        in_flight_slots,
        completed_ids,
        now=clock.now(),
        block_file=block_file,
        effort_levels=settings.effort_levels,
        skips=skips,
        on_park=tracker.note_park,
    )
    # A task the selector just parked (or a dispatch thread did since the
    # tick began) holds its account's slot from now on, not from next tick.
    newly_parked = tracker.drain_parks()
    _report_parks(newly_parked, notify_callback, event_callback)
    new_records = [
        tracker.record_for(
            state, settings=settings, prior_in_flight=snapshot.in_flight, now=clock.now()
        )
        for state, _why in newly_parked
        if state.task_id not in in_flight_slots
    ]
    if new_records:
        held = sorted([*held, *new_records], key=lambda r: r.task_id)
        available = min(
            available,
            account_dispatch_mod.dispatch_headroom(
                accounts,
                [*to_in_flight_records(in_flight_slots), *held],
                _target_concurrency(queue_dir, settings),
            ),
        )
    if idle_log is not None:
        idle_log.observe(len(candidates), skips)
    if not candidates:
        return _refresh_in_flight(snapshot, in_flight_slots, held)

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
        live_in_flight = [*to_in_flight_records(in_flight_slots), *held]
        choice = account_dispatch_mod.choose_account(
            task=task,
            accounts=accounts_by_name,
            account_states=dict(snapshot.accounts),
            in_flight=live_in_flight,
            affined_account=affined,
            broken=broken,
        )
        # Session-affinity TTL (ADR-0024 extension): affinity blocked the
        # dispatch (host account busy / throttled / paused), but if the
        # session has been idle past the affinity TTL its resume/cache
        # value is spent — so clear it and re-select on any eligible
        # account rather than leaving the task stranded on its throttled
        # host. Clearing first (session_id -> None) is what makes the
        # cross-account dispatch safe: the next spawn starts FRESH instead
        # of trying to resume a session that lives under the old account's
        # CLAUDE_CONFIG_DIR. Affinity is still honoured while the host has
        # capacity and for sessions younger than the TTL.
        if choice.account is None and affined is not None:
            ttl = getattr(getattr(settings, "dispatch", None), "affinity_ttl_seconds", 5400.0)
            if _session_affinity_expired(queue_dir, task.id, now=clock.now(), ttl_seconds=ttl):
                _clear_session_for_fresh_dispatch(queue_dir, task.id)
                choice = account_dispatch_mod.choose_account(
                    task=task,
                    accounts=accounts_by_name,
                    account_states=dict(snapshot.accounts),
                    in_flight=live_in_flight,
                    affined_account=None,
                    broken=broken,
                )
                if choice.account is not None:
                    logger.info(
                        "task %s: session affinity expired (idle > %.0fs on "
                        "host %r); cleared session, dispatching fresh via "
                        "account=%s",
                        task.id,
                        ttl,
                        affined,
                        choice.account,
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
            kwargs={"spawn_gate": spawn_gate, "on_park": tracker.note_park},
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

    return _refresh_in_flight(snapshot, in_flight_slots, held)


def _refresh_in_flight(
    snapshot: SupervisorSnapshot,
    in_flight_slots: dict[str, DispatchSlot],
    held: Sequence[InFlightRecord] = (),
) -> SupervisorSnapshot:
    """Return ``snapshot`` with ``in_flight`` rebuilt from ``in_flight_slots``
    and ``held``, the parked live workers' records
    (:func:`supervisor.adoption.track_parked_workers`), sorted by task id.

    Also keeps the legacy ``in_flight_task_ids`` field in sync so v2
    consumers still see the task-id list. The two fields are
    redundant by design (see :class:`SupervisorSnapshot` docstring).
    """
    records = sorted([*to_in_flight_records(in_flight_slots), *held], key=lambda r: r.task_id)
    return snapshot.model_copy(
        update={
            "in_flight": records,
            "in_flight_task_ids": [r.task_id for r in records],
        }
    )


def _report_finished_parked(
    finished: Sequence[adoption_mod.ExitedWorkerResult],
    notify_callback: NotifyCallback | None,
    event_callback: EventCallback | None,
) -> None:
    """Report each parked worker whose run was recorded this tick, as the
    daemon reports a worker that exited during a restart."""
    for ex in finished:
        if notify_callback is not None:
            notify_callback(
                "info",
                f"recorded exited worker for task {ex.task_id} from its log "
                f"(pid={ex.pid}): status={ex.status} stop_reason={ex.stop_reason}",
            )
        if event_callback is not None:
            event_callback(
                "exited_worker_finalized",
                {
                    "task_id": ex.task_id,
                    "pid": ex.pid,
                    "log_path": ex.log_path,
                    "status": ex.status,
                    "stop_reason": ex.stop_reason,
                },
            )


def _report_parks(
    parks: Sequence[tuple[TaskState, str]],
    notify_callback: NotifyCallback | None,
    event_callback: EventCallback | None,
) -> None:
    """Report each live-worker park, as the daemon reports one at startup."""
    for state, why in parks:
        assert state.pid is not None  # a park records its worker
        reconcile_mod.report_park(
            reconcile_mod.ParkedWorker(state.task_id, state.pid, state.log_path, why),
            notify_callback,
            event_callback,
        )


def _start_parked_finalize(
    job: adoption_mod.ParkedFinalize,
    tracker: adoption_mod.ParkedWorkerTracker,
    queue_dir: Path,
    settings: Settings,
    clock: Clock,
    in_flight_slots: dict[str, DispatchSlot],
    daemon: bool,
) -> None:
    """Record a parked worker's run on a thread that holds its task's slot,
    as a dispatch thread holds it through its post-dispatch hook."""
    thread = threading.Thread(
        target=tracker.finalize,
        args=(job, queue_dir),
        kwargs={"settings": settings, "clock": clock},
        name=f"record-parked-{job.state.task_id}",
        daemon=daemon,
    )
    thread.start()
    in_flight_slots[job.state.task_id] = DispatchSlot(
        task_id=job.state.task_id,
        account=job.account or adoption_mod.UNATTRIBUTED_ACCOUNT,
        started_at=clock.now(),
        thread=thread,
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

    Cold start: every account is seeded NO_READING, which is not in the
    dispatchable set, so this returns False until a clean reading has
    classified at least one account. A single-account queue reads its
    account earlier in the same tick, so it dispatches on its first tick.

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


def _reap_finished(
    in_flight_slots: dict[str, DispatchSlot],
    *,
    queue_dir: Path | None = None,
    clock: Clock | None = None,
    notify_callback: NotifyCallback | None = None,
    event_callback: EventCallback | None = None,
) -> None:
    """Reap dispatch threads that have exited; refuse to free leaked subprocesses.

    Defence-in-depth post-kill sanity check (Bug 5 of the 2026-06
    zombie-consolidated PR): for every slot whose dispatch thread has
    exited, we look up the subprocess pid in the just-written run record
    and ask :func:`dispatcher.probe_worker` whether it is still that run's
    worker (a pid another process was given since does not hold the slot).
    If the pid is still alive — the
    dispatcher's SIGTERM→SIGKILL escalation failed (typically a kernel
    D-state) — we log loudly, fire a one-shot operator notification,
    and DO NOT delete the slot. Holding the slot prevents the queue
    from re-dispatching to the leaked subprocess's account-slot until
    operator intervention. Re-checks on subsequent ticks remain silent
    until the pid eventually disappears, at which point the slot frees
    normally.

    ``queue_dir`` is required for the leak check; tests that call this
    helper without it (legacy positional call sites, simple finished-
    thread reaps) fall back to the historical behaviour (free the
    slot unconditionally). ``clock`` is required to stamp
    ``subprocess_leak_notified_at`` on first detection — also optional
    so existing call sites stay compiling.
    """
    finished = [tid for tid, slot in in_flight_slots.items() if not slot.thread.is_alive()]
    for tid in finished:
        slot = in_flight_slots[tid]
        with contextlib.suppress(Exception):
            slot.thread.join(timeout=0.1)

        run = _recorded_subprocess_run(queue_dir, tid) if queue_dir is not None else None
        leak_pid = run.pid if run is not None else None
        if (
            leak_pid is not None
            and run is not None
            # No log_path: the run's worker may have been pipe-backed, and a
            # leak is held on the start-time check alone, erring toward held.
            and dispatcher_mod.probe_worker(leak_pid, log_path=None, started_at=run.started_at)
            is dispatcher_mod.WorkerProbe.ALIVE
        ):
            if slot.subprocess_leak_notified_at is None:
                logger.error(
                    "SUBPROCESS_LEAK_DETECTED: task %s dispatch thread exited but "
                    "subprocess pid=%s is still alive; refusing to free the "
                    "in-flight slot until the kernel releases it (operator "
                    "intervention may be required: SIGKILL the pid manually)",
                    tid,
                    leak_pid,
                )
                if notify_callback is not None:
                    notify_callback(
                        "critical",
                        f"subprocess leak: task {tid} dispatch thread exited but "
                        f"pid={leak_pid} is still alive; in-flight slot held until "
                        f"the kernel releases it",
                    )
                if event_callback is not None:
                    event_callback(
                        "subprocess_leak_detected",
                        {"task_id": tid, "pid": leak_pid},
                    )
                if clock is not None:
                    slot.subprocess_leak_notified_at = clock.now()
            # Leave the slot in place; do NOT delete.
            continue

        del in_flight_slots[tid]
        logger.info("reaped finished dispatch thread for task %s", tid)


def _recorded_subprocess_run(queue_dir: Path, task_id: str) -> RunRecord | None:
    """Return the most recent run record for ``task_id`` when it has a pid, or ``None``.

    Reads the task's state YAML and returns ``runs[-1]``, whose ``pid`` is
    the OS pid of the subprocess most recently spawned for this task and
    whose ``started_at`` dates it. Used by the post-tick reap's
    subprocess-leak detection. Returns ``None`` when:

    * the state file is missing or unparseable (defensive — a missing
      state YAML means we have nothing to check)
    * the task is ``deferred`` (ADR-0029): a pre-dispatch hook exit-1
      deferral spawns NO subprocess and, unlike every other outcome,
      does NOT append a RunRecord (ADR-0026). So ``runs[-1]`` is a
      STALE record from an earlier *real* dispatch whose subprocess has
      long exited — and whose OS pid may since be RECYCLED by an
      unrelated process (or owned by another user, which
      :func:`dispatcher._pid_alive` reports alive on ``EPERM``).
      Reading that pid made :func:`_reap_finished` mistake the recycled
      pid for a live leaked subprocess and hold the in-flight slot
      forever, starving low-concurrency accounts (``work`` at
      ``max_concurrency=1`` sat at 0% dispatch for days). The deferring
      dispatch had no subprocess to leak, so there is nothing to guard.
    * the task has no recorded runs (first dispatch hasn't finalized
      yet)
    * the most recent run record predates the ``pid`` field (legacy
      run records carry ``pid=None`` by the schema default)
    * the run was a pre-dispatch hook failure (no subprocess ever
      spawned — that path DOES append a RunRecord, but with
      ``pid=None``, so this returns ``None`` for it too)
    """
    sp = state_path_for(queue_dir, task_id)
    if not sp.exists():
        return None
    try:
        state = load_state(sp)
    except Exception as exc:
        logger.debug(
            "subprocess-leak check: %s state load failed (%s); skipping probe",
            task_id,
            exc,
        )
        return None
    # A deferral parks the task in `deferred` without spawning a worker
    # or appending a run (ADR-0026), so `runs[-1]` here belongs to an
    # earlier dispatch, not the just-finished one. Probing its
    # (possibly recycled) pid would wrongly hold the slot (ADR-0029).
    if state.status == "deferred":
        return None
    if not state.runs or state.runs[-1].pid is None:
        return None
    return state.runs[-1]


def _target_concurrency(queue_dir: Path, settings: Settings) -> int | None:
    """The explicit queue-wide cap on in-flight tasks in force, or ``None``.

    ``[concurrency].initial_concurrency``, when set, until at least one
    task has completed in this queue; otherwise
    ``[concurrency].max_concurrency``. ``None`` when the key that applies
    is left out: then each account's own ``max_concurrency`` is the only
    limit (:func:`account_dispatch.dispatch_headroom`, ADR-0035).

    Throttling is not applied here. Each account's own cap, including
    its SLOWING_DOWN ramp target, is enforced per task by
    :func:`account_dispatch.choose_account`. This used to halve the
    ceiling whenever the top-level state was SLOWING_DOWN, which ignored
    the ramp the supervisor announced and, with several accounts,
    depended on which account had been captured last.
    """
    initial = settings.concurrency.initial_concurrency
    if initial is not None and not _has_any_completed(queue_dir):
        return max(1, initial)
    configured = settings.concurrency.max_concurrency
    return None if configured is None else max(1, configured)


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


def _session_affinity_expired(
    queue_dir: Path,
    task_id: str,
    *,
    now: datetime,
    ttl_seconds: float,
) -> bool:
    """True iff ``task_id`` has a session idle longer than ``ttl_seconds``.

    Idleness is measured from the most recent of ``last_finished_at`` /
    ``last_heartbeat_at`` / ``last_started_at``. Returns ``False`` when
    the task has no state file, the state cannot be parsed, the task has
    no ``session_id`` (nothing to expire), or no timestamp is available.
    A non-positive ``ttl_seconds`` never expires (guards against a
    misconfigured zero disabling affinity for every task).
    """
    if ttl_seconds <= 0:
        return False
    sp = state_path_for(queue_dir, task_id)
    if not sp.exists():
        return False
    try:
        state = load_state(sp)
    except Exception:
        return False
    if state.session_id is None:
        return False
    last = state.last_finished_at or state.last_heartbeat_at or state.last_started_at
    if last is None:
        return False
    return (now - last).total_seconds() > ttl_seconds


def _clear_session_for_fresh_dispatch(queue_dir: Path, task_id: str) -> None:
    """Clear a task's session so its next dispatch starts fresh anywhere.

    The automatic, time-gated form of ``queue restart-fresh``: drops
    ``session_id`` / ``session_account`` / ``resume_attempts`` so the
    dispatcher spawns a new session (no ``--resume``) on whichever
    account ``choose_account`` picks. Best-effort — a load/write failure
    leaves the existing affinity intact (the task simply stays blocked
    this tick rather than dispatching incorrectly).
    """
    sp = state_path_for(queue_dir, task_id)
    try:
        state = load_state(sp)
        new_state = state.model_copy(
            update={
                "session_id": None,
                "session_account": None,
                "resume_attempts": 0,
            }
        )
        write_state_atomic(new_state, sp)
    except Exception:
        pass


def _dispatch_blocked_task_ids(queue_dir: Path, block_file: str | None) -> set[str]:
    """Return task ids an operator has flagged ``block_dispatch: true`` (ADR-0029).

    ``block_file`` is the queue-relative path from
    ``[dispatch].dispatch_block_file``; ``None`` / empty means the
    feature is off, so this returns an empty set. The file is JSONL:
    each line is parsed independently and contributes a task id only
    when it is a JSON object with ``block_dispatch is True`` and a
    non-empty string ``task``. Every other line — an index row, a
    ``target_path`` re-acquisition row, a blank line, or malformed
    JSON — is skipped, so a mixed-content block-list (the
    ``needs_acquisition.jsonl`` convention carries all three kinds) is
    read without acting on rows this selector shouldn't.

    Fail-safe by construction: an unreadable file or an unparseable
    line yields NO blocked id, so the affected task simply dispatches
    and the pre-dispatch hook re-checks the block. A block-list problem
    therefore *under*-blocks (a wasted defer at worst) rather than
    *over*-blocking (stranding a runnable task) — the safe direction.
    """
    if not block_file:
        return set()
    path = queue_dir / block_file
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    except OSError as exc:
        logger.debug("dispatch block-list %s unreadable (%s); treating as empty", path, exc)
        return set()

    blocked: set[str] = set()
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except ValueError:
            # A non-JSON / partially-written line is not a block entry;
            # skip it (debug, not warning — the block file is expected
            # to carry rows this reader deliberately ignores).
            continue
        if isinstance(entry, dict) and entry.get("block_dispatch") is True:
            tid = entry.get("task")
            if isinstance(tid, str) and tid:
                blocked.add(tid)
    return blocked


def _is_parked_with(state: TaskState | None, reason: str) -> bool:
    """True iff ``state`` is already parked as ``deferred`` with exactly ``reason``."""
    return state is not None and state.status == "deferred" and state.deferred_reason == reason


def _park_deferred(
    queue_dir: Path,
    task_id: str,
    state: TaskState | None,
    reason: str,
) -> None:
    """Park a task a selector gate is holding as ``deferred`` + ``reason``.

    Used by both selector gates: the readiness gate (ADR-0030) and the
    effort gate (ADR-0010). Each writes its own reason prefix, see
    :func:`readiness.hold_reason` and :func:`effort_levels.hold_reason`.

    Makes a hold VISIBLE. Without this a gate is silent: the selector
    skips the task on every tick with only a debug log, so ``queue states``
    still shows it ``pending`` and an operator investigating "why has this
    never run?" has nothing to read, and has to park the task by hand to
    leave any trace.

    Deliberately writes NO ``next_eligible_at``. A cooldown would break the
    ADR-0030 promise that a held task dispatches the first tick after its
    element appears: the `deferred` branch in :func:`_skip_category`
    falls straight through when ``next_eligible_at`` is unset, so the gates
    themselves stay the only thing holding the task and re-admission is
    immediate.

    Writes only on TRANSITION (when the persisted status/reason differ, see
    :func:`_is_parked_with`), so a queue holding hundreds of blocked tasks
    costs one write each — not one per task per tick. ``attempts`` /
    ``runs`` are untouched: a hold is not an attempt and must never feed the
    circuit breaker.
    """
    if _is_parked_with(state, reason):
        return  # already parked with this exact reason — no rewrite, no churn
    base = state if state is not None else TaskState(task_id=task_id)
    new_state = base.model_copy(
        update={
            "status": "deferred",
            "deferred_reason": reason,
            "next_eligible_at": None,
        }
    )
    sp = state_path_for(queue_dir, task_id)
    try:
        sp.parent.mkdir(parents=True, exist_ok=True)
        write_state_atomic(new_state, sp)
    except Exception as exc:
        # Best-effort bookkeeping: the task is already being skipped by the
        # caller, so a failed write costs visibility, never correctness.
        logger.warning("could not park task %s as deferred (%s): %s", task_id, reason, exc)


def _unpark(
    queue_dir: Path,
    task_id: str,
    state: TaskState | None,
    owns: Callable[[str | None], bool],
) -> TaskState | None:
    """Un-park a task whose hold (and only one ``owns`` recognises) has cleared.

    Restores ``pending`` and clears the reason so the state stops claiming a
    block that no longer exists. Scoped by ``owns``, a gate's
    ``is_hold_reason``: an operator's manual park, the pre-dispatch hook's
    exit-1 deferral and the other gate's hold carry different reasons and
    are left exactly as they are — self-healing must never quietly undo a
    human's decision to hold a task.

    Returns the state the rest of the selector should see: the un-parked
    one (even when the write fails, since un-parking is bookkeeping and the
    task is admitted either way), or ``state`` itself when there was
    nothing to clear.
    """
    if state is None or state.status != "deferred" or not owns(state.deferred_reason):
        return state
    new_state = state.model_copy(
        update={"status": "pending", "deferred_reason": None, "next_eligible_at": None}
    )
    logger.info("task %s: un-parked, its hold has cleared: %s", task_id, state.deferred_reason)
    try:
        write_state_atomic(new_state, state_path_for(queue_dir, task_id))
    except Exception as exc:
        logger.warning("could not un-park task %s: %s", task_id, exc)
    return new_state


def _effort_hold_reason(task: Task, effort_levels: dict[str, list[str]]) -> str | None:
    """The ``deferred_reason`` to park ``task`` with, or ``None`` if its pair is accepted.

    See ADR-0010. The task schema cannot check ``(model, effort)`` because
    the accepted sets live in the merged settings, so the selector and the
    dispatch backstop check it here, against the settings the supervisor
    is running with.
    """
    try:
        effort_levels_mod.validate_effort(task.model, task.effort, effort_levels)
    except effort_levels_mod.UnknownEffortLevel as exc:
        return effort_levels_mod.hold_reason(exc)
    return None


def _eligible_candidates(
    queue_dir: Path,
    in_flight_slots: Collection[str],
    completed_ids: set[str],
    now: datetime | None = None,
    block_file: str | None = None,
    *,
    effort_levels: dict[str, list[str]],
    skips: Counter[str] | None = None,
    read_only: bool = False,
    on_park: ParkCallback | None = None,
) -> list[Task]:
    # ``now`` gates the re-check cooldown for `deferred` tasks; the
    # production caller (tick_dispatch) always passes ``clock.now()``.
    # When omitted (older unit tests that exercise non-deferred paths),
    # a deferred task simply isn't cooldown-gated.
    #
    # ``effort_levels`` is the merged ``[effort_levels]`` table the effort
    # gate checks each task's (model, effort) against. Required, with no
    # default: a caller that forgot it must fail, not skip the check.
    #
    # ``skips``, when given, is incremented once for every task YAML in
    # ``todo/`` that is NOT returned, under the category that held it (see
    # :func:`_skip_category`). A task whose state is ``completed`` is counted
    # under ``completed`` whichever check skipped it first, so every other
    # category counts unfinished work only. The returned tasks plus the
    # counts added here equal the number of task YAMLs read.
    #
    # ``in_flight_slots`` is read for its task ids only (a slot map's keys).
    # ``read_only`` turns off the gates' bookkeeping (see
    # :func:`_skip_category`): the same tasks are returned and counted, and
    # no state file is written.
    tally: Counter[str] = skips if skips is not None else Counter()
    out: list[Task] = []
    in_flight_ids = set(in_flight_slots)

    # Cache the open-sidecar set once per call rather than scanning the
    # sidecar directory inside every per-task branch. A task is
    # "sidecar-open" if there's at least one request-NNN.json without a
    # matching response-NNN.json.
    open_sidecar_task_ids: set[str] = {tid for tid, _seq, _path in list_open_sidecars(queue_dir)}

    # Operator-maintained dispatch block-list (ADR-0029, Part 2): task
    # ids flagged `block_dispatch: true`. Read once per call. Off (empty)
    # unless the queue set `[dispatch].dispatch_block_file`.
    blocked_ids = _dispatch_blocked_task_ids(queue_dir, block_file)
    if blocked_ids:
        logger.debug("dispatch block-list active: %d task(s) parked", len(blocked_ids))

    for path in list_pending_tasks(queue_dir):
        try:
            task = load_task(path)
        except Exception as exc:
            logger.warning("skipping unparseable task at %s: %s", path, exc)
            tally[_SKIP_UNPARSEABLE_TASK] += 1
            continue
        category = _skip_category(
            task,
            queue_dir=queue_dir,
            in_flight_ids=in_flight_ids,
            completed_ids=completed_ids,
            open_sidecar_task_ids=open_sidecar_task_ids,
            blocked_ids=blocked_ids,
            now=now,
            effort_levels=effort_levels,
            read_only=read_only,
            on_park=on_park,
        )
        if category is None:
            out.append(task)
        else:
            tally[_COMPLETED if task.id in completed_ids else category] += 1
    return out


def _skip_category(
    task: Task,
    *,
    queue_dir: Path,
    in_flight_ids: set[str],
    completed_ids: set[str],
    open_sidecar_task_ids: set[str],
    blocked_ids: set[str],
    now: datetime | None,
    effort_levels: dict[str, list[str]],
    read_only: bool = False,
    on_park: ParkCallback | None = None,
) -> str | None:
    """Decide whether the selector admits ``task`` this tick.

    Returns ``None`` when the task is eligible. Otherwise returns the
    category of what holds it: one of the ``_SKIP_*`` names, the name of a
    status that is never dispatched (``running``, ``possibly_hung``,
    ``completed``, ...), or :data:`_SKIP_READINESS_PREFIX` followed by the
    categories of the unmet requirements. Admission and the reason for a
    refusal come from this one decision, so the summary
    :class:`IdleDispatchLog` prints cannot disagree with what the selector
    did.

    The effort and readiness gates also park a held task as ``deferred`` and
    un-park it once its hold clears (see :func:`_park_deferred`).
    ``read_only`` skips that bookkeeping and its warning, for a caller that
    only wants to know what the selector would do (:func:`preview_selection`);
    the category returned is the same.
    """
    if task.id in blocked_ids:
        # An operator has flagged this task `block_dispatch: true`
        # (e.g. a paper awaiting a supplement). Skip it in the
        # SELECTOR — without spawning a dispatch that the
        # pre-dispatch hook would only exit-1 defer. That avoids
        # burning a dispatch+defer cycle (and, on a 1-slot account,
        # briefly re-occupying the only slot) every cooldown for a
        # task known to be blocked. The hook remains the enforcing
        # backstop for any task that slips through. (ADR-0029)
        return _SKIP_BLOCK_DISPATCH
    if task.id in in_flight_ids:
        return _SKIP_IN_FLIGHT

    # ``state`` stays in scope past this block: the effort and readiness
    # gates below need it to record / clear a hold, and they must apply to
    # a task that has no state file yet (never dispatched) exactly as they
    # do to one resuming from awaiting_sidecar, deferred, or failed.
    state: TaskState | None = None
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
            return _SKIP_UNPARSEABLE_STATE
        if state.status not in _DISPATCHABLE_STATUSES:
            # Special case: awaiting_sidecar tasks become
            # dispatchable AGAIN once every sidecar request has
            # a matching response file. Without this, a task
            # that stopped to ask an operator question stays
            # stuck forever — the operator's response file is
            # written but the orchestrator never re-evaluates.
            if state.status == "awaiting_sidecar" and task.id in open_sidecar_task_ids:
                # A request is still unanswered — keep the task
                # ineligible until the operator responds.
                return _SKIP_UNANSWERED_SIDECAR
            if state.status == "deferred":
                # A deferred task becomes dispatchable again once its
                # ``next_eligible_at`` passes, whoever wrote it: the
                # pre-dispatch hook (exit 1 — e.g. an input awaiting
                # operator re-acquisition or a pending trim) or an
                # operator by hand. Re-dispatch re-runs the hook: if the
                # input is ready it proceeds, otherwise the dispatcher
                # re-parks it with a fresh cooldown. The cooldown is what
                # keeps a still-blocked task from being re-picked every
                # tick.
                if (
                    now is not None
                    and state.next_eligible_at is not None
                    and now < state.next_eligible_at
                ):
                    return _SKIP_DEFERRED_COOLDOWN
                # cooldown elapsed (or unset) → fall through, re-attempt.
            elif state.status != "awaiting_sidecar":
                # Any other non-dispatchable status (running,
                # completed, failed_circuit_breaker, ...) stays
                # skipped, counted under the status's own name.
                return state.status
            # awaiting_sidecar with every request answered, OR a
            # deferred task past its cooldown: fall through to the
            # gates below.

        # A task whose recorded worker still runs is never dispatched
        # beside it, whatever its status says (a state demoted while its
        # worker ran). Parked, its worker holds its account's slot and its
        # run is recorded once it exits (supervisor.adoption.track_parked_workers).
        if (
            state.pid is not None
            and dispatcher_mod.probe_recorded_worker(state, queue_dir)
            is dispatcher_mod.WorkerProbe.ALIVE
        ):
            if not read_only:
                _park_live_worker(queue_dir, state, _SELECTOR_PARK, on_park)
            return _SKIP_LIVE_WORKER

    # Effort gate (ADR-0010): a task whose (model, effort) pair the merged
    # [effort_levels] rejects is never dispatched. It is parked as
    # `deferred` with an `invalid effort: ...` reason and a WARNING, and
    # un-parked the first tick after the task YAML is fixed (or the TOML
    # is, and the supervisor re-reads it on SIGHUP). The task schema
    # cannot check this: the accepted sets live in the settings.
    #
    # Like the readiness gate below, this sits AFTER the per-status
    # branching, so a completed, running or circuit-broken task is never
    # re-parked because [effort_levels] changed under it. It sits BEFORE
    # depends_on so an authoring error surfaces as soon as the task is
    # queued, not only once its dependencies finish.
    effort_reason = _effort_hold_reason(task, effort_levels)
    if effort_reason is not None:
        if not read_only and not _is_parked_with(state, effort_reason):
            # Once per transition, not once per tick.
            logger.warning(
                "parking task %s as deferred: %s. Fix the task's model or effort, "
                "or add the pair to [effort_levels] and send the supervisor SIGHUP",
                task.id,
                effort_reason,
            )
            _park_deferred(queue_dir, task.id, state, effort_reason)
        return _SKIP_INVALID_EFFORT
    if not read_only:
        state = _unpark(queue_dir, task.id, state, effort_levels_mod.is_hold_reason)

    unmet = [d for d in task.depends_on if d not in completed_ids]
    if unmet:
        return _SKIP_DEPENDS_ON

    # Mechanical readiness gates (ADR-0030): a task waiting on a file
    # (or any no-AI precondition) is kept OUT of the candidate set until
    # every element is satisfied — checked here in-process each tick, so
    # it never burns a dispatch+hook+cooldown cycle and unblocks the
    # first tick after the element appears. The open-sidecar set is
    # already computed above; share it so a `sidecar_response` gate is a
    # free set lookup. Empty `requires` (the default) → no-op.
    #
    # This sits AFTER the per-status branching on purpose, so it is the
    # last word for EVERY resume path — pending, failed, deferred past
    # its cooldown, and awaiting_sidecar whose requests have all been
    # answered. `test_orchestrator_sidecar_resume.py` enumerates those
    # statuses against an unsatisfied requirement so a future special
    # case added above cannot quietly route around the gate.
    unmet_reqs = readiness_mod.unmet_details(
        task, queue_dir, open_sidecar_task_ids=open_sidecar_task_ids
    )
    if unmet_reqs:
        descriptions = [u.description for u in unmet_reqs]
        logger.debug(
            "task %s held by %d unmet readiness requirement(s): %s",
            task.id,
            len(descriptions),
            "; ".join(descriptions),
        )
        if not read_only:
            _park_deferred(queue_dir, task.id, state, readiness_mod.hold_reason(descriptions))
        # Sorted and de-duplicated, so tasks missing the same kinds of
        # element share one category whatever order their `requires` lists.
        return _SKIP_READINESS_PREFIX + "+".join(sorted({u.category for u in unmet_reqs}))

    # Satisfied — if THIS gate is what parked the task, un-park it so the
    # state stops advertising a block that has cleared.
    if not read_only:
        _unpark(queue_dir, task.id, state, readiness_mod.is_hold_reason)

    return None


_SELECTOR_PARK = "though its task was dispatchable"
_BACKSTOP_PARK = "when its task was about to be dispatched again"


def _park_live_worker(
    queue_dir: Path, live: TaskState, why: str, on_park: ParkCallback | None
) -> None:
    """Refuse to dispatch ``live``'s task: its worker still runs. Park it
    (:func:`runner.dispatcher.park_live_worker`), log a WARNING, and hand the
    park to ``on_park`` to be reported and counted."""
    parked = dispatcher_mod.park_live_worker(live, why=why)
    try:
        write_state_atomic(parked, state_path_for(queue_dir, live.task_id))
    except Exception as exc:
        logger.error(
            "refusing to dispatch task %s: its worker pid=%s is still running, and its "
            "park could not be written (%s)",
            live.task_id,
            live.pid,
            exc,
        )
        return
    logger.warning(
        "refusing to dispatch task %s: its worker pid=%s is still running (log=%s); "
        "parked possibly_hung (stop_reason=%s) so it is not dispatched a second time",
        live.task_id,
        live.pid,
        live.log_path,
        parked.stop_reason,
    )
    if on_park is not None:
        on_park(parked, why)


@dataclass(frozen=True)
class SelectionPreview:
    """What the dispatch selector would do now: :func:`preview_selection`."""

    eligible: list[Task]
    """The tasks it would admit, in dispatch order."""
    eligible_statuses: dict[str, str]
    """Each admitted task's state status, ``new`` when it has no state file
    yet: an ``awaiting_sidecar`` task here has had every question answered."""
    skips: Counter[str]
    """Every other task YAML in ``todo/``, counted under the category that
    holds it, ``completed`` included (see :func:`_eligible_candidates`)."""
    queue_cap: int | None
    """The queue-wide cap on tasks in flight now (:func:`_target_concurrency`):
    ``[concurrency].initial_concurrency`` while warming up, else
    ``[concurrency].max_concurrency``; ``None`` when neither applies."""
    warming_up: bool
    """Whether ``queue_cap`` is the warm-up cap: ``initial_concurrency`` is
    set and no task has completed yet."""


def preview_selection(
    queue_dir: Path,
    settings: Settings,
    *,
    in_flight_ids: Collection[str],
    now: datetime,
) -> SelectionPreview:
    """Run the dispatch selector once without any of its side effects.

    The same :func:`_eligible_candidates` pass a supervisor tick runs, with
    the queue's settings, its block-list and ``now``, but read-only: no task
    is parked or un-parked and no state file is written, so a status command
    can show what holds each task. ``in_flight_ids`` stands in for the
    supervisor's slot map (``supervisor.json``'s ``in_flight`` records).
    """
    skips: Counter[str] = Counter()
    eligible = _eligible_candidates(
        queue_dir,
        in_flight_ids,
        _completed_task_ids(queue_dir),
        now=now,
        block_file=getattr(getattr(settings, "dispatch", None), "dispatch_block_file", None),
        effort_levels=settings.effort_levels,
        skips=skips,
        read_only=True,
    )
    eligible.sort(key=priority_sort_key)
    statuses: dict[str, str] = {}
    for task in eligible:
        sp = state_path_for(queue_dir, task.id)
        # The selector loaded this state a moment ago, so it parses.
        statuses[task.id] = load_state(sp).status if sp.exists() else "new"
    return SelectionPreview(
        eligible=eligible,
        eligible_statuses=statuses,
        skips=skips,
        queue_cap=_target_concurrency(queue_dir, settings),
        warming_up=(
            settings.concurrency.initial_concurrency is not None
            and not _has_any_completed(queue_dir)
        ),
    )


def _dispatch_one_safely(
    task: Task,
    queue_dir: Path,
    settings: Settings,
    clock: Clock,
    claude_executable: str,
    claude_config_dir: str,
    linux_user: str | None,
    account: str,
    spawn_gate: SpawnGate | None = None,
    allow_live_worker: bool = False,
    on_park: ParkCallback | None = None,
) -> None:
    """Thread entrypoint — load state, plan spawn, call dispatch, log errors.

    ``claude_config_dir`` / ``linux_user`` / ``account`` are resolved
    per-dispatch by the orchestrator via
    :func:`runner.account_dispatch.choose_account` and routed through to
    :func:`runner.dispatcher.dispatch` so the spawned ``claude``
    subprocess hits the right account's credentials (and uid).

    Re-checks the ADR-0030 readiness gate immediately before spawning. This
    is the LAST chokepoint every dispatch path funnels through — the
    orchestrator's selector and :func:`runner.force_dispatch.tick_consume`
    both spawn this exact function — so enforcing here makes "``requires``
    is checked on every dispatch decision" hold structurally rather than by
    each caller remembering to ask. It also closes the selector's inherent
    race: candidates are chosen once per tick, and a requirement can be
    withdrawn (a re-acquisition park deleting a bad trim) between selection
    and spawn.

    The ADR-0010 effort gate is re-checked here for the same reason: the
    selector parks a task whose (model, effort) pair ``[effort_levels]``
    rejects, and this makes that hold for every path that spawns this
    function, whatever the caller checked.

    First of all, a task with a worker that still runs is not dispatched:
    the recorded worker, or one no state records that writes the task's
    stream log (:func:`runner.dispatcher.live_worker_record`). The task is
    parked with that worker on record and a WARNING. Two workers in one
    worktree corrupt its branch, so this holds for every path, whatever
    produced the state. ``allow_live_worker`` (``queue force-dispatch
    --allow-live-worker``) is the operator's override. ``on_park`` receives
    the park, to be reported at the next tick.
    """
    sp = state_path_for(queue_dir, task.id)
    if sp.exists():
        try:
            state = load_state(sp)
        except Exception:
            state = TaskState(task_id=task.id)
    else:
        state = TaskState(task_id=task.id)

    if not allow_live_worker:
        live = dispatcher_mod.live_worker_record(state, queue_dir)
        if live is not None:
            _park_live_worker(queue_dir, live, _BACKSTOP_PARK, on_park)
            return

    effort_reason = _effort_hold_reason(task, settings.effort_levels)
    if effort_reason is not None:
        logger.warning("refusing to dispatch task %s: %s", task.id, effort_reason)
        _park_deferred(queue_dir, task.id, state, effort_reason)
        return

    unmet_reqs = readiness_mod.unmet_requirements(task, queue_dir)
    if unmet_reqs:
        logger.warning(
            "refusing to dispatch task %s: %d unmet readiness requirement(s): %s",
            task.id,
            len(unmet_reqs),
            "; ".join(unmet_reqs),
        )
        _park_deferred(queue_dir, task.id, state, readiness_mod.hold_reason(unmet_reqs))
        return

    try:
        plan = plan_next_spawn(task, state, settings=settings.session)
        dispatcher_mod.dispatch(
            task=task,
            state=state,
            plan=plan,
            queue_dir=queue_dir,
            clock=clock,
            settings_caps=settings.task_caps,
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
            spawn_gate=spawn_gate,
        )
    except Exception:
        logger.exception("dispatch failed for task %s", task.id)
