"""Usage source layer.

The supervisor consumes ``UsageReading`` values via an abstract
:class:`UsageSource` so that tests can drive the state machine through
scripted readings without spawning ``claude``. See ADR-0009.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from claude_task_runner.clock import Clock
from claude_task_runner.config.schema import UsageSettings
from claude_task_runner.usage import capture as capture_mod
from claude_task_runner.usage import parser as parser_mod
from claude_task_runner.usage.models import UsageReading


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
