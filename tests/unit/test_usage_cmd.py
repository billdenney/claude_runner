"""Tests for cli.usage_cmd — render / json / healthcheck / capture / parse-file / whoami.

All commands except ``parse-file`` go through ``capture_mod.capture``,
which spawns the real ``claude`` binary via pexpect. We mock that
function to return fixture bytes (or raise the relevant exception) so
every code path is exercised without ever touching Claude.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from claude_task_runner.cli.usage_cmd import _bar, _default_captures_dir, app
from claude_task_runner.usage.drift import (
    UsageCaptureSpawnError,
    UsageCaptureTimeout,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "usage"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fixture_bytes() -> bytes:
    """A small known-good capture: 5h=38%, weekly=20%."""
    return (FIXTURE_DIR / "synthetic_normal.cap").read_bytes()


@pytest.fixture
def fixture_path(fixture_bytes: bytes, tmp_path: Path) -> Path:
    p = tmp_path / "sample.cap"
    p.write_bytes(fixture_bytes)
    return p


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pct, expected_filled",
    [
        (0, 0),
        (5, 1),
        (50, 10),
        (95, 19),
        (100, 20),
    ],
)
def test_bar_length(pct: int, expected_filled: int) -> None:
    bar = _bar(pct)
    assert len(bar) == 20
    assert bar.count("█") == expected_filled
    assert bar.count("░") == 20 - expected_filled


def test_default_captures_dir_is_under_home() -> None:
    p = _default_captures_dir()
    assert p.parent.parent == Path.home()
    assert p.name == "usage_captures"


# ---------------------------------------------------------------------------
# `parse-file` — no capture; reads fixture directly
# ---------------------------------------------------------------------------


def test_parse_file_happy_path(runner: CliRunner, fixture_path: Path) -> None:
    result = runner.invoke(app, ["parse-file", str(fixture_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["five_hour"]["utilization"] == 38
    assert payload["seven_day"]["utilization"] == 20


def test_parse_file_drift(runner: CliRunner, tmp_path: Path) -> None:
    """Garbage input fails with a drift JSON payload + exit 1."""
    bogus = tmp_path / "bogus.cap"
    bogus.write_bytes(b"this is not a /usage capture\n")
    result = runner.invoke(app, ["parse-file", str(bogus)])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "format_drift"


def test_parse_file_nonexistent(runner: CliRunner, tmp_path: Path) -> None:
    """Typer's exists=True guard rejects missing paths with exit 2."""
    result = runner.invoke(app, ["parse-file", str(tmp_path / "does_not_exist.cap")])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# `render` — default; goes through capture_mod.capture (mocked)
# ---------------------------------------------------------------------------


def test_render_happy_path(runner: CliRunner, fixture_bytes: bytes, tmp_path: Path) -> None:
    """Mocked capture returns fixture bytes; output should show the bar
    and the percentages from the parsed fixture."""
    with patch(
        "claude_task_runner.cli.usage_cmd.capture_mod.capture",
        return_value=(fixture_bytes, tmp_path / "fake.cap"),
    ):
        result = runner.invoke(app, ["render"])
    assert result.exit_code == 0
    assert "Claude Code Usage" in result.stdout
    assert "5-hour session" in result.stdout
    assert "7-day weekly" in result.stdout
    assert "38%" in result.stdout
    assert "20%" in result.stdout


# The "no subcommand → render" path uses ctx.invoke(render) inside the
# root callback. Typer's CliRunner doesn't propagate the Context to
# `render`'s `ctx` arg cleanly in this case (the operator-facing CLI
# works fine because Typer's real dispatch goes through a different
# code path). We rely on the explicit ``usage render`` invocation
# tested above to exercise the same logic.


def test_render_spawn_error(runner: CliRunner) -> None:
    with patch(
        "claude_task_runner.cli.usage_cmd.capture_mod.capture",
        side_effect=UsageCaptureSpawnError("claude binary not found in PATH"),
    ):
        result = runner.invoke(app, ["render"])
    assert result.exit_code == 3
    assert "spawn error" in result.stdout


def test_render_capture_timeout(runner: CliRunner) -> None:
    with patch(
        "claude_task_runner.cli.usage_cmd.capture_mod.capture",
        side_effect=UsageCaptureTimeout("TUI did not become ready"),
    ):
        result = runner.invoke(app, ["render"])
    assert result.exit_code == 2
    assert "capture timeout" in result.stdout


def test_render_format_drift_via_parser(runner: CliRunner, tmp_path: Path) -> None:
    """Capture returns bytes the parser can't make sense of."""
    with patch(
        "claude_task_runner.cli.usage_cmd.capture_mod.capture",
        return_value=(b"<random non-usage TUI output>", tmp_path / "fake.cap"),
    ):
        result = runner.invoke(app, ["render"])
    assert result.exit_code == 1
    assert "format drift" in result.stdout


# ---------------------------------------------------------------------------
# `json` — machine-readable
# ---------------------------------------------------------------------------


def test_json_happy_path(runner: CliRunner, fixture_bytes: bytes, tmp_path: Path) -> None:
    with patch(
        "claude_task_runner.cli.usage_cmd.capture_mod.capture",
        return_value=(fixture_bytes, tmp_path / "fake.cap"),
    ):
        result = runner.invoke(app, ["json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["five_hour"]["utilization"] == 38
    assert payload["seven_day"]["utilization"] == 20
    assert "resets_at_raw" in payload["five_hour"]


def test_json_capture_error(runner: CliRunner) -> None:
    with patch(
        "claude_task_runner.cli.usage_cmd.capture_mod.capture",
        side_effect=UsageCaptureTimeout("timed out"),
    ):
        result = runner.invoke(app, ["json"])
    assert result.exit_code == 2  # EXIT_CAPTURE_TIMEOUT


def test_json_format_drift(runner: CliRunner, tmp_path: Path) -> None:
    with patch(
        "claude_task_runner.cli.usage_cmd.capture_mod.capture",
        return_value=(b"not a usage panel", tmp_path / "fake.cap"),
    ):
        result = runner.invoke(app, ["json"])
    assert result.exit_code == 1  # EXIT_PARSE_DRIFT


# ---------------------------------------------------------------------------
# `healthcheck`
# ---------------------------------------------------------------------------


def test_healthcheck_pass(runner: CliRunner, fixture_bytes: bytes, tmp_path: Path) -> None:
    with patch(
        "claude_task_runner.cli.usage_cmd.capture_mod.capture",
        return_value=(fixture_bytes, tmp_path / "fake.cap"),
    ):
        result = runner.invoke(app, ["healthcheck"])
    assert result.exit_code == 0
    assert "PASS" in result.stdout


def test_healthcheck_unexpected_exception(runner: CliRunner) -> None:
    """Catches a bare Exception (not one of the drift/timeout types)."""
    with patch(
        "claude_task_runner.cli.usage_cmd.capture_mod.capture",
        side_effect=RuntimeError("disk full"),
    ):
        result = runner.invoke(app, ["healthcheck"])
    assert result.exit_code == 4  # EXIT_UNEXPECTED
    assert "FAIL" in result.stdout
    assert "unexpected" in result.stdout


def test_healthcheck_spawn_error(runner: CliRunner) -> None:
    with patch(
        "claude_task_runner.cli.usage_cmd.capture_mod.capture",
        side_effect=UsageCaptureSpawnError("not found"),
    ):
        result = runner.invoke(app, ["healthcheck"])
    assert result.exit_code == 3


def test_healthcheck_timeout(runner: CliRunner) -> None:
    with patch(
        "claude_task_runner.cli.usage_cmd.capture_mod.capture",
        side_effect=UsageCaptureTimeout("timeout"),
    ):
        result = runner.invoke(app, ["healthcheck"])
    assert result.exit_code == 2


def test_healthcheck_drift(runner: CliRunner, tmp_path: Path) -> None:
    with patch(
        "claude_task_runner.cli.usage_cmd.capture_mod.capture",
        return_value=(b"not a usage panel", tmp_path / "fake.cap"),
    ):
        result = runner.invoke(app, ["healthcheck"])
    assert result.exit_code == 1


def test_healthcheck_warn_on_unparseable_resets_at(
    runner: CliRunner, fixture_bytes: bytes, tmp_path: Path
) -> None:
    """When resets_at can't be parsed (None), report a WARN rather than PASS."""
    # Patch BOTH capture and parser; provide a reading whose resets_at is None.
    from datetime import UTC, datetime

    from claude_task_runner.usage.models import UsageReading, WindowReading

    fake_reading = UsageReading(
        captured_at=datetime.now(UTC),
        five_hour=WindowReading(
            utilization_pct=18,
            resets_at_raw="some unparseable string",
            resets_at=None,
        ),
        seven_day=WindowReading(
            utilization_pct=42,
            resets_at_raw="another unparseable string",
            resets_at=None,
        ),
    )
    with (
        patch(
            "claude_task_runner.cli.usage_cmd.capture_mod.capture",
            return_value=(fixture_bytes, tmp_path / "fake.cap"),
        ),
        patch(
            "claude_task_runner.cli.usage_cmd.parser_mod.parse",
            return_value=fake_reading,
        ),
    ):
        result = runner.invoke(app, ["healthcheck"])
    assert result.exit_code == 0
    assert "WARN" in result.stdout


# ---------------------------------------------------------------------------
# `capture` — capture only, save raw bytes
# ---------------------------------------------------------------------------


def test_capture_only_writes_file(runner: CliRunner, fixture_bytes: bytes, tmp_path: Path) -> None:
    save_path = tmp_path / "out.cap"
    with patch(
        "claude_task_runner.cli.usage_cmd.capture_mod.capture",
        return_value=(fixture_bytes, tmp_path / "fake.cap"),
    ):
        result = runner.invoke(app, ["capture", "--save", str(save_path)])
    assert result.exit_code == 0
    assert save_path.read_bytes() == fixture_bytes
    assert "saved" in result.stdout
    assert str(len(fixture_bytes)) in result.stdout


def test_capture_only_spawn_error(runner: CliRunner, tmp_path: Path) -> None:
    save_path = tmp_path / "out.cap"
    with patch(
        "claude_task_runner.cli.usage_cmd.capture_mod.capture",
        side_effect=UsageCaptureSpawnError("nope"),
    ):
        result = runner.invoke(app, ["capture", "--save", str(save_path)])
    assert result.exit_code == 3


def test_capture_only_timeout(runner: CliRunner, tmp_path: Path) -> None:
    save_path = tmp_path / "out.cap"
    with patch(
        "claude_task_runner.cli.usage_cmd.capture_mod.capture",
        side_effect=UsageCaptureTimeout("timeout"),
    ):
        result = runner.invoke(app, ["capture", "--save", str(save_path)])
    assert result.exit_code == 2


def test_capture_only_creates_parent_dir(
    runner: CliRunner, fixture_bytes: bytes, tmp_path: Path
) -> None:
    """If --save points at a nested path whose dir doesn't exist, the
    command must create it before writing."""
    save_path = tmp_path / "nested" / "subdir" / "out.cap"
    assert not save_path.parent.exists()
    with patch(
        "claude_task_runner.cli.usage_cmd.capture_mod.capture",
        return_value=(fixture_bytes, tmp_path / "fake.cap"),
    ):
        result = runner.invoke(app, ["capture", "--save", str(save_path)])
    assert result.exit_code == 0
    assert save_path.exists()


# ---------------------------------------------------------------------------
# `whoami`
# ---------------------------------------------------------------------------


def _make_whoami_snap(**kw):
    """Build a IdentitySnapshot for mocking. We import the real class so any
    field-rename or schema change here shows up as a clean test failure
    rather than a runtime AttributeError."""
    from claude_task_runner.usage.whoami import IdentitySnapshot

    base = {
        "config_dir": "/home/bill/.claude_personal",
        "subscription_type": "max20",
        "rate_limit_tier": "tier3",
        "welcome_label": "Personal",
        "scopes": ("user:inference",),
    }
    base.update(kw)
    return IdentitySnapshot(**base)


def test_whoami_quick_skips_capture(runner: CliRunner) -> None:
    """--quick must NOT call capture_mod.capture."""
    with (
        patch(
            "claude_task_runner.cli.usage_cmd.whoami_mod.from_credentials_only",
            return_value=_make_whoami_snap(),
        ),
        patch(
            "claude_task_runner.cli.usage_cmd.capture_mod.capture",
        ) as mock_capture,
    ):
        result = runner.invoke(app, ["whoami", "--quick"])
    assert result.exit_code == 0
    mock_capture.assert_not_called()
    assert "Personal" in result.stdout


def test_whoami_with_capture(runner: CliRunner, fixture_bytes: bytes, tmp_path: Path) -> None:
    with (
        patch(
            "claude_task_runner.cli.usage_cmd.capture_mod.capture",
            return_value=(fixture_bytes, tmp_path / "fake.cap"),
        ),
        patch(
            "claude_task_runner.cli.usage_cmd.whoami_mod.from_capture",
            return_value=_make_whoami_snap(welcome_label="Acme Pharma"),
        ),
    ):
        result = runner.invoke(app, ["whoami"])
    assert result.exit_code == 0
    assert "Acme Pharma" in result.stdout


def test_whoami_capture_timeout_falls_back(runner: CliRunner) -> None:
    """Capture timeout in non-quick mode must FALL BACK to creds-only,
    not exit non-zero."""
    with (
        patch(
            "claude_task_runner.cli.usage_cmd.capture_mod.capture",
            side_effect=UsageCaptureTimeout("timeout"),
        ),
        patch(
            "claude_task_runner.cli.usage_cmd.whoami_mod.from_credentials_only",
            return_value=_make_whoami_snap(),
        ),
    ):
        result = runner.invoke(app, ["whoami"])
    assert result.exit_code == 0
    assert "Falling back to credentials-only" in result.stdout


def test_whoami_spawn_error_aborts(runner: CliRunner) -> None:
    """Spawn errors (binary not found) are fatal — exit 3."""
    with patch(
        "claude_task_runner.cli.usage_cmd.capture_mod.capture",
        side_effect=UsageCaptureSpawnError("binary not found"),
    ):
        result = runner.invoke(app, ["whoami"])
    assert result.exit_code == 3


def test_whoami_account_classes(runner: CliRunner) -> None:
    """Three rendering branches: team, personal, unknown."""
    from claude_task_runner.usage.whoami import IdentitySnapshot

    # Team / Enterprise: subscription_type == 'team' or 'enterprise'
    snap_team = IdentitySnapshot(
        config_dir="",
        subscription_type="team",
        rate_limit_tier="",
        welcome_label="",
        scopes=("organization:admin",),
    )
    # Personal: subscription_type starts with 'pro' or 'max'
    snap_personal = IdentitySnapshot(
        config_dir="",
        subscription_type="max20",
        rate_limit_tier="",
        welcome_label="",
        scopes=("user:inference",),
    )
    # Unknown: neither pattern
    snap_unknown = IdentitySnapshot(
        config_dir="",
        subscription_type="",
        rate_limit_tier="",
        welcome_label="",
        scopes=(),
    )
    for snap, expect in [
        (snap_team, "Team / Enterprise"),
        (snap_personal, "Personal (Pro / Max)"),
        (snap_unknown, "unknown"),
    ]:
        with patch(
            "claude_task_runner.cli.usage_cmd.whoami_mod.from_credentials_only",
            return_value=snap,
        ):
            result = runner.invoke(app, ["whoami", "--quick"])
        assert result.exit_code == 0, f"snap={snap}"
        assert expect in result.stdout, f"snap={snap}, stdout={result.stdout}"
