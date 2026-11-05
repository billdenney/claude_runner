"""A ``config_dir`` behind a parent that denies access raises no stray ``PermissionError``.

``Path.exists`` and ``Path.is_file`` raise ``PermissionError`` for such a
path rather than return ``False``. The supervisor's tick catches only the
usage errors it knows (``safe_poll``), so one escaping a usage capture or an
API read would end the tick loop, and one escaping a dispatch's checks would
leave the task to be retried every tick. Each reader of an account's
``config_dir`` turns it into its own error or a documented fallback instead.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from claude_task_runner.claude_init import ensure_initialized
from claude_task_runner.config.paths import ConfigDirError, existing_config_dir
from claude_task_runner.usage.api_source import CredentialsNotFound, _read_oauth_token
from claude_task_runner.usage.capture import _build_spawn_env
from claude_task_runner.usage.drift import UsageCaptureSpawnError
from claude_task_runner.usage.oauth_token_file import read_long_lived_token
from claude_task_runner.usage.whoami import read_credentials

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="root searches a mode-000 directory")

DENIED = "[Errno 13] Permission denied"


@pytest.fixture
def config_dir(tmp_path: Path) -> Iterator[Path]:
    """An account's ``config_dir`` whose parent is mode 000 while the test runs."""
    locked = tmp_path / "locked"
    path = locked / "acct"
    path.mkdir(parents=True)
    (path / "oauth-token").write_text("sk-ant-oat01-x\n", encoding="utf-8")
    (path / ".credentials.json").write_text("{}", encoding="utf-8")
    locked.chmod(0)
    try:
        yield path
    finally:
        locked.chmod(0o755)


def test_existing_config_dir(config_dir: Path) -> None:
    with pytest.raises(ConfigDirError) as excinfo:
        existing_config_dir(str(config_dir))
    assert str(excinfo.value) == (
        f"cannot check CLAUDE_CONFIG_DIR {config_dir}: {DENIED}: '{config_dir}'"
    )


def test_existing_config_dir_that_is_missing(tmp_path: Path) -> None:
    with pytest.raises(ConfigDirError) as excinfo:
        existing_config_dir(str(tmp_path / "absent"))
    assert str(excinfo.value) == f"CLAUDE_CONFIG_DIR does not exist: {tmp_path / 'absent'}"


def test_the_usage_capture_reports_a_spawn_error(config_dir: Path) -> None:
    """The error the tick's ``safe_poll`` catches, for that account alone."""
    with pytest.raises(UsageCaptureSpawnError) as excinfo:
        _build_spawn_env(str(config_dir))
    assert str(excinfo.value) == (
        f"cannot check CLAUDE_CONFIG_DIR {config_dir}: {DENIED}: '{config_dir}'"
    )


def test_the_long_lived_token_reads_as_absent_with_a_warning(
    config_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    token = config_dir / "oauth-token"
    with caplog.at_level(logging.WARNING, logger="claude_task_runner.usage.oauth_token_file"):
        assert read_long_lived_token(str(config_dir)) is None
    assert [r.getMessage() for r in caplog.records] == [
        f"could not read long-lived OAuth token file {token}: {DENIED}: '{token}'"
    ]


def test_the_api_source_reports_missing_credentials(config_dir: Path) -> None:
    creds = config_dir / ".credentials.json"
    with pytest.raises(CredentialsNotFound) as excinfo:
        _read_oauth_token(str(config_dir))
    assert str(excinfo.value) == f"could not read {creds}: {DENIED}: '{creds}'"


def test_whoami_reads_no_credentials(config_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    creds = config_dir / ".credentials.json"
    with caplog.at_level(logging.WARNING, logger="claude_task_runner.usage.whoami"):
        assert read_credentials(str(config_dir)) == {}
    assert [r.getMessage() for r in caplog.records] == [
        f"could not read {creds}: {DENIED}: '{creds}'; reporting no credentials"
    ]


def test_claude_init_leaves_it_alone(config_dir: Path, tmp_path: Path) -> None:
    assert ensure_initialized(str(config_dir), tmp_path) is False
