"""Tests for ``usage.capture._build_spawn_env``.

The TTY capture must read the account's claude.ai login. Under
``CLAUDE_CODE_OAUTH_TOKEN`` the CLI uses that token instead, and a
``setup-token`` token cannot show the plan's limits in ``/usage``
(ADR-0039), so the capture's environment never carries the variable:
not from the account's ``oauth-token`` file, and not inherited.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_task_runner.usage.capture import _build_spawn_env
from claude_task_runner.usage.drift import UsageCaptureSpawnError

# ---------------------------------------------------------------------------
# Empty config_dir: env inheritance (pre-PR-14 single-account behaviour).
# ---------------------------------------------------------------------------


def test_empty_config_dir_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty ``claude_config_dir`` means "use the supervisor's env
    unchanged"; pexpect spawn's ``env=None`` triggers inheritance."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert _build_spawn_env("") is None


def test_empty_config_dir_drops_an_inherited_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no config dir, the env is the supervisor's minus the token,
    and no ``CLAUDE_CONFIG_DIR`` is added."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "inherited-value")
    monkeypatch.setenv("FROM_SUPERVISOR_PROCESS", "sentinel-value")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    env = _build_spawn_env("")
    assert env is not None
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "CLAUDE_CONFIG_DIR" not in env
    assert env["FROM_SUPERVISOR_PROCESS"] == "sentinel-value"


# ---------------------------------------------------------------------------
# Set config_dir: CLAUDE_CONFIG_DIR is added, the supervisor's env kept.
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
# Long-lived token: never in the capture's environment.
# ---------------------------------------------------------------------------


def test_long_lived_token_file_is_not_exported(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    (tmp_path / "oauth-token").write_text("sk-ant-oat01-LONG\n", encoding="utf-8")
    env = _build_spawn_env(str(tmp_path))
    assert env is not None
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_inherited_token_is_dropped(tmp_path: Path, monkeypatch) -> None:
    """A supervisor launched with CLAUDE_CODE_OAUTH_TOKEN set (exported for
    development, say) must not pass it on: the CLI would prefer it to the
    account's login, and the per-account file is not exported either."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "inherited-value")
    (tmp_path / "oauth-token").write_text("sk-ant-oat01-PERACCOUNT\n", encoding="utf-8")
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
