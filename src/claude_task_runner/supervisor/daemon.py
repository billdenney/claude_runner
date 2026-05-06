"""Long-running supervisor daemon: poll → step → execute → persist.

The daemon is the only piece in :mod:`supervisor` that performs I/O.
It composes the pure :func:`state_machine.step` with:

* :class:`UsageSource` for live readings (or ``FakeUsageSource`` in tests).
* :class:`SupervisorSnapshot` persistence (atomic JSON via
  :mod:`supervisor.persistence`).
* PID file + global lock (:mod:`supervisor.pidfile`).
* Action execution: notifications, event emission, wakeup scheduling.

In-flight tasks are NOT killed when the daemon exits — architectural
invariant 2. Restart code in :func:`reattach_in_flight` polls each
recorded PID and finalizes any that died while the supervisor was down.

Driving a single tick is done by :func:`run_one_tick`, which is what
tests exercise. The full daemon loop in :func:`run_forever` adds
sleep / signal handling around it.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from claude_task_runner.clock import Clock, RealClock
from claude_task_runner.config.schema import Settings
from claude_task_runner.runner import orchestrator as orch_mod
from claude_task_runner.supervisor import persistence as persist_mod
from claude_task_runner.supervisor import pidfile as pidfile_mod
from claude_task_runner.supervisor import state_machine as sm_mod
from claude_task_runner.supervisor.actions import (
    Action,
    EmitEvent,
    Notify,
    ScheduleWakeupAt,
)
from claude_task_runner.supervisor.states import SupervisorSnapshot, SupervisorState
from claude_task_runner.usage.drift import (
    UsageCaptureSpawnError,
    UsageCaptureTimeout,
    UsageFormatDrift,
)
from claude_task_runner.usage.models import UsageReading
from claude_task_runner.usage.source import UsageSource

logger = logging.getLogger(__name__)


# Type alias for the union of "what one poll can yield".
PollResult = UsageReading | UsageFormatDrift | UsageCaptureTimeout | UsageCaptureSpawnError


def safe_poll(source: UsageSource) -> PollResult:
    """Call ``source.read()`` with the documented exceptions caught.

    Returns the relevant exception object (not raised) so the daemon
    loop stays simple and the state machine can route on type.
    """
    try:
        return source.read()
    except UsageFormatDrift as exc:
        return exc
    except UsageCaptureTimeout as exc:
        return exc
    except UsageCaptureSpawnError as exc:
        return exc


@dataclass(frozen=True)
class TickContext:
    """Inputs the daemon collects each tick before invoking the state machine.

    Surveys the queue (pending + in-flight counts), polls usage, and
    bundles them with the loaded settings. Tests construct one
    explicitly to drive :func:`run_one_tick` deterministically.
    """

    settings: Settings
    poll_result: PollResult
    pending_count: int
    in_flight_count: int


def run_one_tick(
    snapshot: SupervisorSnapshot,
    ctx: TickContext,
    clock: Clock,
) -> tuple[SupervisorSnapshot, list[Action]]:
    """Drive the state machine for exactly one tick.

    Pure-ish: no I/O performed here either. Action execution is the
    daemon's responsibility (:func:`execute_actions`). Tests can call
    this with hand-built :class:`TickContext` and skip the whole
    side-effects layer.
    """
    inp = sm_mod.StepInput(
        snapshot=snapshot,
        reading=ctx.poll_result,
        settings_throttle=ctx.settings.throttle,
        settings_supervisor=ctx.settings.supervisor,
        settings_usage=ctx.settings.usage,
        pending_count=ctx.pending_count,
        in_flight_count=ctx.in_flight_count,
    )
    return sm_mod.step(inp, clock)


def execute_actions(
    actions: list[Action],
    *,
    notify_callback: Callable[[str, str], None] | None = None,
    event_callback: Callable[[str, dict[str, object]], None] | None = None,
) -> None:
    """Side-effectful execution of state-machine actions.

    Notifications and event emissions are delegated to caller-supplied
    callbacks so the daemon can wire them to whatever notification
    backend (`notify_send`, file banner, webhook) is configured. The
    other action types (``MonitorInFlight``, ``StopDispatch``,
    ``ScheduleWakeupAt``) are advisory: the caller observes the action
    list and reacts.
    """
    for action in actions:
        if isinstance(action, Notify):
            if notify_callback is not None:
                notify_callback(action.level, action.message)
            else:
                logger.info("notify[%s]: %s", action.level, action.message)
        elif isinstance(action, EmitEvent):
            if event_callback is not None:
                event_callback(action.event_type, action.payload)
            else:
                logger.debug("event %s: %s", action.event_type, action.payload)


def next_wakeup(actions: list[Action]) -> datetime | None:
    """Return the latest ``ScheduleWakeupAt.when`` in ``actions``, or None."""
    when: datetime | None = None
    for action in actions:
        if isinstance(action, ScheduleWakeupAt) and (when is None or action.when > when):
            when = action.when
    return when


def sleep_for_next_poll(
    *,
    wakeup_at: datetime | None,
    poll_interval_s: float,
    clock: Clock,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Sleep until the next poll tick.

    If ``wakeup_at`` is set and is closer than ``poll_interval_s``, we
    sleep until then. Otherwise we sleep ``poll_interval_s``. Skewing
    later than the wakeup is fine — the next clean poll will reclassify.
    """
    now = clock.now()
    delay = float(poll_interval_s)
    if wakeup_at is not None:
        until = (wakeup_at - now).total_seconds()
        if until > 0:
            delay = min(delay, until)
    if delay > 0:
        sleep_fn(delay)


@dataclass
class DaemonHandle:
    """Returned by :func:`start_daemon` so callers can inspect / stop it.

    Currently a thin wrapper; here so the public API stays stable when
    we add observability fields later (last tick time, action history
    ring buffer, etc.).
    """

    queue_dir: Path
    state_path: Path
    pid_path: Path


def start_daemon(
    *,
    queue_dir: Path,
    settings: Settings,
    source: UsageSource,
    pending_count_fn: Callable[[], int],
    in_flight_count_fn: Callable[[], int],
    clock: Clock | None = None,
    notify_callback: Callable[[str, str], None] | None = None,
    event_callback: Callable[[str, dict[str, object]], None] | None = None,
    install_signal_handlers: bool = True,
    max_ticks: int | None = None,
) -> DaemonHandle:
    """Run the supervisor loop in the calling thread.

    Acquires the host-wide lock (single supervisor enforcement),
    persists each tick, sleeps between polls. Returns the
    :class:`DaemonHandle` once the loop exits (e.g., on SIGTERM).

    ``max_ticks`` caps the loop at N ticks — used in integration tests
    to drive a finite number of state transitions deterministically.
    """
    clk = clock if clock is not None else RealClock()

    state_path = persist_mod.supervisor_state_path(queue_dir, settings.supervisor.state_file)
    pid_path = queue_dir / ".claude_task_runner" / "supervisor.pid"
    handle = DaemonHandle(queue_dir=queue_dir, state_path=state_path, pid_path=pid_path)

    stop_flag = {"stop": False}

    def _on_signal(signum: int, _frame: object) -> None:
        logger.info("supervisor caught signal %s; stopping", signum)
        stop_flag["stop"] = True

    if install_signal_handlers:
        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)

    # Tracks live dispatch threads keyed by task id. Threads are non-daemon
    # so the supervisor process won't terminate until in-flight tasks finish
    # (architectural invariant 2 — in-flight tasks are not killed by
    # supervisor death).
    in_flight_threads: dict[str, threading.Thread] = {}

    with pidfile_mod.acquire_global_lock():
        pidfile_mod.write_pid_file(pid_path)
        try:
            snapshot = persist_mod.load(state_path) or persist_mod.initial_snapshot(since=clk.now())

            ticks = 0
            while not stop_flag["stop"]:
                if max_ticks is not None and ticks >= max_ticks:
                    break

                ctx = TickContext(
                    settings=settings,
                    poll_result=safe_poll(source),
                    pending_count=pending_count_fn(),
                    in_flight_count=in_flight_count_fn(),
                )
                snapshot, actions = run_one_tick(snapshot, ctx, clk)
                execute_actions(
                    actions,
                    notify_callback=notify_callback,
                    event_callback=event_callback,
                )
                persist_mod.write_atomic(snapshot, state_path)

                # Reap finished dispatch threads + spawn new ones up to the
                # target concurrency. The orchestrator is a thin bridge; the
                # supervisor's pure step() never sees thread state.
                try:
                    orch_mod.tick_dispatch(
                        queue_dir=queue_dir,
                        settings=settings,
                        clock=clk,
                        snapshot=snapshot,
                        in_flight_threads=in_flight_threads,
                        claude_executable=settings.claude.executable,
                    )
                except Exception:
                    logger.exception("tick_dispatch failed")

                if snapshot.state is SupervisorState.STOPPED:
                    logger.info("supervisor in STOPPED state; exiting loop")
                    break

                wakeup = next_wakeup(actions)
                sleep_for_next_poll(
                    wakeup_at=wakeup,
                    poll_interval_s=settings.usage.poll_interval_s,
                    clock=clk,
                )
                ticks += 1
        finally:
            pidfile_mod.clear_pid_file(pid_path)

    return handle
