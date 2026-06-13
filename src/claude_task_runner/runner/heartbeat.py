"""Stream-json silence detection for hung-task surfacing.

Replaces the existing 2h "likely_stale" heuristic with a configurable
fast-path. The dispatcher refreshes an in-memory heartbeat on every
stream-json event it parses and feeds that value to :func:`evaluate`
each iteration; the persisted ``last_heartbeat_at`` state field it
writes from the same value is rate-limited to one write per
``heartbeat_persist_interval_s`` (so a chatty subprocess doesn't thrash
the filesystem). This module's pure functions decide whether silence
has crossed the alert/kill thresholds.

Settings come from ``[task_caps]``:

* ``heartbeat_silence_alert_s`` — after this many seconds of no events,
  flip task to ``possibly_hung`` and notify. Default 5min.
* ``heartbeat_silence_kill_s`` — after this many seconds, SIGTERM the
  subprocess. Default ``0`` (off).

Both checks are pure (datetime-only) so the dispatcher's monitor loop
stays trivially testable with :class:`FakeClock`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from claude_task_runner.config.schema import TaskCapsSettings


class HeartbeatVerdict(StrEnum):
    """Three possible verdicts on a single heartbeat check."""

    HEALTHY = "healthy"
    """Within the alert window — task continues without intervention."""

    SILENT = "silent"
    """Silence exceeded the alert threshold; task should be marked
    ``possibly_hung``. Do not kill yet."""

    KILL = "kill"
    """Silence exceeded the kill threshold; dispatcher should SIGTERM
    the subprocess. Only reachable when ``heartbeat_silence_kill_s > 0``."""


@dataclass(frozen=True)
class HeartbeatStatus:
    """Output of :func:`evaluate`."""

    verdict: HeartbeatVerdict
    silence_s: float
    """Wall-clock seconds since the last heartbeat. ``0.0`` when no
    heartbeat has yet been recorded (treated as the start of the run)."""


def evaluate(
    *,
    settings: TaskCapsSettings,
    last_heartbeat_at: datetime | None,
    started_at: datetime,
    now: datetime,
) -> HeartbeatStatus:
    """Classify the current heartbeat state.

    The "silence" measurement is from whichever is *later* between
    ``last_heartbeat_at`` and ``started_at``. A run that has emitted no
    events at all uses ``started_at`` as the baseline so we don't
    falsely escalate during normal startup latency.

    Raises ``ValueError`` if ``now < started_at`` or if
    ``last_heartbeat_at`` is in the future relative to ``now``.
    """
    if now < started_at:
        raise ValueError("now must be >= started_at")
    if last_heartbeat_at is not None and last_heartbeat_at > now:
        raise ValueError("last_heartbeat_at cannot be in the future")

    baseline = last_heartbeat_at if last_heartbeat_at is not None else started_at
    silence = (now - baseline).total_seconds()

    kill_threshold = settings.heartbeat_silence_kill_s
    if kill_threshold > 0 and silence > kill_threshold:
        return HeartbeatStatus(verdict=HeartbeatVerdict.KILL, silence_s=silence)

    if silence > settings.heartbeat_silence_alert_s:
        return HeartbeatStatus(verdict=HeartbeatVerdict.SILENT, silence_s=silence)

    return HeartbeatStatus(verdict=HeartbeatVerdict.HEALTHY, silence_s=silence)


def silence_window(
    settings: TaskCapsSettings,
) -> tuple[float, float | None]:
    """Return ``(alert_threshold_s, kill_threshold_s | None)``.

    Useful for telemetry / status dashboards that want to render the
    same windows the runner is using.
    """
    kill = settings.heartbeat_silence_kill_s if settings.heartbeat_silence_kill_s > 0 else None
    return settings.heartbeat_silence_alert_s, kill
