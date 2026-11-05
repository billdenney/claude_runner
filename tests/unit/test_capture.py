"""``capture()`` reports a usage capture that cannot run as a capture error.

An ``OSError`` escaping ``capture()`` used to escape the supervisor's
``safe_poll`` too and end the supervisor loop. Creating the captures
directory and spawning ``claude`` now raise :class:`UsageCaptureSpawnError`,
which the tick skips; a ``.cap`` that cannot be written costs only the
forensics file, not the reading; that case runs through the capture's
start-up screens in test_capture_tui.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pexpect
import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.config.schema import UsageSettings
from claude_task_runner.usage import capture as capture_mod
from claude_task_runner.usage.drift import UsageCaptureSpawnError

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


@pytest.fixture
def settings() -> UsageSettings:
    return load_settings(None).usage.model_copy(
        update={"capture_post_ready_pad_ms": 0, "capture_post_data_pad_ms": 0}
    )


@pytest.fixture(autouse=True)
def claude_on_path() -> Any:
    with (
        patch.object(capture_mod.shutil, "which", return_value="/usr/bin/claude"),
        patch("claude_task_runner.claude_init.ensure_initialized", return_value=False),
    ):
        yield


def test_a_captures_dir_that_cannot_be_created(settings: UsageSettings, tmp_path: Path) -> None:
    blocker = tmp_path / "runtime"
    blocker.write_text("a file where the directory should be", encoding="utf-8")
    captures_dir = blocker / "usage_captures"
    with (
        patch.object(capture_mod.pexpect, "spawn", side_effect=AssertionError("spawned")),
        pytest.raises(UsageCaptureSpawnError) as caught,
    ):
        capture_mod.capture(settings, FakeClock(NOW), captures_dir=captures_dir)
    assert str(caught.value) == (
        f"cannot create the usage captures directory {captures_dir}: "
        f"[Errno 20] Not a directory: '{captures_dir}'"
    )
    assert isinstance(caught.value.__cause__, NotADirectoryError)


@pytest.mark.parametrize(
    ("error", "shown"),
    [
        (OSError(5, "out of pty devices"), "[Errno 5] out of pty devices"),
        (
            pexpect.ExceptionPexpect("The command was not found or was not executable: claude."),
            "The command was not found or was not executable: claude.",
        ),
    ],
)
def test_a_claude_that_cannot_be_spawned(
    settings: UsageSettings, tmp_path: Path, error: Exception, shown: str
) -> None:
    with (
        patch.object(capture_mod.pexpect, "spawn", side_effect=error),
        pytest.raises(UsageCaptureSpawnError) as caught,
    ):
        capture_mod.capture(settings, FakeClock(NOW), captures_dir=tmp_path / "caps")
    assert str(caught.value) == f"cannot start claude: {shown}"
    assert caught.value.__cause__ is error
