"""Data classes for parsed usage readings.

A `UsageReading` is the validated result of capturing and parsing
`claude /usage`, or of the API source's rate-limit headers. It captures
the 7-day window, the 5-hour window when one applies to the account, and
enough metadata to handle reset-time-parse failures gracefully. See
ADR-0008 and ADR-0038.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["NOT_REPORTED", "ExtraWindow", "UsageReading", "WindowReading", "utilization_text"]

NOT_REPORTED = "not reported"
"""How a window the reading did not report is shown: never as 0%."""


def utilization_text(pct: int | None) -> str:
    """``"42%"``, or :data:`NOT_REPORTED` for a window the reading lacks.

    ``None`` is a 5-hour utilization from a reading that reported no
    5-hour window (:attr:`UsageReading.five_hour`).
    """
    return NOT_REPORTED if pct is None else f"{pct}%"


class WindowReading(BaseModel):
    """One window's slice of a `/usage` reading."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    utilization_pct: Annotated[int, Field(ge=0, le=100)]
    """Integer percent in [0, 100] as reported by Claude's TUI."""

    resets_at_raw: str
    """The raw 'Resets ...' text — preserved verbatim from the TUI."""

    resets_at: datetime | None
    """Parsed reset datetime in UTC, or None if the raw string couldn't
    be parsed. A None here is NOT drift — it's a non-fatal degrade. The
    supervisor uses the last-known reset + window length as fallback."""


class ExtraWindow(BaseModel):
    """An additional model-specific weekly bucket reported by the TUI.

    The ``/usage`` panel shows a primary ``Current week (all models)``
    section plus optional per-model sections (e.g. ``Sonnet only``)
    depending on the user's plan. We capture them here, and the
    ``usage`` command prints them. The primary windows
    :class:`UsageReading.seven_day` always remains the all-models view.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    """The text inside the parentheses of the section header, e.g.
    ``"Sonnet only"``. Verbatim from the TUI."""

    utilization_pct: Annotated[int, Field(ge=0, le=100)]
    resets_at_raw: str
    resets_at: datetime | None


class UsageReading(BaseModel):
    """A complete usage snapshot: the windows that apply + capture metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 2

    captured_at: datetime
    """When we ran `claude /usage`. UTC."""

    five_hour: WindowReading | None
    """The 5-hour window, or ``None`` when the source reported none.

    ``None`` means only that no 5-hour window was reported: the API omits
    the 5h headers when no 5-hour window applies to the account
    (ADR-0038). It is never a stand-in for 0% or for a reading that
    failed; a failed reading raises instead. The TTY parser gives ``None``
    only for a complete ``/usage`` panel that has a weekly section, no
    5-hour section and nothing that could be one (ADR-0040). With ``None``
    the 5-hour dispatch thresholds do not apply."""

    seven_day: WindowReading

    extra_windows: list[ExtraWindow] = Field(default_factory=list)
    """Per-model weekly buckets the TUI showed in addition to the
    primary ``seven_day`` (all-models) bucket. Empty for plans that
    don't expose them."""

    capture_path: str | None = None
    """Filesystem path of the raw .cap forensics file, if persisted."""

    account: str | None = None
    """Which configured account this reading belongs to. ``None`` for
    legacy single-account flows (the daemon applies the reading to
    the top-level snapshot fields). Multi-account sources set this so
    the daemon knows which ``AccountState`` to update.

    Added in PR 8 (per-account ``/usage`` capture). Backward compatible
    with PR 1-7 readings which omit the field."""

    @property
    def five_hour_util_pct(self) -> int | None:
        """``five_hour.utilization_pct``, or ``None`` with no 5-hour window."""
        return self.five_hour.utilization_pct if self.five_hour is not None else None
