"""Parse human-readable duration strings (e.g. ``"40h"``, ``"1d 16h"``).

Used by `[dispatch_pct.week].eow_time_switch` (ADR-0022) so an operator
writes ``"40h"`` instead of ``144000`` seconds. Pure function, no I/O.

Grammar (informal)::

    duration  = token+ (whitespace-separated; whitespace optional)
    token     = digit+ unit
    unit      = "d" | "h" | "m" | "s"

Each unit may appear at most once. Bare numbers and fractional values
are rejected — units are required and integral.
"""

from __future__ import annotations

import re
from typing import Final

_TOKEN_RE: Final = re.compile(r"(\d+)\s*([dhms])")
_FULL_RE: Final = re.compile(r"^\s*(?:\d+\s*[dhms]\s*)+$")
_UNIT_SECONDS: Final[dict[str, int]] = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
}


class DurationParseError(ValueError):
    """Raised when a duration string can't be parsed."""


def parse_duration(value: str) -> float:
    """Parse ``value`` into a float number of seconds.

    Accepted forms (whitespace between tokens is optional but recommended)::

        "40h"           →   144_000.0
        "1d 16h"        →   144_000.0
        "30m"           →     1_800.0
        "30s"           →        30.0
        "1d 2h 3m 4s"   →    93_784.0

    Rejected (``DurationParseError``):

    * Empty string / non-string input.
    * Bare numbers without a unit (``"40"``).
    * Unknown units (``"1w"``, ``"1x"``).
    * Fractional values (``"1.5h"``).
    * Negative values (``"-1h"``).
    * Repeated units (``"1h 2h"``).
    """
    if not isinstance(value, str):
        raise DurationParseError(f"expected str, got {type(value).__name__}: {value!r}")
    if not value.strip():
        raise DurationParseError("empty duration string")
    if not _FULL_RE.match(value):
        raise DurationParseError(
            f"invalid duration {value!r}: expected one or more '<n>d', '<n>h', "
            "'<n>m', '<n>s' tokens (e.g. '40h', '1d 16h', '30m')"
        )
    total = 0
    seen: set[str] = set()
    for n_str, unit in _TOKEN_RE.findall(value):
        if unit in seen:
            raise DurationParseError(f"unit {unit!r} appears more than once in {value!r}")
        seen.add(unit)
        total += int(n_str) * _UNIT_SECONDS[unit]
    return float(total)


__all__ = ["DurationParseError", "parse_duration"]
