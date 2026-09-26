"""Drift and capture-failure exceptions for usage parsing.

**Format drift** — the parser couldn't extract two valid blocks from the
raw bytes. Likely Anthropic changed the TUI layout. Raised by the parser
itself; the supervisor halts dispatch on this. The other exceptions here
tell a transient capture failure (a timeout, a network error) from a
structural one, so the daemon can react to each.

Nothing compares consecutive readings; see invariant 3 in
``docs/architecture.md``.
"""

from __future__ import annotations


class UsageFormatDrift(ValueError):
    """The parser could not produce a valid `UsageReading`.

    The exception carries the raw bytes that triggered it so a forensics
    trail is always available, even if persistence to a `.cap` file failed.
    """

    def __init__(self, message: str, raw: bytes | None = None) -> None:
        super().__init__(message)
        self.raw = raw


class UsageCaptureTimeout(RuntimeError):
    """`claude /usage` did not render both Resets lines within the timeout.

    This is NOT format drift — the TUI structure may be unchanged but the
    OAuth API was slow, or `claude` was unresponsive.
    """


class UsageCaptureSpawnError(RuntimeError):
    """`claude` could not be spawned (binary missing, permission denied, etc.)."""


class UsageApiAuthExpired(UsageCaptureSpawnError):
    """The OAuth bearer token in ``<config_dir>/.credentials.json`` returned 401/403.

    The API usage source uses the same OAuth credentials the
    ``claude`` CLI does; the CLI refreshes them in-process. When the
    runner reads them directly and the access token has expired, the
    only safe response is to fall through to the TTY source for one
    capture (which spawns ``claude``, which refreshes the token as a
    side effect) and retry the API path next tick.

    Inherits from ``UsageCaptureSpawnError`` so the daemon's existing
    ``safe_poll`` routes it correctly without any new branches.
    """


class UsageApiHeaderMissing(UsageFormatDrift):
    """The ``/v1/messages`` response was missing one of the documented headers.

    The ``anthropic-ratelimit-unified-{5h,7d}-{utilization,reset,status}``
    headers are reverse-engineered (see the andrew-kramer-inno gist
    referenced in the ADR). Anthropic could rename or remove them
    without warning. Surfacing this as ``UsageFormatDrift`` re-uses
    the supervisor's existing format-drift response (halt dispatch,
    log the offending payload, fall back to TTY in
    ``api_then_tty`` mode).
    """


class UsageApiNetworkError(UsageCaptureTimeout):
    """The ``/v1/messages`` call could not be completed (connection / TLS / DNS).

    Inherits from ``UsageCaptureTimeout`` so the daemon classifies it
    the same way as a slow TTY capture — observable as a transient
    failure rather than a structural one.
    """
