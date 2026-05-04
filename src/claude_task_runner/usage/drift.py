"""Drift detection for usage parsing.

Two kinds of drift:

* **Format drift** — the parser couldn't extract two valid blocks from the
  raw bytes. Likely Anthropic changed the TUI layout. Raised by the parser
  itself; the supervisor halts dispatch on this.
* **Monotonicity drift** — utilization decreased without an observed reset.
  This signals either a parser misread (the percentages got swapped) or
  a server-side anomaly. Surfaced as a separate exception so the supervisor
  can react differently.

Tests should be authored in `tests/unit/test_drift.py` covering both.
"""

from __future__ import annotations

from claude_task_runner.usage.models import UsageReading


class UsageFormatDrift(ValueError):
    """The parser could not produce a valid `UsageReading`.

    The exception carries the raw bytes that triggered it so a forensics
    trail is always available, even if persistence to a `.cap` file failed.
    """

    def __init__(self, message: str, raw: bytes | None = None) -> None:
        super().__init__(message)
        self.raw = raw


class UsageMonotonicityDrift(ValueError):
    """A reading's utilization went DOWN without an observed reset boundary."""


class UsageCaptureTimeout(RuntimeError):
    """`claude /usage` did not render both Resets lines within the timeout.

    This is NOT format drift — the TUI structure may be unchanged but the
    OAuth API was slow, or `claude` was unresponsive.
    """


class UsageCaptureSpawnError(RuntimeError):
    """`claude` could not be spawned (binary missing, permission denied, etc.)."""


def validate_monotonicity(
    previous: UsageReading,
    current: UsageReading,
    *,
    suspicious_delta_pct: int,
) -> None:
    """Compare two consecutive readings for monotonicity invariants.

    Within a window, server-reported utilization is monotonically
    non-decreasing — it can only go down across a reset boundary. The
    caller is responsible for detecting reset boundaries; this function
    only checks the invariant.

    Raises ``UsageMonotonicityDrift`` on a regression that doesn't look
    like a reset. A reset is heuristically "current << previous AND
    current.captured_at is well past previous.resets_at" — but the caller
    should already have detected that and skipped this check.

    Also flags suspicious *increases*: a single-poll jump greater than
    ``suspicious_delta_pct`` percentage points. Not necessarily drift, but
    worth a log warning.
    """
    if current.captured_at < previous.captured_at:
        raise UsageMonotonicityDrift(
            f"current capture ({current.captured_at}) is older than "
            f"previous ({previous.captured_at})"
        )

    five_h_drop = previous.five_hour.utilization_pct - current.five_hour.utilization_pct
    if five_h_drop > 0:
        raise UsageMonotonicityDrift(
            f"5h utilization decreased by {five_h_drop} points without an "
            f"observed reset (was {previous.five_hour.utilization_pct}%, "
            f"now {current.five_hour.utilization_pct}%)"
        )

    weekly_drop = previous.seven_day.utilization_pct - current.seven_day.utilization_pct
    if weekly_drop > 0:
        raise UsageMonotonicityDrift(
            f"weekly utilization decreased by {weekly_drop} points without an "
            f"observed reset (was {previous.seven_day.utilization_pct}%, "
            f"now {current.seven_day.utilization_pct}%)"
        )

    five_h_jump = current.five_hour.utilization_pct - previous.five_hour.utilization_pct
    weekly_jump = current.seven_day.utilization_pct - previous.seven_day.utilization_pct
    if five_h_jump > suspicious_delta_pct or weekly_jump > suspicious_delta_pct:
        # Not raised — caller decides how to surface this. Returned via the
        # `_suspicious_jumps` attribute on the function for tests to assert.
        validate_monotonicity._last_suspicious_jump = (  # type: ignore[attr-defined]
            f"5h+{five_h_jump} weekly+{weekly_jump}"
        )
