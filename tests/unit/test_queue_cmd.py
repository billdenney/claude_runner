"""Tests for cli.queue_cmd — list / states / show / add."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from claude_task_runner.cli.queue_cmd import app
from claude_task_runner.queue.schema import RunRecord, Task, TaskState
from claude_task_runner.queue.store import (
    queue_runtime_dir,
    state_path_for,
    task_path_for,
    todo_dir,
    write_state_atomic,
    write_task_atomic,
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


def _seed_task(qd: Path, task_id: str, **overrides: object) -> Task:
    payload = {
        "id": task_id,
        "title": f"Task {task_id}",
        "prompt": "do the thing",
        "model": "claude-opus-4-7",
        "effort": "medium",
        **overrides,
    }
    task = Task.model_validate(payload)
    write_task_atomic(task, task_path_for(qd, task_id))
    return task


def _seed_state(
    qd: Path, task_id: str, *, status: str = "pending", **overrides: object
) -> TaskState:
    payload: dict[str, object] = {
        "task_id": task_id,
        "status": status,
        **overrides,
    }
    state = TaskState.model_validate(payload)
    write_state_atomic(state, state_path_for(qd, task_id))
    return state


class TestList:
    def test_empty(self, runner: CliRunner, queue_dir: Path) -> None:
        result = runner.invoke(app, ["list", "--queue", str(queue_dir), "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload == {"tasks": []}

    def test_returns_pending_tasks(self, runner: CliRunner, queue_dir: Path) -> None:
        _seed_task(queue_dir, "001-a")
        _seed_task(queue_dir, "002-b", priority="high")
        result = runner.invoke(app, ["list", "--queue", str(queue_dir), "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        ids = [t["id"] for t in payload["tasks"]]
        assert ids == ["001-a", "002-b"]
        assert payload["tasks"][1]["priority"] == "high"

    def test_skips_malformed(self, runner: CliRunner, queue_dir: Path) -> None:
        bad = todo_dir(queue_dir) / "999-bad.yaml"
        bad.write_text("not even close to valid yaml: : :")
        result = runner.invoke(app, ["list", "--queue", str(queue_dir), "--json"])
        # Bad task surfaces as an error entry but doesn't fail.
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert any("error" in t for t in payload["tasks"])


class TestStates:
    def test_filter_by_status(self, runner: CliRunner, queue_dir: Path) -> None:
        _seed_state(queue_dir, "001", status="completed", attempts=1)
        _seed_state(queue_dir, "002", status="awaiting_sidecar", attempts=2)
        _seed_state(queue_dir, "003", status="failed", attempts=3)

        result = runner.invoke(
            app,
            [
                "states",
                "--queue",
                str(queue_dir),
                "--status",
                "awaiting_sidecar",
                "--json",
            ],
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert len(payload["states"]) == 1
        assert payload["states"][0]["task_id"] == "002"

    def test_no_filter_returns_all(self, runner: CliRunner, queue_dir: Path) -> None:
        for tid in ("001", "002", "003"):
            _seed_state(queue_dir, tid)
        result = runner.invoke(
            app,
            ["states", "--queue", str(queue_dir), "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert len(payload["states"]) == 3


class TestShow:
    def test_full_payload(self, runner: CliRunner, queue_dir: Path) -> None:
        _seed_task(queue_dir, "007-foo")
        when = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
        run = RunRecord(
            attempt=1,
            started_at=when,
            finished_at=when,
            stop_reason="end_turn",
            duration_s=1.5,
            cost_usd=0.42,
        )
        _seed_state(queue_dir, "007-foo", status="completed", attempts=1, runs=[run])
        result = runner.invoke(
            app,
            ["show", "007-foo", "--queue", str(queue_dir), "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["task_id"] == "007-foo"
        assert payload["task"] is not None
        assert payload["state"]["status"] == "completed"

    def test_missing_task(self, runner: CliRunner, queue_dir: Path) -> None:
        result = runner.invoke(
            app,
            ["show", "nope", "--queue", str(queue_dir), "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["task"] is None
        assert payload["state"] is None


class TestAdd:
    def test_basic_add(self, runner: CliRunner, queue_dir: Path) -> None:
        result = runner.invoke(
            app,
            [
                "add",
                "--queue",
                str(queue_dir),
                "--id",
                "010-new",
                "--title",
                "New thing",
                "--prompt",
                "do it",
                "--model",
                "claude-opus-4-7",
                "--effort",
                "high",
            ],
        )
        assert result.exit_code == 0, result.stdout
        path = task_path_for(queue_dir, "010-new")
        assert path.exists()
        assert "do it" in path.read_text()

    def test_invalid_id_rejected(self, runner: CliRunner, queue_dir: Path) -> None:
        result = runner.invoke(
            app,
            [
                "add",
                "--queue",
                str(queue_dir),
                "--id",
                "has space",
                "--title",
                "x",
                "--prompt",
                "x",
            ],
        )
        assert result.exit_code != 0
        assert "invalid task id" in result.stdout

    def test_unknown_model_rejected(self, runner: CliRunner, queue_dir: Path) -> None:
        result = runner.invoke(
            app,
            [
                "add",
                "--queue",
                str(queue_dir),
                "--id",
                "020-x",
                "--title",
                "x",
                "--prompt",
                "x",
                "--model",
                "claude-mystery-9-9",
                "--effort",
                "medium",
            ],
        )
        assert result.exit_code != 0
        # Either "unknown model" or "invalid effort" is acceptable —
        # validation knocks the task back either way.
        assert "unknown model" in result.stdout.lower() or "invalid effort" in result.stdout.lower()

    def test_unknown_effort_for_model_rejected(self, runner: CliRunner, queue_dir: Path) -> None:
        # claude-sonnet-4-6 doesn't have a "max" level in defaults.
        result = runner.invoke(
            app,
            [
                "add",
                "--queue",
                str(queue_dir),
                "--id",
                "021-y",
                "--title",
                "y",
                "--prompt",
                "y",
                "--model",
                "claude-sonnet-4-6",
                "--effort",
                "max",
            ],
        )
        assert result.exit_code != 0
        assert "invalid effort" in result.stdout

    def test_overwrite_protection(self, runner: CliRunner, queue_dir: Path) -> None:
        _seed_task(queue_dir, "030-existing")
        result = runner.invoke(
            app,
            [
                "add",
                "--queue",
                str(queue_dir),
                "--id",
                "030-existing",
                "--title",
                "x",
                "--prompt",
                "x",
            ],
        )
        assert result.exit_code != 0
        assert "already exists" in result.stdout

    def test_overwrite_flag(self, runner: CliRunner, queue_dir: Path) -> None:
        _seed_task(queue_dir, "030-existing", title="Old")
        result = runner.invoke(
            app,
            [
                "add",
                "--queue",
                str(queue_dir),
                "--id",
                "030-existing",
                "--title",
                "New",
                "--prompt",
                "x",
                "--overwrite",
            ],
        )
        assert result.exit_code == 0
        assert "New" in task_path_for(queue_dir, "030-existing").read_text()

    def test_prompt_from_file(
        self,
        runner: CliRunner,
        queue_dir: Path,
        tmp_path: Path,
    ) -> None:
        prompt_file = tmp_path / "p.txt"
        prompt_file.write_text("multi\nline\nprompt\n")
        result = runner.invoke(
            app,
            [
                "add",
                "--queue",
                str(queue_dir),
                "--id",
                "040-fp",
                "--title",
                "x",
                "--prompt-file",
                str(prompt_file),
            ],
        )
        assert result.exit_code == 0
        text = task_path_for(queue_dir, "040-fp").read_text()
        assert "multi" in text and "line" in text and "prompt" in text

    def test_must_supply_prompt(self, runner: CliRunner, queue_dir: Path) -> None:
        result = runner.invoke(
            app,
            [
                "add",
                "--queue",
                str(queue_dir),
                "--id",
                "050-np",
                "--title",
                "x",
            ],
        )
        assert result.exit_code != 0
        assert "must supply --prompt" in result.stdout

    def test_cannot_supply_both(
        self,
        runner: CliRunner,
        queue_dir: Path,
        tmp_path: Path,
    ) -> None:
        prompt_file = tmp_path / "p.txt"
        prompt_file.write_text("x")
        result = runner.invoke(
            app,
            [
                "add",
                "--queue",
                str(queue_dir),
                "--id",
                "051-bp",
                "--title",
                "x",
                "--prompt",
                "y",
                "--prompt-file",
                str(prompt_file),
            ],
        )
        assert result.exit_code != 0
        assert "either --prompt OR --prompt-file" in result.stdout
