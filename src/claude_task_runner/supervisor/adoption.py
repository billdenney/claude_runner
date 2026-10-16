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

The reaper and this pass grade a worker by one classifier, the agent
heartbeat and the signs of life (the agent heartbeat alone does not do:
the dispatcher does not advance it during a long tool call, see
:mod:`runner.stream`). The reaper also keeps running a worker whose
worktree had a file written recently (it records that time as the
heartbeat) and one it cannot grade; this pass adopts such a worker only
if that recorded heartbeat is within the alert window, and
``reconcile_orphans`` parks the rest whose worker runs.

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
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
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
from claude_task_runner.runner import caps as caps_mod
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
    log_path: str | None
    """``None`` for a parked pipe-backed worker, which left no log."""
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

        if not _is_adoptable(state, queue_dir, now=now, settings=settings):
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


def _is_adoptable(state: TaskState, queue_dir: Path, *, now: datetime, settings: Settings) -> bool:
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
    if dispatcher_mod.probe_recorded_worker(state, queue_dir) is not WorkerProbe.ALIVE:
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

    A task parked by :func:`runner.dispatcher.park_live_worker` whose
    worker has exited since is recorded too, whatever its log holds (a
    crash without a ``result`` event), since no later pass handles a
    parked task; :class:`ParkedWorkerTracker` does the same every tick.

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

        if not _has_exited(state, queue_dir):
            continue
        try:
            result = _record_exited(
                queue_dir,
                state,
                settings=settings,
                clock=clock,
                account=prior_accounts.get(state.task_id) or state.session_host_account(),
            )
        except _RecordFailed as exc:
            # One task must not keep the supervisor from starting.
            logger.warning(
                "exited-worker finalize: task %s's worker pid=%s exited, but its run "
                "was not recorded: %s; leaving its state as it is",
                state.task_id,
                state.pid,
                exc,
                exc_info=exc.__cause__,
            )
            continue
        if result is not None:
            results.append(result)

    return results


class _RecordFailed(Exception):
    """Recording an exited worker's run failed; the message says why."""


def _record_exited(
    queue_dir: Path,
    state: TaskState,
    *,
    settings: Settings,
    clock: Clock,
    account: str | None,
    cap_violation: caps_mod.CapViolation | None = None,
) -> ExitedWorkerResult | None:
    """Record the run of ``state``'s worker, which has exited, from its log.

    A ``running`` task whose log has no ``result`` event is left alone (the
    worker crashed; the silent-orphan reaper and ``reconcile_orphans``
    handle it). A task parked by :func:`runner.dispatcher.park_live_worker`
    has no later pass, so its run is recorded whatever the log holds: a
    crash without a ``result`` event, or with no log at all.
    ``cap_violation`` marks a worker stopped at its duration cap. ``None``
    when nothing was recorded; :class:`_RecordFailed` when the Task YAML
    cannot be loaded or the finalize raises.
    """
    assert state.pid is not None  # narrowed by the callers
    try:
        task = load_task(task_path_for(queue_dir, state.task_id))
    except Exception as exc:
        # The finalize needs the Task for the ADR-0020 output gate.
        raise _RecordFailed(f"its Task YAML could not be loaded ({exc})") from exc

    try:
        outcome = dispatcher_mod.finalize_exited_worker(
            task=task,
            state=state,
            queue_dir=queue_dir,
            clock=clock,
            settings_dispatch=settings.dispatch,
            settings_hooks=settings.hooks,
            settings_failure_classifier=settings.failure_classifier,
            account=account,
            require_result=not dispatcher_mod.is_parked_live_worker(state),
            cap_violation=cap_violation,
        )
    except Exception as exc:
        raise _RecordFailed(f"its finalize raised {exc!r}") from exc
    if outcome is None:
        logger.info(
            "task %s: worker pid=%s exited with no result event in %s; "
            "leaving it to the silent-orphan reaper and reconcile_orphans",
            state.task_id,
            state.pid,
            state.log_path,
        )
        return None
    if outcome.new_state.runs[-1:] != [outcome.run_record]:
        # The recheck guard found the task already off "running" and
        # stood down, so the new state is the other writer's, not ours.
        return None

    logger.info(
        "recorded exited worker for task %s from its log (pid=%s, log=%s): "
        "status=%s stop_reason=%s",
        state.task_id,
        state.pid,
        state.log_path,
        outcome.new_state.status,
        outcome.new_state.stop_reason,
    )
    return ExitedWorkerResult(
        task_id=state.task_id,
        pid=state.pid,
        log_path=state.log_path,
        status=outcome.new_state.status,
        stop_reason=outcome.new_state.stop_reason,
    )


def _has_exited(state: TaskState, queue_dir: Path) -> bool:
    """True iff ``state``'s recorded worker is gone and its run is unrecorded.

    That is a file-backed ``running`` task, or a task parked by
    :func:`runner.dispatcher.park_live_worker` (whose ``pid`` and
    ``log_path`` are kept for this), whose worker no longer runs: no
    process has the pid, or another process does
    (:func:`runner.dispatcher.probe_recorded_worker`). Needs a recorded
    ``pid`` to probe. Without one there is no telling whether the worker
    still runs, so the task stays with the later passes.
    """
    if state.pid is None:
        return False
    if state.status == "running":
        if state.log_path is None or not Path(state.log_path).exists():
            return False
    elif not dispatcher_mod.is_parked_live_worker(state):
        return False
    return dispatcher_mod.probe_recorded_worker(state, queue_dir) is not WorkerProbe.ALIVE


@dataclass(frozen=True)
class ParkedFinalize:
    """A parked worker whose run is to be recorded, off the tick thread
    (:meth:`ParkedWorkerTracker.finalize`): it exited, or it was stopped at
    its duration cap."""

    state: TaskState
    account: str | None
    """The account the run is recorded under, or ``None`` when no record
    says."""
    cap_violation: caps_mod.CapViolation | None


@dataclass(frozen=True)
class ParkedWorkers:
    """What :meth:`ParkedWorkerTracker.tick` found."""

    in_flight: list[InFlightRecord]
    """One record per parked worker still running, naming the account it
    runs on (:data:`UNATTRIBUTED_ACCOUNT` when no record says), sorted by
    task id."""
    to_finalize: list[ParkedFinalize]
    """Parked workers that have exited or were stopped at their duration
    cap, whose runs are to be recorded."""
    finished: list[ExitedWorkerResult]
    """Runs recorded off the tick thread since the previous tick."""


@dataclass
class _Failure:
    """A parked task whose run could not be recorded, and when to retry."""

    error: str
    retry_at: datetime
    delay_s: float
    cap_violation: caps_mod.CapViolation | None = None
    """The duration cap the worker was stopped at, kept so the retry
    records the run as killed by it, not as a crash."""


_PARKED_MARKER = dispatcher_mod.LIVE_WORKER_STOP_REASON.encode()


class ParkedWorkerTracker:
    """The per-tick pass over workers parked by
    :func:`runner.dispatcher.park_live_worker`, kept for a supervisor run.

    A parked worker has no monitor thread, so no slot in
    ``in_flight_slots``. It still runs on an account, so while
    :func:`runner.dispatcher.probe_recorded_worker` says it runs, :meth:`tick`
    returns an :class:`InFlightRecord` for it, which the orchestrator adds to
    the slots' records for ``dispatch_headroom``, ``choose_account`` and the
    persisted ``in_flight`` list. The account comes from the sources
    adoption uses (:meth:`record_for`).

    A parked worker is held to its task's duration cap
    (``runner.caps.effective_duration_cap_s``) like a monitored one: past it,
    :meth:`tick` terminates its process group through
    :func:`supervisor.reconcile_silent.terminate_worker` and records the run
    as killed by the cap. Once a worker has exited, or been stopped, its run
    is recorded from its log, as at startup, by :meth:`finalize`, which the
    orchestrator runs on a thread holding the task's slot (the
    post-dispatch hook can take minutes); its record goes with it. A run
    that cannot be recorded (the Task YAML missing, the finalize raising)
    is logged once per distinct error and retried after a growing delay
    (:data:`RETRY_FIRST_S` doubling to :data:`RETRY_MAX_S`).

    Tasks in ``owned_ids`` (with a thread in this supervisor: a task a
    reaper parked while its dispatch thread still waits on the worker, or a
    run being recorded) are left to that thread. Only state files whose text
    names the park's stop reason are parsed, so a tick on a queue of
    thousands of tasks reads each file but parses few.

    It also queues parks made outside it (:meth:`note_park`, from the
    dispatch guards' threads) for the orchestrator to report.
    """

    RETRY_FIRST_S = 60.0
    RETRY_MAX_S = 3600.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._finished: list[ExitedWorkerResult] = []
        self._failures: dict[str, _Failure] = {}
        self._parks: list[tuple[TaskState, str]] = []

    def tick(
        self,
        queue_dir: Path,
        *,
        settings: Settings,
        clock: Clock,
        prior_in_flight: Sequence[InFlightRecord],
        owned_ids: Collection[str],
    ) -> ParkedWorkers:
        """This tick's parked workers: see the class docstring."""
        with self._lock:
            finished, self._finished = self._finished, []
        paths = [
            p for p in list_state_files(queue_dir) if p.stem not in owned_ids and _names_park(p)
        ]
        if not paths:
            return ParkedWorkers(in_flight=[], to_finalize=[], finished=finished)

        now = clock.now()
        records: list[InFlightRecord] = []
        jobs: list[ParkedFinalize] = []
        for state_path in paths:
            try:
                state = load_state(state_path)
            except Exception as exc:
                logger.debug("parked-worker pass: skipping %s: %s", state_path, exc)
                continue
            if not dispatcher_mod.is_parked_live_worker(state):
                continue
            assert state.pid is not None  # narrowed by is_parked_live_worker
            record = self.record_for(
                state, settings=settings, prior_in_flight=prior_in_flight, now=now
            )
            account = record.account if record.account != UNATTRIBUTED_ACCOUNT else None
            if dispatcher_mod.probe_recorded_worker(state, queue_dir) is not WorkerProbe.ALIVE:
                if not self._waiting(state.task_id, now):
                    jobs.append(ParkedFinalize(state, account, self._stopped_at_cap(state)))
                continue
            violation = self._past_cap(queue_dir, state, settings, now)
            if violation is None or self._waiting(state.task_id, now):
                records.append(record)
                continue
            logger.warning(
                "task %s: parked worker pid=%s has run %.0fs, past its duration cap of "
                "%.0fs; terminating its process group",
                state.task_id,
                state.pid,
                violation.observed,
                violation.cap,
            )
            if not reconcile_silent.terminate_worker(state.pid):
                with self._lock:
                    self._fail(
                        state.task_id,
                        f"worker pid={state.pid} survived its duration-cap terminate",
                        now,
                        cap_violation=violation,
                    )
                records.append(record)
                continue
            jobs.append(ParkedFinalize(state, account, violation))
        records.sort(key=lambda r: r.task_id)
        return ParkedWorkers(in_flight=records, to_finalize=jobs, finished=finished)

    def record_for(
        self,
        state: TaskState,
        *,
        settings: Settings,
        prior_in_flight: Sequence[InFlightRecord],
        now: datetime,
    ) -> InFlightRecord:
        """The in-flight record of ``state``'s parked worker.

        The account is the previous tick's ``prior_in_flight`` record for the
        task (the previous supervisor's, after a restart:
        ``reconcile_orphans`` keeps it), then the state's session host
        account, then the only account of a single-account queue; with none,
        :data:`UNATTRIBUTED_ACCOUNT`, which counts against an explicit
        ``[concurrency].max_concurrency`` only.
        """
        sole = settings.accounts[0].name if len(settings.accounts) == 1 else None
        account = (
            _prior_accounts(settings, prior_in_flight).get(state.task_id)
            or state.session_host_account()
            or sole
            or UNATTRIBUTED_ACCOUNT
        )
        started = state.last_started_at if state.last_started_at is not None else now
        return InFlightRecord(task_id=state.task_id, account=account, started_at=started)

    def finalize(
        self, job: ParkedFinalize, queue_dir: Path, *, settings: Settings, clock: Clock
    ) -> None:
        """Record ``job``'s run (thread entry point); report it at the next tick."""
        try:
            result = _record_exited(
                queue_dir,
                job.state,
                settings=settings,
                clock=clock,
                account=job.account,
                cap_violation=job.cap_violation,
            )
        except _RecordFailed as exc:
            with self._lock:
                self._fail(
                    job.state.task_id,
                    str(exc),
                    clock.now(),
                    cause=exc.__cause__,
                    cap_violation=job.cap_violation,
                )
            return
        except Exception as exc:
            with self._lock:
                self._fail(
                    job.state.task_id,
                    f"recording raised {exc!r}",
                    clock.now(),
                    cause=exc,
                    cap_violation=job.cap_violation,
                )
            return
        with self._lock:
            self._failures.pop(job.state.task_id, None)
            if result is not None:
                self._finished.append(result)

    def note_park(self, state: TaskState, why: str) -> None:
        """Queue a park made outside this pass, for the orchestrator to report."""
        with self._lock:
            self._parks.append((state, why))

    def drain_parks(self) -> list[tuple[TaskState, str]]:
        """The parks noted since the last call, each once."""
        with self._lock:
            parks, self._parks = self._parks, []
        return parks

    def _waiting(self, task_id: str, now: datetime) -> bool:
        with self._lock:
            failure = self._failures.get(task_id)
        return failure is not None and now < failure.retry_at

    def _stopped_at_cap(self, state: TaskState) -> caps_mod.CapViolation | None:
        """The duration cap a failed attempt to record ``state``'s run had
        stopped its worker at (:attr:`_Failure.cap_violation`), or ``None``."""
        with self._lock:
            failure = self._failures.get(state.task_id)
        return failure.cap_violation if failure is not None else None

    def _fail(
        self,
        task_id: str,
        error: str,
        now: datetime,
        cause: BaseException | None = None,
        cap_violation: caps_mod.CapViolation | None = None,
    ) -> None:
        """Back off ``task_id``; log the error when it is new for the task.
        A ``cap_violation`` (the worker was stopped at its duration cap) is
        kept for the retry, as is one an earlier failure kept. The caller
        holds the lock."""
        previous = self._failures.get(task_id)
        if cap_violation is None and previous is not None:
            cap_violation = previous.cap_violation
        if previous is not None and previous.error == error:
            delay = min(previous.delay_s * 2, self.RETRY_MAX_S)
        else:
            delay = self.RETRY_FIRST_S
            logger.warning(
                "parked task %s: its run could not be recorded: %s; retrying in %.0fs, "
                "and not logging this error again for it",
                task_id,
                error,
                delay,
                exc_info=cause,
            )
        self._failures[task_id] = _Failure(
            error, now + timedelta(seconds=delay), delay, cap_violation
        )

    @staticmethod
    def _past_cap(
        queue_dir: Path, state: TaskState, settings: Settings, now: datetime
    ) -> caps_mod.CapViolation | None:
        """The duration-cap violation of ``state``'s worker, or ``None``.
        ``None`` too when the Task YAML or the start time is unknown."""
        if state.last_started_at is None or now < state.last_started_at:
            return None
        try:
            task = load_task(task_path_for(queue_dir, state.task_id))
        except Exception:
            return None
        violation = caps_mod.evaluate_caps(
            settings=settings.task_caps,
            task=task,
            cumulative_tokens=0,
            started_at=state.last_started_at,
            now=now,
        )
        return violation if violation is not None and violation.which == "duration" else None


def _names_park(state_path: Path) -> bool:
    """Whether the state file's bytes contain the park's stop reason."""
    try:
        return _PARKED_MARKER in state_path.read_bytes()
    except OSError:
        return False


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
    "ParkedFinalize",
    "ParkedWorkerTracker",
    "ParkedWorkers",
    "adopt_running_workers",
    "finalize_exited_workers",
]
