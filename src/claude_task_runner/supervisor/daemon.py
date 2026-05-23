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
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from claude_task_runner.clock import Clock, RealClock
from claude_task_runner.config.loader import ConfigError, load_settings
from claude_task_runner.config.schema import AccountPolicy, Settings
from claude_task_runner.runner import force_dispatch as fd_mod
from claude_task_runner.runner import orchestrator as orch_mod
from claude_task_runner.runner.in_flight import DispatchSlot
from claude_task_runner.supervisor import persistence as persist_mod
from claude_task_runner.supervisor import pidfile as pidfile_mod
from claude_task_runner.supervisor import reconcile as reconcile_mod
from claude_task_runner.supervisor import state_machine as sm_mod
from claude_task_runner.supervisor import throttle_merge as merge_throttle_mod
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

    ``account_policies`` (PR 13) maps account name → resolved
    :class:`AccountPolicy`. Used by :func:`run_one_tick` to layer
    per-account throttle overrides on top of the queue-wide
    ``settings.throttle`` when the poll result is attributed to a
    specific account. Empty dict (the default) reproduces the
    pre-PR-13 single-throttle behaviour.
    """

    settings: Settings
    poll_result: PollResult
    pending_count: int
    in_flight_count: int
    account_policies: dict[str, AccountPolicy] = field(default_factory=dict)


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

    Multi-account attribution + per-account throttle (PR 8 + PR 13):
        When ``ctx.poll_result`` is a :class:`UsageReading` with
        ``.account`` set (produced by
        :class:`MultiAccountUsageSource`), the daemon:

        1. Focuses the snapshot on that account's per-account state
           (mirror to top-level so the state machine sees the right
           prior state).
        2. Merges the per-account throttle policy from
           ``ctx.account_policies[<account>]`` on top of the queue-
           wide ``ctx.settings.throttle`` via
           :func:`throttle_merge.merge_throttle_with_account`. Any
           ``None`` field in the per-account policy inherits queue-
           wide; explicit values override. Passes the merged
           ``ThrottleSettings`` into ``StepInput`` so
           ``_classify_active`` operates with the right bands.
        3. Runs ``step()``.
        4. Copies the resulting top-level state back into
           ``snapshot.accounts[<account>]``.

        Without per-account attribution (single-account / cold
        start), the queue-wide throttle is used as before.
    """
    account_name = _reading_account(ctx.poll_result)
    focused = _focus_on_account(snapshot, account_name)

    # Per-account throttle merge (PR 13). When attributed, pull the
    # account's policy and overlay onto queue-wide throttle. The
    # merge helper is a pure function and a no-op if every per-
    # account override field is None (i.e. inherit everything).
    effective_throttle = ctx.settings.throttle
    if account_name is not None:
        policy = ctx.account_policies.get(account_name)
        if policy is not None:
            effective_throttle = merge_throttle_mod.merge_throttle_with_account(
                ctx.settings.throttle, policy
            )

    inp = sm_mod.StepInput(
        snapshot=focused,
        reading=ctx.poll_result,
        settings_throttle=effective_throttle,
        settings_supervisor=ctx.settings.supervisor,
        settings_usage=ctx.settings.usage,
        pending_count=ctx.pending_count,
        in_flight_count=ctx.in_flight_count,
    )
    new_focused, actions = sm_mod.step(inp, clock)
    new_snapshot = _propagate_to_account(new_focused, account_name, clock)
    return new_snapshot, actions


def _reading_account(poll_result: object) -> str | None:
    """Extract the account name from a ``UsageReading`` or attributed exception.

    Returns ``None`` for legacy single-account flows (reading has no
    account, or the poll yielded an exception with no ``.account``
    attribute) so callers fall back to the un-attributed code path.
    """
    name = getattr(poll_result, "account", None)
    return name if isinstance(name, str) and name else None


def _focus_on_account(
    snapshot: SupervisorSnapshot,
    account_name: str | None,
) -> SupervisorSnapshot:
    """Return a snapshot whose top-level fields mirror ``accounts[name]``.

    No-op when ``account_name`` is None (single-account flow) or the
    name is not in ``snapshot.accounts`` (defensive — covers a race
    where the supervisor's accounts list was reduced mid-tick).

    The state machine reads the top-level fields; mirroring lets it
    operate on the focused account's prior state without changes.
    """
    if account_name is None:
        return snapshot
    acct = snapshot.accounts.get(account_name)
    if acct is None:
        return snapshot
    return snapshot.model_copy(
        update={
            "state": acct.state,
            "since": acct.since,
            "last_5h_util_pct": acct.last_5h_util_pct,
            "last_weekly_util_pct": acct.last_weekly_util_pct,
            "last_5h_reset_at": acct.last_5h_reset_at,
            "last_weekly_reset_at": acct.last_weekly_reset_at,
            "scheduled_wakeup_at": acct.scheduled_wakeup_at,
            "consecutive_clean_polls": acct.consecutive_clean_polls,
            "last_drift_message": acct.last_drift_message,
        }
    )


def _propagate_to_account(
    snapshot: SupervisorSnapshot,
    account_name: str | None,
    clock: Clock,
) -> SupervisorSnapshot:
    """Copy ``snapshot``'s top-level fields back into ``accounts[name]``.

    Inverse of :func:`_focus_on_account`. Also bumps
    ``accounts[name].last_capture_at`` to the current clock so the
    :class:`MultiAccountUsageSource` round-robin picker advances past
    this account on the next tick.

    No-op when ``account_name`` is None — keeps single-account flow
    bit-for-bit identical.
    """
    if account_name is None:
        return snapshot
    acct = snapshot.accounts.get(account_name)
    if acct is None:
        return snapshot
    updated_acct = acct.model_copy(
        update={
            "state": snapshot.state,
            "since": snapshot.since,
            "last_5h_util_pct": snapshot.last_5h_util_pct,
            "last_weekly_util_pct": snapshot.last_weekly_util_pct,
            "last_5h_reset_at": snapshot.last_5h_reset_at,
            "last_weekly_reset_at": snapshot.last_weekly_reset_at,
            "scheduled_wakeup_at": snapshot.scheduled_wakeup_at,
            "consecutive_clean_polls": snapshot.consecutive_clean_polls,
            "last_drift_message": snapshot.last_drift_message,
            "last_capture_at": clock.now(),
        }
    )
    new_accounts = dict(snapshot.accounts)
    new_accounts[account_name] = updated_acct
    return snapshot.model_copy(update={"accounts": new_accounts})


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


def _diff_settings(old: Settings, new: Settings) -> int:
    """Return a coarse count of top-level setting groups whose dumped JSON differs.

    Used only for the SIGHUP reload summary log line; we don't try to
    pinpoint individual keys (the operator can compare the TOML files
    if they want detail). Returns 0 when the merged dicts are equal,
    so the SIGHUP path can emit "0 changes" idempotently.
    """
    old_d = old.model_dump(mode="json")
    new_d = new.model_dump(mode="json")
    if old_d == new_d:
        return 0
    keys = set(old_d) | set(new_d)
    return sum(1 for k in keys if old_d.get(k) != new_d.get(k))


def start_daemon(
    *,
    queue_dir: Path,
    settings: Settings,
    source: UsageSource | None = None,
    source_builder: Callable[[Callable[[], SupervisorSnapshot]], UsageSource] | None = None,
    pending_count_fn: Callable[[], int],
    in_flight_count_fn: Callable[[], int],
    clock: Clock | None = None,
    notify_callback: Callable[[str, str], None] | None = None,
    event_callback: Callable[[str, dict[str, object]], None] | None = None,
    install_signal_handlers: bool = True,
    max_ticks: int | None = None,
    config_path: Path | None = None,
) -> DaemonHandle:
    """Run the supervisor loop in the calling thread.

    Acquires the host-wide lock (single supervisor enforcement),
    persists each tick, sleeps between polls. Returns the
    :class:`DaemonHandle` once the loop exits (e.g., on SIGTERM).

    Parameters
    ----------
    config_path
        Path to ``claude_runner.toml``, recorded so the SIGHUP handler
        can re-read it. When ``None`` (the historical default), SIGHUP
        re-reads only the package defaults — useful in tests but in
        production callers should pass the same path they fed to
        ``load_settings``.
    max_ticks
        Caps the loop at N ticks — used in integration tests to drive
        a finite number of state transitions deterministically.

    Signal handling (when ``install_signal_handlers=True``):

    * ``SIGTERM`` / ``SIGINT`` — request a clean stop. In-flight
      dispatch threads are NOT killed (architectural invariant 2);
      the loop exits but threads finish their current attempt.
    * ``SIGHUP`` — request a hot-reload of ``claude_runner.toml`` on
      the next tick. Newly-added task YAMLs in ``todo/`` are picked
      up automatically because the orchestrator rescans on every tick.
      Malformed TOML is logged and the old config stays active.
      In-flight tasks are unaffected.
    """
    clk = clock if clock is not None else RealClock()

    state_path = persist_mod.supervisor_state_path(queue_dir, settings.supervisor.state_file)
    pid_path = queue_dir / ".claude_task_runner" / "supervisor.pid"
    handle = DaemonHandle(queue_dir=queue_dir, state_path=state_path, pid_path=pid_path)

    stop_flag = {"stop": False}
    reload_flag = {"pending": False}
    drain_flag = {"draining": False}

    def _on_signal(signum: int, _frame: object) -> None:
        logger.info("supervisor caught signal %s; stopping", signum)
        stop_flag["stop"] = True

    def _on_sighup(_signum: int, _frame: object) -> None:
        # Defer the reload to the next tick — running reload work
        # inside the signal handler would be unsafe (re-entrant I/O,
        # GIL surprises). The handler only flips a flag.
        logger.info("supervisor caught SIGHUP; reload pending on next tick")
        reload_flag["pending"] = True

    def _on_sigusr1(_signum: int, _frame: object) -> None:
        # Graceful drain (PR 11). Operator (or systemd's ExecStop)
        # delivers SIGUSR1 when they want to restart without losing
        # in-flight work. The handler flips the drain flag; the main
        # loop stops dispatching new tasks but keeps ticking so the
        # reaper sees in-flight completions. Once in_flight_slots is
        # empty, the loop exits cleanly. Idempotent — a second
        # SIGUSR1 doesn't do anything new.
        if not drain_flag["draining"]:
            logger.info(
                "supervisor caught SIGUSR1; entering drain mode "
                "(no new dispatches; exit when in_flight=0)"
            )
        drain_flag["draining"] = True

    if install_signal_handlers:
        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGHUP, _on_sighup)
        signal.signal(signal.SIGUSR1, _on_sigusr1)

    # Tracks live dispatch slots (thread + account attribution) keyed by
    # task id. Threads are non-daemon so the supervisor process won't
    # terminate until in-flight tasks finish (architectural invariant 2 —
    # in-flight tasks are not killed by supervisor death). The slot's
    # ``account`` field is the source of truth for
    # :class:`InFlightRecord` rebuilds each tick.
    in_flight_slots: dict[str, DispatchSlot] = {}

    with pidfile_mod.acquire_global_lock():
        pidfile_mod.write_pid_file(pid_path)
        try:
            account_names = [a.name for a in settings.accounts]
            snapshot = persist_mod.load(state_path) or persist_mod.initial_snapshot(
                since=clk.now(),
                account_names=account_names,
            )

            # Orphan reconciliation (PR 12): if the previous supervisor
            # exited ungracefully (crash, SIGKILL after TimeoutStopSec, or
            # the bootstrap case of a pre-drain-handler supervisor being
            # forcibly restarted), TaskState YAMLs are stuck at
            # status="running" with their session_id recorded. Demote
            # those orphans to "failed" so the orchestrator picks them up
            # via the normal session-resume path
            # (runner.session.plan_next_spawn → claude --resume <id>).
            # Clear the stale snapshot.in_flight records the dead
            # supervisor wrote — this supervisor owns the in-memory slot
            # map from here on.
            snapshot, orphan_ids = reconcile_mod.reconcile_orphans(queue_dir, snapshot)
            if orphan_ids:
                logger.info(
                    "reconciled %d orphan task(s) for session resume: %s",
                    len(orphan_ids),
                    orphan_ids,
                )
            persist_mod.write_atomic(snapshot, state_path)

            prior_pending = pending_count_fn()

            # If the caller passed a source_builder, instantiate the
            # source now that we have the loaded snapshot. The builder
            # gets a snapshot accessor so a multi-account wrapper can
            # consult the freshest ``accounts[*].last_capture_at``
            # between ticks. ``source`` (the plain pre-built source)
            # remains supported for single-account / test paths.
            if source_builder is not None and source is None:

                def _get_snapshot() -> SupervisorSnapshot:
                    return snapshot

                effective_source: UsageSource = source_builder(_get_snapshot)
            elif source is not None:
                effective_source = source
            else:
                raise ValueError(
                    "start_daemon requires either source or source_builder; got neither"
                )

            ticks = 0
            while not stop_flag["stop"]:
                if max_ticks is not None and ticks >= max_ticks:
                    break

                if reload_flag["pending"]:
                    settings, prior_pending = _apply_sighup_reload(
                        current=settings,
                        config_path=config_path,
                        prior_pending=prior_pending,
                        pending_count_fn=pending_count_fn,
                    )
                    reload_flag["pending"] = False

                # Resolve account policies each tick so per-account
                # runner-account.toml edits are picked up live (matches
                # the live-config posture of `resolve_accounts` already
                # called per-tick from tick_dispatch). Local import to
                # avoid a circular at module top (config.loader imports
                # config.schema which the supervisor package depends on
                # via its own state types).
                from claude_task_runner.config.loader import resolve_accounts

                resolved = resolve_accounts(settings)
                account_policies = {a.name: a.policy for a in resolved}

                ctx = TickContext(
                    settings=settings,
                    poll_result=safe_poll(effective_source),
                    pending_count=pending_count_fn(),
                    in_flight_count=in_flight_count_fn(),
                    account_policies=account_policies,
                )
                snapshot, actions = run_one_tick(snapshot, ctx, clk)
                execute_actions(
                    actions,
                    notify_callback=notify_callback,
                    event_callback=event_callback,
                )
                persist_mod.write_atomic(snapshot, state_path)

                # Drain force-dispatch requests BEFORE the throttle gate so
                # operator overrides land even when the state machine has
                # parked the supervisor in THROTTLED_5H / PAUSED_WEEKLY.
                #
                # Skipped entirely in drain mode: force-dispatch is by
                # definition new work, and the operator's intent during
                # drain is "finish what's running and exit." The next
                # supervisor picks up the request file.
                if not drain_flag["draining"]:
                    try:
                        fd_mod.tick_consume(
                            queue_dir=queue_dir,
                            settings=settings,
                            clock=clk,
                            in_flight_slots=in_flight_slots,
                            claude_executable=settings.claude.executable,
                        )
                    except Exception:
                        logger.exception("force-dispatch tick_consume failed")

                # Reap finished dispatch threads + spawn new ones up to the
                # target concurrency. tick_dispatch returns the snapshot
                # with the refreshed InFlightRecord list; persist it so
                # ``account list`` (and a restarted supervisor) see the
                # current attribution. In drain mode tick_dispatch skips
                # the dispatch step but still reaps + refreshes.
                try:
                    snapshot = orch_mod.tick_dispatch(
                        queue_dir=queue_dir,
                        settings=settings,
                        clock=clk,
                        snapshot=snapshot,
                        in_flight_slots=in_flight_slots,
                        claude_executable=settings.claude.executable,
                        draining=drain_flag["draining"],
                    )
                    persist_mod.write_atomic(snapshot, state_path)
                except Exception:
                    logger.exception("tick_dispatch failed")

                # Drain-complete check: once every dispatch thread has
                # finished, exit cleanly. Done AFTER tick_dispatch so the
                # reap step inside it gets a final pass at picking up
                # threads that completed during this tick.
                if drain_flag["draining"] and not in_flight_slots:
                    logger.info(
                        "drain complete: in_flight=0; exiting cleanly so a "
                        "fresh supervisor can pick up the queue"
                    )
                    break

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


def _apply_sighup_reload(
    *,
    current: Settings,
    config_path: Path | None,
    prior_pending: int,
    pending_count_fn: Callable[[], int],
) -> tuple[Settings, int]:
    """Apply a SIGHUP-triggered reload; return ``(new_settings, new_pending)``.

    On any failure (TOML parse error, schema violation, OS error) we log
    a warning and return ``(current, prior_pending)`` — the old config
    stays active and the operator sees the failure in the journal.
    The orchestrator rescans ``todo/`` on every tick already, so the
    only "rescan" work this function does is recount ``prior_pending``
    so the summary line can report how many new tasks appeared.
    """
    try:
        new_settings = load_settings(config_path)
    except ConfigError as exc:
        logger.warning("SIGHUP received: reload failed (%s); keeping old config", exc)
        return current, prior_pending
    except OSError as exc:
        logger.warning(
            "SIGHUP received: cannot read %s (%s); keeping old config",
            config_path,
            exc,
        )
        return current, prior_pending

    changes = _diff_settings(current, new_settings)
    new_pending = pending_count_fn()
    delta = max(0, new_pending - prior_pending)
    logger.info(
        "SIGHUP received: reloaded config (%d changes); rescanned queue (%d new tasks)",
        changes,
        delta,
    )
    return new_settings, new_pending
