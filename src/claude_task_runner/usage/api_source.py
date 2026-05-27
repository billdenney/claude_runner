"""Derive usage readings from ``/v1/messages`` response headers.

This source is the fast-path alternative to spawning ``claude /usage``
in a PTY: it makes a single minimal ``/v1/messages`` POST against
``api.anthropic.com``, reads the ``anthropic-ratelimit-unified-*``
headers from the response, and assembles a :class:`UsageReading`
from them. One round trip vs. a 10-30s TUI render.

The header names are documented in
https://gist.github.com/andrew-kramer-inno/34f9303a5cc29a14af7c2e729b676fc9
(reverse-engineered from live API traffic). They are not in the public
Anthropic docs, so the source treats absent / renamed headers as
:class:`UsageApiHeaderMissing` — a format-drift signal the supervisor
can route on without confusing it for a network timeout.

Authentication: two-stage lookup per account.

1. **Long-lived token (PR 14)** — if ``<config_dir>/oauth-token``
   exists and contains a non-empty string, use it as the bearer.
   The file is produced by ``claude setup-token`` and lasts ~1 year;
   the runner never tries to refresh it (it's not refreshable by
   design, and the long lifetime makes refresh cycles irrelevant).
2. **Short-lived token (PR 6, legacy)** — fall back to the
   ``accessToken`` field in ``<config_dir>/.credentials.json`` (the
   same file ``claude /login`` writes). The Claude Code CLI
   refreshes this token internally on every successful CLI call;
   when the runner reads it directly and the access token has
   expired (and the refresh token can't roll it forward), this
   source raises :class:`UsageApiAuthExpired`.

The ``api_then_tty`` composite source falls through to the TTY
source on :class:`UsageApiAuthExpired` (the TTY path spawns
``claude``, which refreshes the short-lived token as a side
effect). For accounts on long-lived tokens, the supervisor's per-
account source selection (see ``supervisor_cmd._build_source``)
skips the TTY composite — TTY can't recover a revoked long-lived
token either, so the right response is to escalate to ERROR_DRIFT
and notify the operator.

No new third-party deps: uses stdlib ``urllib.request`` so the runner
install footprint is unchanged.

Token cost: each read sends ``max_tokens=1`` against the cheapest
model. At a 60-second poll interval this is ~5,800 tokens/day —
negligible against any Max-plan budget. Output is not consumed
beyond reading the headers.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from claude_task_runner.clock import Clock
from claude_task_runner.usage.drift import (
    UsageApiAuthExpired,
    UsageApiHeaderMissing,
    UsageApiNetworkError,
)
from claude_task_runner.usage.models import UsageReading, WindowReading
from claude_task_runner.usage.oauth_token_file import read_long_lived_token

logger = logging.getLogger(__name__)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
"""Production messages endpoint. Override via constructor for tests."""

ANTHROPIC_VERSION = "2023-06-01"
"""``anthropic-version`` header required by /v1/messages. Pinned to
the stable value the Claude Code CLI uses today; bump when Anthropic
GA's a newer version."""

DEFAULT_PROBE_MODEL = "claude-haiku-4-5"
"""Cheapest model for the probe call. ``max_tokens=1`` keeps the
billable cost to a single output token plus the ~3-token user prompt."""

_KNOWN_OK_STATUSES: frozenset[str] = frozenset({"allowed", "allowed_warning"})
"""``anthropic-ratelimit-unified-{5h,7d}-status`` values that mean
"the API will accept dispatches" and require no special handling.

The gist that documented the headers initially listed only
``"allowed"``. Live traffic against the personal account on
2026-05-21 returned ``"allowed_warning"`` when the 7d window was
above ~70% utilization. Both values are benign for our purposes:
the supervisor drives state transitions off the *utilization
percentage*, not the status string, and the configured thresholds
in ``[dispatch_pct.*]`` already encode the operator's chosen
slowdown / stop points. Treating ``"allowed_warning"`` as
"slow down now" would double-count those thresholds and
prematurely throttle dispatch.

Statuses NOT in this set are logged once per capture (not raised)
so ops can extend the enumeration as Anthropic introduces new
values. The log is the early-warning channel; promotion to
state-machine signals (if any) belongs in a separate ADR."""

# OAuth credential file keys, in priority order. Different Claude Code
# versions and login flows have stored the access token under different
# nested paths; we try each before giving up so the operator gets a
# clear error rather than a key-not-found traceback.
_CREDENTIAL_KEY_PATHS: tuple[tuple[str, ...], ...] = (
    ("claudeAiOauth", "accessToken"),
    ("oauth", "accessToken"),
    ("oauth", "access_token"),
    ("accessToken",),
    ("access_token",),
)


class CredentialsNotFound(UsageApiAuthExpired):
    """``<config_dir>/.credentials.json`` is missing or has no recognizable token.

    Surfaced as ``UsageApiAuthExpired`` so the composite ``api_then_tty``
    source falls through to the TTY path (which will either spawn
    ``claude`` to log in or fail with a clear PTY-side error)."""


class ApiUsageSource:
    """Production source: derive usage from ``/v1/messages`` response headers.

    Parameters
    ----------
    clock
        Time source for the ``captured_at`` field. Tests inject a
        ``FakeClock`` to make readings deterministic.
    config_dir
        ``CLAUDE_CONFIG_DIR`` for the account whose usage we want.
        Empty string targets ``~/.claude``. The class reads
        ``<config_dir>/.credentials.json`` for the OAuth bearer.
    probe_model
        Model name for the probe call. Default
        ``"claude-haiku-4-5"``. The choice only matters for cost; the
        rate-limit headers come back the same regardless.
    api_url
        Override for tests / non-production endpoints. Defaults to
        ``ANTHROPIC_API_URL``.
    timeout_s
        Per-call HTTP timeout in seconds. Defaults to 10s — generous
        enough to swallow ordinary network jitter, tight enough that
        the daemon's tick loop doesn't stall on a stuck connection.
    """

    def __init__(
        self,
        clock: Clock,
        *,
        config_dir: str = "",
        probe_model: str = DEFAULT_PROBE_MODEL,
        api_url: str = ANTHROPIC_API_URL,
        timeout_s: float = 10.0,
    ) -> None:
        self._clock = clock
        self._config_dir = config_dir
        self._probe_model = probe_model
        self._api_url = api_url
        self._timeout_s = timeout_s

    def read(self) -> UsageReading:
        """Make one probe call and convert response headers into a reading.

        Raises
        ------
        UsageApiAuthExpired
            Credentials missing, malformed, or 401/403 from the API.
        UsageApiHeaderMissing
            The response was OK but didn't carry the documented
            rate-limit headers.
        UsageApiNetworkError
            Connection failed, TLS failed, DNS failed, or any non-
            auth HTTP error code.
        """
        token = _read_oauth_token(self._config_dir)
        payload = json.dumps(
            {
                "model": self._probe_model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "."}],
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._api_url,
            data=payload,
            method="POST",
            headers={
                "authorization": f"Bearer {token}",
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                headers = dict(resp.headers.items())
                # Read & discard the body so the connection can be
                # cleanly closed; we don't need the content.
                resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise UsageApiAuthExpired(
                    f"OAuth bearer rejected by {self._api_url} (HTTP {exc.code})"
                ) from exc
            raise UsageApiNetworkError(
                f"{self._api_url} returned HTTP {exc.code}: {exc.reason}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise UsageApiNetworkError(f"{self._api_url}: {exc}") from exc

        return _headers_to_reading(headers, self._clock.now())


def _read_oauth_token(config_dir: str) -> str:
    """Extract the OAuth bearer for ``config_dir``.

    Priority (PR 14):

    1. ``<config_dir>/oauth-token`` — long-lived token from
       ``claude setup-token``. Wins outright if present and non-empty.
       Bypasses ``.credentials.json`` entirely so a stale / dead
       short-lived ``accessToken`` does not poison the lookup.
    2. ``<config_dir>/.credentials.json`` — short-lived OAuth bearer
       written by ``claude /login``. Tries each path in
       :data:`_CREDENTIAL_KEY_PATHS` until one resolves to a non-empty
       string.

    Raises :class:`CredentialsNotFound` only when BOTH stages produce
    nothing — the operator can then either run ``claude setup-token``
    (recommended) or ``claude /login`` to populate one of the two.
    """
    long_lived = read_long_lived_token(config_dir)
    if long_lived is not None:
        return long_lived

    creds_path = (
        Path(config_dir).expanduser() if config_dir else Path.home() / ".claude"
    ) / ".credentials.json"
    if not creds_path.exists():
        raise CredentialsNotFound(
            f"OAuth credentials file not found: {creds_path}. "
            f"Run `CLAUDE_CONFIG_DIR={creds_path.parent} claude /login` first."
        )
    try:
        with creds_path.open("rb") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise CredentialsNotFound(f"could not read {creds_path}: {exc}") from exc

    for path in _CREDENTIAL_KEY_PATHS:
        node: Any = data
        for key in path:
            if not isinstance(node, dict) or key not in node:
                node = None
                break
            node = node[key]
        if isinstance(node, str) and node:
            return node

    tried = ", ".join(".".join(p) for p in _CREDENTIAL_KEY_PATHS)
    raise CredentialsNotFound(
        f"no OAuth access token found in {creds_path}. Tried key paths: {tried}. "
        "Inspect the file shape and add the new key path to "
        "claude_task_runner.usage.api_source._CREDENTIAL_KEY_PATHS."
    )


def _headers_to_reading(headers: dict[str, str], captured_at: datetime) -> UsageReading:
    """Convert response headers into a :class:`UsageReading`.

    Raises :class:`UsageApiHeaderMissing` if any of the four required
    headers (``-5h-utilization``, ``-5h-reset``, ``-7d-utilization``,
    ``-7d-reset``) is absent or unparseable.

    Header lookup is case-insensitive — ``urllib`` preserves the wire
    casing, which can vary by HTTP/2 vs 1.1 framing.
    """
    lower = {k.lower(): v for k, v in headers.items()}

    five_h_util_raw = lower.get("anthropic-ratelimit-unified-5h-utilization")
    five_h_reset_raw = lower.get("anthropic-ratelimit-unified-5h-reset")
    week_util_raw = lower.get("anthropic-ratelimit-unified-7d-utilization")
    week_reset_raw = lower.get("anthropic-ratelimit-unified-7d-reset")
    missing = [
        name
        for name, val in (
            ("anthropic-ratelimit-unified-5h-utilization", five_h_util_raw),
            ("anthropic-ratelimit-unified-5h-reset", five_h_reset_raw),
            ("anthropic-ratelimit-unified-7d-utilization", week_util_raw),
            ("anthropic-ratelimit-unified-7d-reset", week_reset_raw),
        )
        if val is None
    ]
    if missing:
        raise UsageApiHeaderMissing(
            f"missing rate-limit headers from /v1/messages: {missing}. "
            "Possible Anthropic header rename — falling back to TTY source."
        )

    # Log unknown statuses without raising; the supervisor's state
    # machine drives off the utilization percentage, so an unknown
    # status doesn't gate dispatch. ``"allowed"`` and
    # ``"allowed_warning"`` are both treated as benign (see
    # ``_KNOWN_OK_STATUSES``). The log is the early-warning channel
    # for ops to map out the enumeration as new values appear.
    for window in ("5h", "7d"):
        status = lower.get(f"anthropic-ratelimit-unified-{window}-status")
        if status is not None and status not in _KNOWN_OK_STATUSES:
            logger.warning(
                "API usage source: %s status=%r is outside the recognised "
                "set (%s); the supervisor still routes on utilization%%.",
                window,
                status,
                sorted(_KNOWN_OK_STATUSES),
            )

    # After the `missing` check above, none of these are None; the
    # explicit assertion narrows the type for mypy without adding a
    # runtime ignore comment.
    assert five_h_util_raw is not None
    assert five_h_reset_raw is not None
    assert week_util_raw is not None
    assert week_reset_raw is not None
    five_h = _window_from_headers(
        utilization=five_h_util_raw,
        reset=five_h_reset_raw,
    )
    week = _window_from_headers(
        utilization=week_util_raw,
        reset=week_reset_raw,
    )
    return UsageReading(
        captured_at=captured_at,
        five_hour=five_h,
        seven_day=week,
    )


def _window_from_headers(*, utilization: str, reset: str) -> WindowReading:
    """Parse one window's util + reset headers into a :class:`WindowReading`.

    Utilization arrives as a float in [0.0, 1.0]; the schema field is
    an int in [0, 100] so we multiply and round. Reset arrives as a
    Unix timestamp (seconds since epoch).
    """
    try:
        util_pct = max(0, min(100, round(float(utilization) * 100)))
    except (TypeError, ValueError) as exc:
        raise UsageApiHeaderMissing(
            f"unparseable utilization header value {utilization!r}: {exc}"
        ) from exc
    try:
        resets_at = datetime.fromtimestamp(int(reset), tz=UTC)
    except (TypeError, ValueError) as exc:
        raise UsageApiHeaderMissing(f"unparseable reset header value {reset!r}: {exc}") from exc
    return WindowReading(
        utilization_pct=util_pct,
        resets_at_raw=str(reset),
        resets_at=resets_at,
    )
