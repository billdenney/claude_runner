"""Tests for the ``claude-task-runner account`` CLI subcommands.

Covers:
* ``account list``: reports resolved per-account policy + observed
  state (max_concurrency, bands, 5h util, in-flight count). Defaults
  apply when ``runner-account.toml`` is absent.
* ``account pause`` / ``account resume``: create / remove the account's
  pause marker and never write ``supervisor.json``, which a running
  supervisor rewrites from memory every tick; idempotent when already in
  the requested state; refuses unknown account names.
* ``account list`` reports ``paused`` from the markers, not from the
  snapshot's copy of the flag.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from claude_task_runner.cli import app
from claude_task_runner.supervisor import account_pause
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

    def test_auto_discovers_queue_config_when_config_flag_omitted(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Regression: ``account list --queue <dir>`` without ``--config`` must
        pick up ``<dir>/claude_runner.toml`` instead of silently falling back
        to package defaults.

        Bug history: pre-fix, the operator-friendly invocation
        ``claude-task-runner account list --queue <q> --json`` returned only
        the synthesised ``"default"`` placeholder account, ignoring the real
        ``[[accounts]]`` declarations sitting at ``<q>/claude_runner.toml``.
        Operators (and the runner-status skill) had to know to pass
        ``--config <q>/claude_runner.toml`` explicitly to see real state —
        easy to forget; the supervisor itself always passes ``--config``.
        """
        cfg_dir_personal = tmp_path / "personal"
        cfg_dir_personal.mkdir()
        cfg_dir_work = tmp_path / "work"
        cfg_dir_work.mkdir()
        queue_dir = tmp_path / "q"
        queue_dir.mkdir()
        # Place the per-queue config AT the conventional path inside queue_dir,
        # not at tmp_path. Auto-discovery should find it from --queue alone.
        _write_queue_config(
            queue_dir,
            accounts=[
                ("personal", str(cfg_dir_personal), None),
                ("work", str(cfg_dir_work), None),
            ],
        )
        result = runner.invoke(
            app,
            [
                "account",
                "list",
                "--queue",
                str(queue_dir),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        rows = json.loads(result.stdout)["accounts"]
        names = [r["name"] for r in rows]
        assert names == ["personal", "work"], (
            "auto-discovery of <queue>/claude_runner.toml regressed; "
            f"saw {names!r} instead of ['personal', 'work']"
        )

    def test_explicit_config_overrides_auto_discovery(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """When ``--config`` is passed explicitly, honour it even if
        ``<queue>/claude_runner.toml`` exists. Operators occasionally point
        at a sibling TOML for testing / dry-runs."""
        cfg_dir_inqueue = tmp_path / "inqueue_acct"
        cfg_dir_inqueue.mkdir()
        cfg_dir_explicit = tmp_path / "explicit_acct"
        cfg_dir_explicit.mkdir()
        queue_dir = tmp_path / "q"
        queue_dir.mkdir()
        explicit_dir = tmp_path / "explicit"
        explicit_dir.mkdir()
        # A different config at the auto-discovery path (would win if we
        # auto-discovered)...
        _write_queue_config(queue_dir, accounts=[("in_queue", str(cfg_dir_inqueue), None)])
        # ...and an explicit one elsewhere — the explicit one should win.
        explicit_cfg = _write_queue_config(
            explicit_dir, accounts=[("explicit", str(cfg_dir_explicit), None)]
        )
        result = runner.invoke(
            app,
            [
                "account",
                "list",
                "--config",
                str(explicit_cfg),
                "--queue",
                str(queue_dir),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        rows = json.loads(result.stdout)["accounts"]
        names = [r["name"] for r in rows]
        assert names == ["explicit"], (
            f"explicit --config should win over auto-discovery; saw {names!r}"
        )

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
        # Seed a snapshot with util for one account; pause the other.
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
        account_pause.set_paused(queue_dir, "work", paused=True)
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

    def test_dispatch_cap_is_max_lowered_to_the_throttle_target(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """personal (max 5) is slowing down with a target of 2; work (max 1)
        has no decision yet; ghost has no state at all."""
        cfg_dir = tmp_path / "personal"
        cfg_dir.mkdir()
        (cfg_dir / "runner-account.toml").write_text(
            "[concurrency]\nmax_concurrency = 5\n", encoding="utf-8"
        )
        queue_dir = tmp_path / "q"
        queue_dir.mkdir()
        config = _write_queue_config(
            tmp_path,
            accounts=[("personal", str(cfg_dir), None), ("work", "", None), ("ghost", "", None)],
        )
        accounts = {
            "personal": AccountState(
                state=SupervisorState.SLOWING_DOWN,
                since=datetime(2026, 5, 21, tzinfo=UTC),
                last_5h_util_pct=55,
                target_concurrency=2,
            ),
            "work": AccountState(
                state=SupervisorState.IDLE, since=datetime(2026, 5, 21, tzinfo=UTC)
            ),
        }
        in_flight = [
            InFlightRecord(
                task_id=f"t{i}", account="personal", started_at=datetime(2026, 5, 21, tzinfo=UTC)
            )
            for i in range(2)
        ]
        _seed_snapshot(queue_dir, accounts=accounts, in_flight=in_flight)
        args = ["account", "list", "--config", str(config), "--queue", str(queue_dir)]

        result = runner.invoke(app, [*args, "--json"])
        assert result.exit_code == 0, result.output
        by_name = {r["name"]: r for r in json.loads(result.stdout)["accounts"]}
        assert by_name["personal"]["target_concurrency"] == 2
        assert by_name["personal"]["dispatch_cap"] == 2
        assert by_name["work"]["target_concurrency"] is None
        assert by_name["work"]["dispatch_cap"] == 1
        # No state row: dispatch declines it ("no state"), so its cap is 0.
        assert by_name["ghost"]["target_concurrency"] is None
        assert by_name["ghost"]["dispatch_cap"] == 0
        assert by_name["ghost"]["last_reading_at"] is None

        human = runner.invoke(app, args)
        assert human.exit_code == 0, human.output
        assert "in_flight=2/2" in human.stdout
        assert "in_flight=0/1" in human.stdout


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

    def test_pause_writes_a_marker_and_no_supervisor_json(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """With no supervisor.json yet, pause creates only the marker."""
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
        assert account_pause.paused_names(queue_dir) == frozenset({"personal"})
        assert not persist_mod.supervisor_state_path(queue_dir).exists()

    def test_pause_leaves_supervisor_json_unchanged(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """The supervisor owns supervisor.json; pause must not write it,
        or the supervisor's next tick overwrites the flag from memory."""
        queue_dir = tmp_path / "q"
        queue_dir.mkdir()
        config = _write_queue_config(
            tmp_path, accounts=[("personal", "", None), ("work", "", None)]
        )
        state_path = _seed_snapshot(
            queue_dir,
            accounts={
                "work": AccountState(
                    state=SupervisorState.DISPATCHING, since=datetime(2026, 5, 21, tzinfo=UTC)
                )
            },
        )
        before = state_path.read_bytes()
        result = runner.invoke(
            app,
            ["account", "pause", "personal", "--config", str(config), "--queue", str(queue_dir)],
        )
        assert result.exit_code == 0, result.output
        assert state_path.read_bytes() == before
        assert account_pause.paused_names(queue_dir) == frozenset({"personal"})

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
        assert account_pause.paused_names(queue_dir) == frozenset({"personal"})

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
        assert account_pause.paused_names(queue_dir) == frozenset()

    def test_pause_already_paused_is_idempotent(self, runner: CliRunner, tmp_path: Path) -> None:
        queue_dir = tmp_path / "q"
        queue_dir.mkdir()
        config = _write_queue_config(tmp_path, accounts=[("personal", "", None)])
        account_pause.set_paused(queue_dir, "personal", paused=True)
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

    def test_list_reports_paused_from_the_marker_not_the_snapshot(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Between a pause and the supervisor's next tick, supervisor.json
        still has the old flag; ``account list`` must show the pause."""
        queue_dir = tmp_path / "q"
        queue_dir.mkdir()
        config = _write_queue_config(
            tmp_path, accounts=[("personal", "", None), ("work", "", None)]
        )
        since = datetime(2026, 5, 21, tzinfo=UTC)
        _seed_snapshot(
            queue_dir,
            accounts={
                "personal": AccountState(state=SupervisorState.IDLE, since=since, paused=False),
                "work": AccountState(state=SupervisorState.IDLE, since=since, paused=True),
            },
        )
        account_pause.set_paused(queue_dir, "personal", paused=True)
        result = runner.invoke(
            app,
            ["account", "list", "--config", str(config), "--queue", str(queue_dir), "--json"],
        )
        assert result.exit_code == 0, result.output
        rows = {r["name"]: r for r in json.loads(result.stdout)["accounts"]}
        assert rows["personal"]["paused"] is True
        assert rows["work"]["paused"] is False

    def test_resume_removes_the_marker_of_an_account_no_longer_configured(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """A leftover marker would pause the account again if it were re-added."""
        queue_dir = tmp_path / "q"
        queue_dir.mkdir()
        config = _write_queue_config(tmp_path, accounts=[("personal", "", None)])
        account_pause.set_paused(queue_dir, "work", paused=True)
        result = runner.invoke(
            app,
            [
                "account",
                "resume",
                "work",
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
        assert "not in [[accounts]]" in out["message"]
        assert account_pause.paused_names(queue_dir) == frozenset()

    def test_list_reports_an_unreadable_marker_directory_in_one_line(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        queue_dir = tmp_path / "q"
        queue_dir.mkdir()
        config = _write_queue_config(tmp_path, accounts=[("personal", "", None)])
        account_pause.pause_dir(queue_dir).parent.mkdir(parents=True)
        account_pause.pause_dir(queue_dir).write_text("not a directory", encoding="utf-8")
        args = ["account", "list", "--config", str(config), "--queue", str(queue_dir)]
        human = runner.invoke(app, args)
        assert human.exit_code == 2
        assert "cannot read the account pause markers" in human.stdout
        assert "Traceback" not in human.output
        as_json = runner.invoke(app, [*args, "--json"])
        assert as_json.exit_code == 2
        assert "cannot read the account pause markers" in json.loads(as_json.stdout)["error"]


class TestAccountListHumanReadable:
    """Cover the non-JSON render loop in ``account list``."""

    def test_no_snapshot_renders_defaults(self, runner: CliRunner, tmp_path: Path) -> None:
        cfg_dir = tmp_path / "personal"
        cfg_dir.mkdir()
        queue_dir = tmp_path / "q"
        queue_dir.mkdir()
        config = _write_queue_config(tmp_path, accounts=[("personal", str(cfg_dir), None)])
        result = runner.invoke(
            app,
            ["account", "list", "--config", str(config), "--queue", str(queue_dir)],
        )
        assert result.exit_code == 0, result.output
        assert "personal" in result.stdout
        assert "max_concurrency:" in result.stdout
        assert "dispatch_pct:" in result.stdout
        # No snapshot ⇒ state should render as "—".
        assert "—" in result.stdout

    def test_with_snapshot_renders_state_and_paused_marker(
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
        _seed_snapshot(queue_dir, accounts=accounts, in_flight=[])
        account_pause.set_paused(queue_dir, "work", paused=True)
        result = runner.invoke(
            app,
            ["account", "list", "--config", str(config), "--queue", str(queue_dir)],
        )
        assert result.exit_code == 0, result.output
        assert "personal" in result.stdout
        assert "dispatching" in result.stdout
        assert "42%" in result.stdout
        assert "work (paused)" in result.stdout
        assert "personal (paused)" not in result.stdout
        # linux_user line printed for the work account.
        assert "bw" in result.stdout


class TestAccountPauseResumeHumanReadable:
    """Cover the non-JSON output paths of ``account pause`` / ``account resume``."""

    def test_pause_prints_changed(self, runner: CliRunner, tmp_path: Path) -> None:
        cfg_dir = tmp_path / "personal"
        cfg_dir.mkdir()
        queue_dir = tmp_path / "q"
        queue_dir.mkdir()
        config = _write_queue_config(tmp_path, accounts=[("personal", str(cfg_dir), None)])
        result = runner.invoke(
            app,
            ["account", "pause", "personal", "--config", str(config), "--queue", str(queue_dir)],
        )
        assert result.exit_code == 0, result.output
        assert "paused=True" in result.stdout

    def test_resume_already_resumed_is_idempotent_dim(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        cfg_dir = tmp_path / "personal"
        cfg_dir.mkdir()
        queue_dir = tmp_path / "q"
        queue_dir.mkdir()
        config = _write_queue_config(tmp_path, accounts=[("personal", str(cfg_dir), None)])
        _seed_snapshot(
            queue_dir,
            accounts={
                "personal": AccountState(
                    state=SupervisorState.IDLE,
                    since=datetime(2026, 5, 21, tzinfo=UTC),
                    paused=False,
                ),
            },
        )
        result = runner.invoke(
            app,
            ["account", "resume", "personal", "--config", str(config), "--queue", str(queue_dir)],
        )
        assert result.exit_code == 0, result.output
        assert "already paused=False" in result.stdout
