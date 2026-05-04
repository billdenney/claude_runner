"""Exponential backoff state for the watchdog.

The watchdog runs every minute (cron) or on-demand (systemd
``Restart=on-failure``) and decides whether to restart the supervisor.
If the supervisor crashes immediately after each restart, naive policy
would loop forever burning CPU and log volume. We protect with:

* A **cooldown**: after a restart, refuse another for
  ``[watchdog].restart_cooldown_s`` seconds (default 30s).
* **Crash-loop detection**: if more than
  ``[watchdog].crash_loop_threshold`` restarts happen within an
  exponentially growing window, back off — sleep up to
  ``[watchdog].restart_backoff_max_s`` (default 600s) before the next
  attempt and emit a ``critical`` notification.

State persists to ``~/.claude_task_runner/watchdog_state.json``. The
file is small (a list of timestamps) and atomic-write keeps the
watchdog safe to run concurrently with itself.

This module is **pure logic**: callers feed it the current time + a
loaded :class:`WatchdogState`, get back a :class:`WatchdogDecision`.
The watchdog tick subcommand handles the I/O (read state → decide →
maybe restart → write state).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from claude_task_runner.clock import Clock
from claude_task_runner.config.schema import WatchdogSettings
from claude_task_runner.queue.schema import CURRENT_SCHEMA_VERSION

WATCHDOG_STATE_FILENAME = "watchdog_state.json"


class WatchdogStateError(ValueError):
    """The watchdog state JSON is malformed."""


class WatchdogVerdict(StrEnum):
    """What the watchdog should do this tick."""

    SKIP = "skip"
    """Supervisor is alive; nothing to do."""

    COOLDOWN = "cooldown"
    """Supervisor is dead but we restarted recently — wait."""

    BACKOFF = "backoff"
    """Repeated rapid crashes; refuse to restart and notify."""

    RESTART = "restart"
    """OK to restart now."""


class WatchdogState(BaseModel):
    """Persisted watchdog bookkeeping."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = CURRENT_SCHEMA_VERSION
    recent_restarts: list[datetime] = Field(default_factory=list)
    """Restart timestamps within the analysis window. Older entries
    are pruned on each tick."""

    last_backoff_alerted_at: datetime | None = None
    """Most recent time we emitted a crash-loop alert, so we don't
    spam notifications when stuck in backoff."""


@dataclass(frozen=True)
class WatchdogDecision:
    """Outcome of :func:`decide`. Watchdog acts on ``verdict`` and
    persists the returned ``new_state``.

    Attributes
    ----------
    verdict
        SKIP, COOLDOWN, BACKOFF, or RESTART.
    new_state
        The state to persist after acting (whether or not we restarted).
        For RESTART verdict, this includes the new restart timestamp.
    next_check_at
        Suggested time for the next watchdog tick. ``None`` means
        "default cadence". For COOLDOWN/BACKOFF this gives the cron/
        systemd timer a hint about when waiting is over.
    detail
        Human-readable explanation logged by the watchdog (and
        included in the BACKOFF notification message).
    """

    verdict: WatchdogVerdict
    new_state: WatchdogState
    next_check_at: datetime | None
    detail: str


def watchdog_state_path() -> Path:
    """Resolve ``~/.claude_task_runner/watchdog_state.json``."""
    base = Path.home() / ".claude_task_runner"
    base.mkdir(parents=True, exist_ok=True)
    return base / WATCHDOG_STATE_FILENAME


def load_state(path: Path) -> WatchdogState:
    """Read the watchdog state, returning a fresh empty one if missing."""
    if not path.exists():
        return WatchdogState()
    try:
        with path.open("rb") as fh:
            payload = json.load(fh)
    except OSError as exc:
        raise WatchdogStateError(f"failed to read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise WatchdogStateError(f"{path}: invalid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise WatchdogStateError(f"{path}: top-level JSON must be an object")

    sv = payload.get("schema_version", CURRENT_SCHEMA_VERSION)
    if sv != CURRENT_SCHEMA_VERSION:
        raise WatchdogStateError(
            f"{path}: schema_version={sv} does not match {CURRENT_SCHEMA_VERSION}"
        )
    try:
        return WatchdogState.model_validate(payload)
    except ValidationError as exc:
        raise WatchdogStateError(f"{path}: {exc}") from exc


def write_state_atomic(state: WatchdogState, path: Path) -> None:
    """Atomic JSON write of the watchdog state."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    payload = state.model_dump(mode="json")
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=parent,
        delete=False,
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True, default=str)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def _prune(state: WatchdogState, *, now: datetime, window_s: float) -> WatchdogState:
    """Drop restart timestamps older than ``now - window_s``."""
    cutoff = now - timedelta(seconds=window_s)
    kept = [t for t in state.recent_restarts if t > cutoff]
    if len(kept) == len(state.recent_restarts):
        return state
    return state.model_copy(update={"recent_restarts": kept})


def _backoff_window_s(settings: WatchdogSettings) -> float:
    """How far back to look for recent restarts when crash-counting.

    Ten cooldowns is enough to capture a sustained crash loop while
    aging out one-off blips. Capped at restart_backoff_max_s so the
    window doesn't grow unboundedly with operator misconfiguration.
    """
    return min(
        settings.restart_cooldown_s * 10,
        settings.restart_backoff_max_s,
    )


def decide(
    *,
    state: WatchdogState,
    supervisor_alive: bool,
    settings: WatchdogSettings,
    clock: Clock,
) -> WatchdogDecision:
    """Pure decision: should the watchdog restart now?

    Decision tree:

    1. Supervisor is alive → SKIP (no action).
    2. Supervisor is dead, last restart within ``restart_cooldown_s`` →
       COOLDOWN (wait).
    3. Supervisor is dead, recent restart count exceeds
       ``crash_loop_threshold`` → BACKOFF (refuse + notify).
    4. Otherwise → RESTART.

    Caller is responsible for actually invoking the restart and for
    persisting :attr:`WatchdogDecision.new_state` afterward.
    """
    now = clock.now()
    window_s = _backoff_window_s(settings)
    pruned = _prune(state, now=now, window_s=window_s)

    if supervisor_alive:
        return WatchdogDecision(
            verdict=WatchdogVerdict.SKIP,
            new_state=pruned,
            next_check_at=None,
            detail="supervisor alive",
        )

    # Crash-loop protection takes priority over plain cooldown: rapid
    # repeated restarts is itself the signature of a crash loop, so a
    # "we just restarted, give it a moment" cooldown alone isn't
    # sufficient.
    if len(pruned.recent_restarts) >= settings.crash_loop_threshold:
        # Compute exponential backoff: each crash beyond the threshold
        # doubles the wait, up to ``restart_backoff_max_s``.
        excess = len(pruned.recent_restarts) - settings.crash_loop_threshold + 1
        backoff_s = min(
            settings.restart_cooldown_s * (2**excess),
            settings.restart_backoff_max_s,
        )
        last = pruned.recent_restarts[-1]
        wait_until = last + timedelta(seconds=backoff_s)
        if wait_until > now:
            new_state = pruned
            # Throttle alerts: only re-notify if 10 minutes since last alert.
            should_alert = (
                pruned.last_backoff_alerted_at is None
                or (now - pruned.last_backoff_alerted_at).total_seconds() > 600
            )
            if should_alert:
                new_state = new_state.model_copy(update={"last_backoff_alerted_at": now})
            return WatchdogDecision(
                verdict=WatchdogVerdict.BACKOFF,
                new_state=new_state,
                next_check_at=wait_until,
                detail=(
                    f"crash loop: {len(pruned.recent_restarts)} restarts "
                    f"in window; backing off until {wait_until.isoformat()}"
                ),
            )

    # Below crash-loop threshold: enforce a short cooldown so rapid
    # double-fires (e.g., cron racing the supervisor's startup) don't
    # spawn duplicates.
    if pruned.recent_restarts:
        last = pruned.recent_restarts[-1]
        elapsed = (now - last).total_seconds()
        if elapsed < settings.restart_cooldown_s:
            wait_until = last + timedelta(seconds=settings.restart_cooldown_s)
            return WatchdogDecision(
                verdict=WatchdogVerdict.COOLDOWN,
                new_state=pruned,
                next_check_at=wait_until,
                detail=(
                    f"cooldown: last restart {elapsed:.0f}s ago, "
                    f"need {settings.restart_cooldown_s}s"
                ),
            )

    # OK to restart.
    new_restarts = [*pruned.recent_restarts, now]
    new_state = pruned.model_copy(
        update={
            "recent_restarts": new_restarts,
            "last_backoff_alerted_at": None,
        }
    )
    return WatchdogDecision(
        verdict=WatchdogVerdict.RESTART,
        new_state=new_state,
        next_check_at=None,
        detail=(
            f"restart approved (recent count: {len(new_restarts)} "
            f"of threshold {settings.crash_loop_threshold})"
        ),
    )
