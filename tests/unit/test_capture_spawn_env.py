"""Tests for ``usage.capture._build_spawn_env`` (PR 15).

PR 14 added per-account long-lived OAuth tokens at
``<config_dir>/oauth-token`` and wired them into ``ApiUsageSource``
and ``dispatcher.py``. The TTY capture path in ``usage/capture.py``
was missed: it spawned ``claude /usage`` with ``CLAUDE_CONFIG_DIR``
but no ``CLAUDE_CODE_OAUTH_TOKEN``, so pure-tty ``[usage].source``
deployments couldn't benefit from ``setup-token``. PR 15 closes
that gap via the same helper pattern.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_task_runner.usage.capture import _build_spawn_env
from claude_task_runner.usage.drift import UsageCaptureSpawnError

# ---------------------------------------------------------------------------
# Empty config_dir: env inheritance (pre-PR-14 single-account behaviour).
# ---------------------------------------------------------------------------


def test_empty_config_dir_returns_none() -> None:
    """Empty ``claude_config_dir`` means "use the supervisor's env
    unchanged"; pexpect spawn's ``env=None`` triggers inheritance."""
    assert _build_spawn_env("") is None


# ---------------------------------------------------------------------------
# Set config_dir without oauth-token: only CLAUDE_CONFIG_DIR is added.
# ---------------------------------------------------------------------------


def test_config_dir_only_exports_config_dir(tmp_path: Path) -> None:
    env = _build_spawn_env(str(tmp_path))
    assert env is not None
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_config_dir_inherits_supervisors_env(tmp_path: Path, monkeypatch) -> None:
    """Process env (PATH, HOME, etc.) is copied through, not stripped."""
    monkeypatch.setenv("FROM_SUPERVISOR_PROCESS", "sentinel-value")
    env = _build_spawn_env(str(tmp_path))
    assert env is not None
    assert env.get("FROM_SUPERVISOR_PROCESS") == "sentinel-value"


# ---------------------------------------------------------------------------
# Long-lived token: CLAUDE_CODE_OAUTH_TOKEN is added when oauth-token exists.
# ---------------------------------------------------------------------------


def test_long_lived_token_exported_alongside_config_dir(tmp_path: Path) -> None:
    (tmp_path / "oauth-token").write_text("sk-ant-oat01-LONG\n", encoding="utf-8")
    env = _build_spawn_env(str(tmp_path))
    assert env is not None
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-LONG"


def test_long_lived_token_overrides_any_inherited_value(tmp_path: Path, monkeypatch) -> None:
    """If the supervisor itself was launched with
    CLAUDE_CODE_OAUTH_TOKEN set (e.g. someone exported it for
    development), the per-account file wins — that's what the
    operator presumably intended by putting it under <config_dir>."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "inherited-value")
    (tmp_path / "oauth-token").write_text("sk-ant-oat01-PERACCOUNT\n", encoding="utf-8")
    env = _build_spawn_env(str(tmp_path))
    assert env is not None
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-PERACCOUNT"


def test_empty_long_lived_file_not_exported(tmp_path: Path) -> None:
    """A half-written oauth-token file (read_long_lived_token returns
    None for empty / whitespace-only) must not export an empty token
    that would 401 the CLI."""
    (tmp_path / "oauth-token").write_text("   \n", encoding="utf-8")
    env = _build_spawn_env(str(tmp_path))
    assert env is not None
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


# ---------------------------------------------------------------------------
# Pre-condition: nonexistent config_dir raises (caller sees a single
# exception type from the spawn site).
# ---------------------------------------------------------------------------


def test_nonexistent_config_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(UsageCaptureSpawnError) as exc_info:
        _build_spawn_env(str(tmp_path / "does-not-exist"))
    assert "does-not-exist" in str(exc_info.value)
