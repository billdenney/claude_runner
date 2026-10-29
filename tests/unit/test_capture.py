"""``capture()`` reports a usage capture that cannot run as a capture error.

An ``OSError`` escaping ``capture()`` used to escape the supervisor's
``safe_poll`` too and end the supervisor loop. Creating the captures
directory and spawning ``claude`` now raise :class:`UsageCaptureSpawnError`,
which the tick skips; a ``.cap`` that cannot be written costs only the
forensics file, not the reading.
"""

from __future__ import annotations

import io
import logging
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
FIXTURE = Path(__file__).parent.parent / "fixtures" / "usage" / "synthetic_normal.cap"
CAPTURE_LOGGER = "claude_task_runner.usage.capture"


class _FakeChild:
    """A ``claude`` whose TUI is ready at once and shows a recorded ``/usage``.

    The whole recording reaches ``logfile_read`` on the first ``expect``, as
    pexpect would copy it while matching ``shortcuts``.
    """

    def __init__(self, raw: bytes) -> None:
        self._raw = raw
        self._answers = iter([3, 0, 0, 0])  # shortcuts, Resets, Resets, EOF
        self.logfile_read: io.BytesIO | None = None

    def expect(self, *args: Any, **kwargs: Any) -> int:
        if self._raw and self.logfile_read is not None:
            self.logfile_read.write(self._raw)
            self._raw = b""
        return next(self._answers)

    def send(self, data: bytes) -> None:
        pass

    def sendline(self, data: str) -> None:
        pass

    def isalive(self) -> bool:
        return False


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


def test_a_written_capture_returns_its_path(settings: UsageSettings, tmp_path: Path) -> None:
    raw = FIXTURE.read_bytes()
    captures_dir = tmp_path / "caps"
    with patch.object(capture_mod.pexpect, "spawn", return_value=_FakeChild(raw)):
        got, path = capture_mod.capture(settings, FakeClock(NOW), captures_dir=captures_dir)
    assert got == raw
    assert path == captures_dir / "20261029T120000Z.cap"
    assert path.read_bytes() == raw


def test_a_capture_file_that_cannot_be_written_keeps_the_reading(
    settings: UsageSettings, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    raw = FIXTURE.read_bytes()
    captures_dir = tmp_path / "caps"
    cap = captures_dir / "20261029T120000Z.cap"
    with (
        patch.object(capture_mod.pexpect, "spawn", return_value=_FakeChild(raw)),
        patch.object(Path, "write_bytes", side_effect=OSError(28, "No space left on device")),
        caplog.at_level(logging.WARNING, logger=CAPTURE_LOGGER),
    ):
        got, path = capture_mod.capture(settings, FakeClock(NOW), captures_dir=captures_dir)
    assert (got, path) == (raw, None)
    assert not cap.exists()
    assert [r.getMessage() for r in caplog.records if r.name == CAPTURE_LOGGER] == [
        f"usage capture: cannot write the forensics file {cap}: [Errno 28] No space left on device"
    ]
