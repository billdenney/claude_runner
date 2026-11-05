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


AUTH_FAILURE_MARKERS = ("OAuth", ".credentials.json")
"""Text one of which every :class:`UsageApiAuthExpired` message the runner
records carries, and no format-drift message does: the API source names the
OAuth bearer, token or credentials file in each of its login failures, and
:func:`auth_failure_message` adds the first marker to any message that
lacks both. A reader of ``supervisor.json`` tells a login that needs
renewing from a changed usage page by them (:func:`usage_error_kind`),
including the messages an older supervisor recorded unchanged."""


def auth_failure_message(exc: Exception) -> str:
    """The ``last_drift_message`` to record for a login failure ``exc``."""
    message = str(exc)
    return message if usage_error_kind(message) == "auth" else f"OAuth login failed: {message}"


def usage_error_kind(message: str) -> str:
    """``auth`` for a recorded login failure, ``format`` for anything else.

    ``message`` is an account's ``last_drift_message``; see
    :data:`AUTH_FAILURE_MARKERS`.
    """
    return "auth" if any(marker in message for marker in AUTH_FAILURE_MARKERS) else "format"


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

    Missing means a 7d header, or one of the two 5h headers without the
    other. A response with neither 5h header is a reading with no 5-hour
    window only when it shows that none applies: a representative claim
    of another window, no header naming a 5-hour window, and no window
    the source does not know. Otherwise it raises this too (ADR-0038).

    Also raised when a required header is present but its value cannot
    become a reading: unparseable, not finite, a utilization outside
    [0, 100] percent, or a reset timestamp ``datetime`` cannot represent.

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


class UsageCaptureLoginRequired(UsageApiAuthExpired):
    """``claude`` in the account's config dir has no claude.ai plan login.

    Raised by the TTY capture when the TUI's banner names API billing
    (``Claude API`` or ``API Usage Billing``) instead of a plan: the CLI
    then has no subscription to read, and ``/usage`` shows the session's
    cost but not the plan's limit bars. Only an interactive ``/login`` for
    that config dir fixes it, so it is an auth failure (``ErrorDrift``),
    not a capture to retry; its message names ``.credentials.json``, one of
    :data:`AUTH_FAILURE_MARKERS`. See ADR-0039.
    """
