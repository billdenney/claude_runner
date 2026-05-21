"""Usage source layer.

The supervisor consumes ``UsageReading`` values via an abstract
:class:`UsageSource` so that tests can drive the state machine through
scripted readings without spawning ``claude``. See ADR-0009.

Three production sources are available, selected by ``[usage].source``:

* ``"tty"`` — the original :class:`ClaudeUsageSource`; spawns
  ``claude /usage`` in a PTY and scrapes the rendered TUI. Slow
  (10-30s/capture) but reads exactly what the operator sees.
* ``"api"`` — :class:`ApiUsageSource` (in ``api_source.py``); reads
  rate-limit response headers from a minimal ``/v1/messages`` call.
  Fast (~500ms) and cheap (~4 tokens), but the headers are reverse-
  engineered and the OAuth token can expire.
* ``"api_then_tty"`` — :class:`ApiThenTtyUsageSource` (this module);
  tries API first, falls through to TTY on auth-expired / missing-
  header / network error. The TTY fall-through naturally refreshes
  the OAuth token as a side effect of spawning ``claude``.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from claude_task_runner.clock import Clock
from claude_task_runner.config.schema import UsageSettings
from claude_task_runner.usage import capture as capture_mod
from claude_task_runner.usage import parser as parser_mod
from claude_task_runner.usage.drift import (
    UsageApiAuthExpired,
    UsageApiHeaderMissing,
    UsageApiNetworkError,
)
from claude_task_runner.usage.models import UsageReading

logger = logging.getLogger(__name__)


class UsageSource(Protocol):
    """Source of usage readings."""

    def read(self) -> UsageReading: ...


class ClaudeUsageSource:
    """Production source: capture ``claude /usage``, parse, return."""

    def __init__(
        self,
        settings: UsageSettings,
        clock: Clock,
        *,
        captures_dir: Path,
        claude_executable: str = "claude",
        claude_config_dir: str = "",
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._captures_dir = captures_dir
        self._claude_executable = claude_executable
        self._claude_config_dir = claude_config_dir

    def read(self) -> UsageReading:
        raw, cap_path = capture_mod.capture(
            self._settings,
            self._clock,
            captures_dir=self._captures_dir,
            claude_executable=self._claude_executable,
            claude_config_dir=self._claude_config_dir,
        )
        reading = parser_mod.parse(raw, self._clock.now(), self._clock)
        return reading.model_copy(update={"capture_path": str(cap_path)})


class ApiThenTtyUsageSource:
    """Composite source: try API first, fall through to TTY on documented failures.

    The API path is fast (~500ms) but the OAuth bearer can expire and
    the rate-limit header names are reverse-engineered. The TTY path
    spawns ``claude /usage``, which both reads usage and refreshes
    the OAuth token natively. This composite gets the best of both:
    the fast path the vast majority of the time, with a self-healing
    fall-through that also keeps the token fresh.

    Fall-through triggers (all defined in ``usage.drift``):

    * :class:`UsageApiAuthExpired` — 401/403; the TTY spawn refreshes
      the OAuth bearer as a side effect, so the next tick goes back
      to the API path.
    * :class:`UsageApiHeaderMissing` — the response was OK but the
      ``anthropic-ratelimit-unified-*`` headers we depend on are gone
      or renamed (a possible Anthropic-side change). The TTY path is
      independent of that surface.
    * :class:`UsageApiNetworkError` — network/TLS/DNS failure; the
      TTY path goes through ``claude`` which has its own retry
      machinery.

    Any other exception (including format-drift on the TTY side)
    propagates unchanged so the supervisor's existing error routing
    fires.
    """

    def __init__(self, api: UsageSource, tty: UsageSource) -> None:
        self._api = api
        self._tty = tty

    def read(self) -> UsageReading:
        try:
            return self._api.read()
        except (UsageApiAuthExpired, UsageApiHeaderMissing, UsageApiNetworkError) as exc:
            logger.info(
                "API usage source failed (%s: %s); falling through to TTY",
                type(exc).__name__,
                exc,
            )
            return self._tty.read()


class FakeUsageSource:
    """Scripted source for tests.

    Yields readings from a list in order; once exhausted, the last reading
    is returned indefinitely. Use :meth:`set_readings` to replace the
    script mid-test.
    """

    def __init__(self, readings: list[UsageReading]) -> None:
        if not readings:
            raise ValueError("FakeUsageSource requires at least one reading")
        self._readings: list[UsageReading] = list(readings)
        self._iter: Iterator[UsageReading] = iter(self._readings)
        self._last: UsageReading = self._readings[0]

    def set_readings(self, readings: list[UsageReading]) -> None:
        if not readings:
            raise ValueError("set_readings requires at least one reading")
        self._readings = list(readings)
        self._iter = iter(self._readings)
        self._last = self._readings[0]

    def read(self) -> UsageReading:
        with contextlib.suppress(StopIteration):
            self._last = next(self._iter)
        return self._last
