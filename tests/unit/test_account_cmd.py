"""Tests for the ``claude-task-runner account`` CLI subcommands.

Covers:
* ``account list``: reports resolved per-account policy + observed
  state (max_concurrency, bands, 5h util, in-flight count). Defaults
  apply when ``runner-account.toml`` is absent.
* ``account pause`` / ``account resume``: mutate the ``paused`` flag
  in ``supervisor.json``; idempotent when already in the requested
  state; refuses unknown account names.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from claude_task_runner.cli import app
from claude_task_runner.supervisor import persistence as persist_mod
from claude_task_runner.supervisor.states import (
    AccountState,
    InFlightRecord,
    SupervisorSnapshot,
    SupervisorState,
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _write_queue_config(
    dir_: Path,
    *,
    accounts: list[tuple[str, str, str | None]],
) -> Path:
    """Write a claude_runner.toml in ``dir_`` with the given (name, config_dir, linux_user) tuples."""
    lines: list[str] = []
    for name, config_dir, linux_user in accounts:
        lines.append("[[accounts]]")
        lines.append(f'name = "{name}"')
        lines.append(f'config_dir = "{config_dir}"')
        if linux_user is not None:
            lines.append(f'linux_user = "{linux_user}"')
        lines.append("")
    path = dir_ / "claude_runner.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _seed_snapshot(
    queue_dir: Path,
    *,
    accounts: dict[str, AccountState],
    in_flight: list[InFlightRecord] | None = None,
) -> Path:
    """Write a v3 supervisor.json under ``queue_dir/.claude_task_runner``."""
    runtime = queue_dir / ".claude_task_runner"
    runtime.mkdir(parents=True, exist_ok=True)
    snap = SupervisorSnapshot(
        state=SupervisorState.IDLE,
        since=datetime(2026, 5, 21, tzinfo=UTC),
        accounts=accounts,
        in_flight=in_flight or [],
    )
    path = persist_mod.supervisor_state_path(queue_dir)
    persist_mod.write_atomic(snap, path)
    return path


class TestAccountList:
    def test_no_supervisor_snapshot_defaults_only(self, runner: CliRunner, tmp_path: Path) -> None:
        cfg_dir = tmp_path / "personal"
        cfg_dir.mkdir()
        queue_dir = tmp_path / "q"
        queue_dir.mkdir()
        config = _write_queue_config(tmp_path, accounts=[("personal", str(cfg_dir), None)])
        result = runner.invoke(
            app,
            [
                "account",
                "list",
                "--config",
                str(config),
                "--queue",
                str(queue_dir),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        rows = payload["accounts"]
        assert len(rows) == 1
        assert rows[0]["name"] == "personal"
        assert rows[0]["max_concurrency"] == 1  # default
        assert rows[0]["state"] is None  # no snapshot
        assert rows[0]["in_flight_count"] == 0

    def test_per_account_file_overrides_defaults(self, runner: CliRunner, tmp_path: Path) -> None:
        cfg_dir = tmp_path / "personal"
        cfg_dir.mkdir()
        (cfg_dir / "runner-account.toml").write_text(
            "[concurrency]\nmax_concurrency = 5\n", encoding="utf-8"
        )
        queue_dir = tmp_path / "q"
        queue_dir.mkdir()
        config = _write_queue_config(tmp_path, accounts=[("personal", str(cfg_dir), None)])
        result = runner.invoke(
            app,
            [
                "account",
                "list",
                "--config",
                str(config),
                "--queue",
                str(queue_dir),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        rows = json.loads(result.stdout)["accounts"]
        assert rows[0]["max_concurrency"] == 5

    def test_lists_state_and_in_flight_from_snapshot(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        cfg_dir = tmp_path / "personal"
        cfg_dir.mkdir()
        queue_dir = tmp_path / "q"
        queue_dir.mkdir()
        config = _write_queue_config(
            tmp_path,
            accounts=[("personal", str(cfg_dir), None), ("work", "", "bw")],
        )
        # Seed a snapshot with one account paused and one with util.
        accounts = {
            "personal": AccountState(
                state=SupervisorState.DISPATCHING,
                since=datetime(2026, 5, 21, tzinfo=UTC),
                last_5h_util_pct=42,
                last_weekly_util_pct=18,
                paused=False,
            ),
            "work": AccountState(
                state=SupervisorState.IDLE,
                since=datetime(2026, 5, 21, tzinfo=UTC),
                paused=True,
            ),
        }
        in_flight = [
            InFlightRecord(
                task_id="t1",
                account="personal",
                started_at=datetime(2026, 5, 21, tzinfo=UTC),
            ),
            InFlightRecord(
                task_id="t2",
                account="personal",
                started_at=datetime(2026, 5, 21, tzinfo=UTC),
            ),
        ]
        _seed_snapshot(queue_dir, accounts=accounts, in_flight=in_flight)
        result = runner.invoke(
            app,
            [
                "account",
                "list",
                "--config",
                str(config),
                "--queue",
                str(queue_dir),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        rows = json.loads(result.stdout)["accounts"]
        by_name = {r["name"]: r for r in rows}
        assert by_name["personal"]["state"] == "dispatching"
        assert by_name["personal"]["last_5h_util_pct"] == 42
        assert by_name["personal"]["in_flight_count"] == 2
        assert by_name["work"]["paused"] is True


class TestAccountPauseResume:
    def test_pause_unknown_account_errors(self, runner: CliRunner, tmp_path: Path) -> None:
        queue_dir = tmp_path / "q"
        queue_dir.mkdir()
        config = _write_queue_config(tmp_path, accounts=[("personal", "", None)])
        result = runner.invoke(
            app,
            [
                "account",
                "pause",
                "ghost",
                "--config",
                str(config),
                "--queue",
                str(queue_dir),
            ],
        )
        assert result.exit_code != 0
        assert "ghost" in (result.output + (result.stderr or ""))

    def test_pause_seeds_snapshot_when_missing(self, runner: CliRunner, tmp_path: Path) -> None:
        """No supervisor.json yet → pause seeds one with the flag set."""
        queue_dir = tmp_path / "q"
        queue_dir.mkdir()
        config = _write_queue_config(tmp_path, accounts=[("personal", "", None)])
        result = runner.invoke(
            app,
            [
                "account",
                "pause",
                "personal",
                "--config",
                str(config),
                "--queue",
                str(queue_dir),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        out = json.loads(result.stdout)
        assert out["changed"] is True
        snap = persist_mod.load(persist_mod.supervisor_state_path(queue_dir))
        assert snap is not None
        assert snap.accounts["personal"].paused is True

    def test_pause_then_resume_flips_flag(self, runner: CliRunner, tmp_path: Path) -> None:
        queue_dir = tmp_path / "q"
        queue_dir.mkdir()
        config = _write_queue_config(tmp_path, accounts=[("personal", "", None)])
        _seed_snapshot(
            queue_dir,
            accounts={
                "personal": AccountState(
                    state=SupervisorState.IDLE,
                    since=datetime(2026, 5, 21, tzinfo=UTC),
                ),
            },
        )

        r1 = runner.invoke(
            app,
            [
                "account",
                "pause",
                "personal",
                "--config",
                str(config),
                "--queue",
                str(queue_dir),
                "--json",
            ],
        )
        assert r1.exit_code == 0, r1.output
        assert json.loads(r1.stdout)["changed"] is True

        r2 = runner.invoke(
            app,
            [
                "account",
                "resume",
                "personal",
                "--config",
                str(config),
                "--queue",
                str(queue_dir),
                "--json",
            ],
        )
        assert r2.exit_code == 0, r2.output
        assert json.loads(r2.stdout)["changed"] is True

        snap = persist_mod.load(persist_mod.supervisor_state_path(queue_dir))
        assert snap is not None
        assert snap.accounts["personal"].paused is False

    def test_pause_already_paused_is_idempotent(self, runner: CliRunner, tmp_path: Path) -> None:
        queue_dir = tmp_path / "q"
        queue_dir.mkdir()
        config = _write_queue_config(tmp_path, accounts=[("personal", "", None)])
        _seed_snapshot(
            queue_dir,
            accounts={
                "personal": AccountState(
                    state=SupervisorState.IDLE,
                    since=datetime(2026, 5, 21, tzinfo=UTC),
                    paused=True,
                ),
            },
        )
        result = runner.invoke(
            app,
            [
                "account",
                "pause",
                "personal",
                "--config",
                str(config),
                "--queue",
                str(queue_dir),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        out = json.loads(result.stdout)
        assert out["changed"] is False
        assert "already paused" in out["message"]
