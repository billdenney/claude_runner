"""``claude-task-runner worktree reclaim`` (ADR-0034), end to end on a fixture repo.

The reclaim logic itself is pinned in ``test_worktree_reclaim.py``; these
tests cover what the CLI adds: dry run by default, per-queue config
discovery, the supervisor's persisted in-flight set, rendering, and exit
codes (0 clean, 1 something failed, 2 could not run).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from claude_task_runner.cli import app
from claude_task_runner.queue.store import todo_dir
from claude_task_runner.supervisor import persistence as persist_mod
from claude_task_runner.supervisor.states import InFlightRecord

from ._git_world import World, git


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World.create(tmp_path, monkeypatch)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def reclaim(runner: CliRunner, world: World, *args: str) -> object:
    return runner.invoke(app, ["worktree", "reclaim", "--queue", str(world.queue), *args])


def write_supervisor_snapshot(world: World, *, in_flight: list[str]) -> Path:
    now = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    snapshot = persist_mod.initial_snapshot(since=now).model_copy(
        update={
            "in_flight": [
                InFlightRecord(task_id=task_id, account="default", started_at=now)
                for task_id in in_flight
            ]
        }
    )
    path = persist_mod.supervisor_state_path(world.queue)
    persist_mod.write_atomic(snapshot, path)
    return path


class TestModes:
    def test_dry_run_is_the_default(self, runner: CliRunner, world: World) -> None:
        wt = world.add_task("t-merged")
        result = runner.invoke(app, ["worktree", "reclaim", "--queue", str(world.queue)])
        assert result.exit_code == 0, result.output
        lines = result.stdout.splitlines()
        assert lines[0].split() == ["would", "t-merged"]
        assert lines[-1] == (
            "dry run: 1 worktree(s) seen; 1 reclaimable; kept 0 (status), 0 (unmerged), "
            "0 (uncommitted work), 0 (other); 0 failed"
        )
        assert (wt / ".git").is_file()
        assert world.branch_exists("claude/t-merged")

    def test_apply_removes(self, runner: CliRunner, world: World) -> None:
        wt = world.add_task("t-merged")
        result = reclaim(runner, world, "--apply")
        assert result.exit_code == 0, result.output
        assert result.stdout.splitlines()[0].split() == ["gone", "t-merged"]
        assert result.stdout.splitlines()[-1].startswith("applied: 1 worktree(s) seen; 1 reclaimed")
        assert not wt.exists()
        assert not world.branch_exists("claude/t-merged")

    def test_reachable_from_the_root_cli(self, runner: CliRunner, world: World) -> None:
        world.add_task("t-merged")
        result = runner.invoke(app, ["worktree", "reclaim", "--queue", str(world.queue)])
        assert result.exit_code == 0, result.output
        assert "1 reclaimable" in result.stdout

    def test_keep_and_force_lines_explain_themselves(self, runner: CliRunner, world: World) -> None:
        world.add_task("t-open", merge=False)
        world.add_task("t-run", status="running")
        wt = world.add_task("t-problems")
        (wt / "tests" / "testthat" / "_problems").mkdir()
        (wt / "tests" / "testthat" / "_problems" / "x.md").write_text("snapshot\n")
        result = reclaim(runner, world)
        assert result.exit_code == 0, result.output
        by_task = {line.split()[1]: line for line in result.stdout.splitlines()[:3]}
        assert by_task["t-open"].split(None, 2)[2] == (
            "unmerged: claude/t-open is not an ancestor of origin/main"
        )
        assert by_task["t-run"].split(None, 2)[2] == "status: status=running"
        assert by_task["t-problems"].split(None, 2)[2] == (
            "--force discards 1 untracked path(s): tests/testthat/_problems/"
        )

    def test_limit(self, runner: CliRunner, world: World) -> None:
        world.add_task("t-0")
        world.add_task("t-1")
        result = reclaim(runner, world, "--apply", "--limit", "1")
        assert result.exit_code == 0, result.output
        assert [line.split()[0] for line in result.stdout.splitlines()[:2]] == ["gone", "keep"]
        assert "limit: this pass already reached its limit of 1" in result.stdout

    def test_limit_below_one_is_a_usage_error(self, runner: CliRunner, world: World) -> None:
        result = reclaim(runner, world, "--limit", "0")
        assert result.exit_code == 2


class TestJson:
    def test_payload(self, runner: CliRunner, world: World) -> None:
        world.add_task("t-merged")
        result = reclaim(runner, world, "--json")
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert payload["applied"] is False
        assert payload["counts"]["would_reclaim"] == 1
        assert [r["task_id"] for r in payload["results"]] == ["t-merged"]

    def test_could_not_run_is_json_too(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(app, ["worktree", "reclaim", "--queue", str(tmp_path), "--json"])
        assert result.exit_code == 2
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["error"].endswith("has no todo/ subdirectory")


class TestConfig:
    def test_per_queue_toml_is_discovered(self, runner: CliRunner, world: World) -> None:
        wt = world.add_task("t-problems")
        (wt / "tests" / "testthat" / "_problems").mkdir()
        (wt / "tests" / "testthat" / "_problems" / "x.md").write_text("snapshot\n")
        (world.queue / "claude_runner.toml").write_text(
            "[worktree_reclaim]\ndiscardable_untracked = []\n"
        )
        result = reclaim(runner, world, "--apply")
        assert result.exit_code == 0, result.output
        assert "dirty: uncommitted: ?? tests/testthat/_problems/" in result.stdout
        assert (wt / ".git").is_file()

    def test_explicit_config_wins(self, runner: CliRunner, world: World, tmp_path: Path) -> None:
        world.add_task("t-merged")
        other = tmp_path / "other.toml"
        other.write_text('[worktree_reclaim]\nparent_branch = "release"\n')
        result = reclaim(runner, world, "--config", str(other))
        assert result.exit_code == 1, result.output
        assert "git fetch origin release" in result.stderr

    def test_invalid_config_exits_2(self, runner: CliRunner, world: World) -> None:
        (world.queue / "claude_runner.toml").write_text(
            '[worktree_reclaim]\nbranch_template = "main"\n'
        )
        result = reclaim(runner, world)
        assert result.exit_code == 2
        assert result.stderr.startswith("error: Settings validation failed")

    def test_not_a_queue_exits_2(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(app, ["worktree", "reclaim", "--queue", str(tmp_path)])
        assert result.exit_code == 2
        assert "has no todo/ subdirectory" in result.stderr
        assert not (tmp_path / ".claude_task_runner").exists(), (
            "a wrong --queue must not be written to"
        )


class TestSupervisorSnapshot:
    def test_in_flight_task_is_kept(self, runner: CliRunner, world: World) -> None:
        wt = world.add_task("t-hook")
        write_supervisor_snapshot(world, in_flight=["t-hook"])
        result = reclaim(runner, world, "--apply")
        assert result.exit_code == 0, result.output
        assert "in_flight: a dispatch thread still holds the task" in result.stdout
        assert (wt / ".git").is_file()

    def test_legacy_in_flight_ids_are_honoured(self, runner: CliRunner, world: World) -> None:
        wt = world.add_task("t-hook")
        path = write_supervisor_snapshot(world, in_flight=[])
        snapshot = persist_mod.load(path)
        assert snapshot is not None
        persist_mod.write_atomic(
            snapshot.model_copy(update={"in_flight_task_ids": ["t-hook"]}), path
        )
        result = reclaim(runner, world, "--apply")
        assert result.exit_code == 0, result.output
        assert (wt / ".git").is_file()

    def test_unreadable_snapshot_exits_2(self, runner: CliRunner, world: World) -> None:
        wt = world.add_task("t-merged")
        persist_mod.supervisor_state_path(world.queue).write_text("{not json")
        result = reclaim(runner, world, "--apply")
        assert result.exit_code == 2
        assert result.stderr.startswith("error: cannot tell which tasks are in flight: ")
        assert (wt / ".git").is_file()


class TestExitCodes:
    def test_fetch_failure_exits_1(self, runner: CliRunner, world: World) -> None:
        world.add_task("t-offline")
        git(world.repo, "remote", "set-url", "origin", str(world.root / "missing.git"))
        result = reclaim(runner, world, "--apply")
        assert result.exit_code == 1
        assert "keep" in result.stdout
        assert result.stderr.startswith("error: git fetch origin main in ")

    def test_kept_worktrees_alone_exit_0(self, runner: CliRunner, world: World) -> None:
        world.add_task("t-run", status="running")
        result = reclaim(runner, world, "--apply")
        assert result.exit_code == 0, result.output

    def test_unparseable_task_yaml_is_noted(self, runner: CliRunner, world: World) -> None:
        (todo_dir(world.queue) / "broken.yaml").write_text("id: [\n")
        result = reclaim(runner, world)
        assert result.exit_code == 0, result.output
        assert "note: 1 task YAML(s) in todo/ could not be parsed" in result.stderr


def test_branch_kept_by_git_is_shown_on_the_gone_line(runner: CliRunner, world: World) -> None:
    world.add_task("t-upstream")
    seed = git(world.repo, "rev-list", "--max-parents=0", "HEAD")
    git(world.repo, "push", "-q", "origin", f"{seed}:refs/heads/stale")
    git(world.repo, "branch", "-q", "--set-upstream-to=origin/stale", "claude/t-upstream")
    result = reclaim(runner, world, "--apply")
    assert result.exit_code == 0, result.output
    line = result.stdout.splitlines()[0]
    assert line.split()[:2] == ["gone", "t-upstream"]
    assert "git branch -d kept the branch: " in line
    assert result.stdout.splitlines()[-1].endswith("; 1 branch(es) kept by git branch -d")


def test_unopenable_lock_file_exits_2(runner: CliRunner, world: World) -> None:
    world.add_task("t-lock")
    (world.queue / "not-a-dir").write_text("regular file\n")
    (world.queue / "claude_runner.toml").write_text(
        '[worktree_reclaim]\nlock_file = "not-a-dir/setup_worktree.lock"\n'
    )
    result = reclaim(runner, world, "--apply")
    assert result.exit_code == 2
    assert result.stderr.startswith("error: cannot open lock_file ")
    assert (world.worktree("t-lock") / ".git").is_file()


def test_lines_carry_no_trailing_padding(runner: CliRunner, world: World) -> None:
    world.add_task("t-merged")
    result = reclaim(runner, world)
    assert result.stdout.splitlines()[0] == "would  t-merged"
