"""Tests for the per-account long-lived OAuth token file helper (PR 14)."""

from __future__ import annotations

import os
from pathlib import Path

from claude_task_runner.usage.oauth_token_file import (
    OAUTH_TOKEN_FILENAME,
    oauth_token_path,
    read_long_lived_token,
)

# ---------------------------------------------------------------------------
# oauth_token_path: resolves to ``<config_dir>/oauth-token``; empty config_dir
# defaults to ``~/.claude`` (mirrors api_source._read_oauth_token convention).
# ---------------------------------------------------------------------------


def test_oauth_token_path_with_config_dir(tmp_path: Path) -> None:
    assert oauth_token_path(str(tmp_path)) == tmp_path / OAUTH_TOKEN_FILENAME


def test_oauth_token_path_empty_falls_back_to_home() -> None:
    assert oauth_token_path("") == Path.home() / ".claude" / OAUTH_TOKEN_FILENAME


def test_oauth_token_path_expands_tilde(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert oauth_token_path("~/.claude_alt") == tmp_path / ".claude_alt" / OAUTH_TOKEN_FILENAME


# ---------------------------------------------------------------------------
# read_long_lived_token: returns the stripped contents, or None for any of
# the documented "treat as not configured" conditions.
# ---------------------------------------------------------------------------


def test_returns_none_when_file_absent(tmp_path: Path) -> None:
    assert read_long_lived_token(str(tmp_path)) is None


def test_returns_stripped_token(tmp_path: Path) -> None:
    (tmp_path / OAUTH_TOKEN_FILENAME).write_text("  sk-ant-oat01-EXAMPLE  \n", encoding="utf-8")
    assert read_long_lived_token(str(tmp_path)) == "sk-ant-oat01-EXAMPLE"


def test_returns_none_for_whitespace_only_file(tmp_path: Path) -> None:
    """A half-written file produces an empty/whitespace string; treating it
    as ``None`` keeps the supervisor from emitting a 401 storm during the
    operator's setup-token paste."""
    (tmp_path / OAUTH_TOKEN_FILENAME).write_text("   \n\t\n", encoding="utf-8")
    assert read_long_lived_token(str(tmp_path)) is None


def test_returns_none_for_empty_file(tmp_path: Path) -> None:
    (tmp_path / OAUTH_TOKEN_FILENAME).write_text("", encoding="utf-8")
    assert read_long_lived_token(str(tmp_path)) is None


def test_loose_permissions_logged_but_token_returned(tmp_path: Path, caplog) -> None:
    """A world-readable token file is a security smell but not fatal —
    the supervisor logs a warning and proceeds."""
    path = tmp_path / OAUTH_TOKEN_FILENAME
    path.write_text("sk-ant-oat01-EXAMPLE\n", encoding="utf-8")
    os.chmod(path, 0o644)  # group+other readable
    with caplog.at_level("WARNING"):
        token = read_long_lived_token(str(tmp_path))
    assert token == "sk-ant-oat01-EXAMPLE"
    assert any("loose permissions" in r.message for r in caplog.records)


def test_tight_permissions_no_warning(tmp_path: Path, caplog) -> None:
    path = tmp_path / OAUTH_TOKEN_FILENAME
    path.write_text("sk-ant-oat01-EXAMPLE\n", encoding="utf-8")
    os.chmod(path, 0o600)
    with caplog.at_level("WARNING"):
        token = read_long_lived_token(str(tmp_path))
    assert token == "sk-ant-oat01-EXAMPLE"
    assert not any("loose permissions" in r.message for r in caplog.records)
