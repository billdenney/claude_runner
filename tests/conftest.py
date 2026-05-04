"""Shared pytest fixtures."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.config.schema import Settings


@pytest.fixture
def fake_clock() -> FakeClock:
    """A FakeClock anchored at 2026-05-03T18:00:00Z."""
    return FakeClock(datetime(2026, 5, 3, 18, 0, 0, tzinfo=UTC))


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def usage_fixtures_dir(fixtures_dir: Path) -> Path:
    return fixtures_dir / "usage"


@pytest.fixture
def default_settings() -> Settings:
    """The package defaults loaded with no per-queue overrides."""
    return load_settings(None)
