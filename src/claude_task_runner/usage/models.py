"""Data classes for parsed usage readings.

A `UsageReading` is the validated result of capturing and parsing
`claude /usage`. It captures both windows (5-hour and 7-day) plus enough
metadata to handle reset-time-parse failures gracefully. See ADR-0008.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ExtraWindow", "UsageReading", "WindowReading"]


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
    depending on the user's plan. We capture them here so cohort
    reasoning can consume them later. The primary windows
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
    """A complete `/usage` snapshot: both windows + capture metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 2

    captured_at: datetime
    """When we ran `claude /usage`. UTC."""

    five_hour: WindowReading
    seven_day: WindowReading

    extra_windows: list[ExtraWindow] = Field(default_factory=list)
    """Per-model weekly buckets the TUI showed in addition to the
    primary ``seven_day`` (all-models) bucket. Empty for plans that
    don't expose them."""

    capture_path: str | None = None
    """Filesystem path of the raw .cap forensics file, if persisted."""
