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

    def test_order_by_dispatch_high_first(self, runner: CliRunner, queue_dir: Path) -> None:
        """With --order-by-dispatch, priority high precedes normal even when
        the high task's id sorts alphabetically last."""
        _seed_task(queue_dir, "001-normal", priority="normal")
        _seed_task(queue_dir, "002-normal", priority="normal")
        _seed_task(queue_dir, "999-high", priority="high")
        result = runner.invoke(
            app,
            ["list", "--queue", str(queue_dir), "--json", "--order-by-dispatch"],
        )
        assert result.exit_code == 0, result.stdout
        payload = json.loads(result.stdout)
        ids = [t["id"] for t in payload["tasks"]]
        assert ids == ["999-high", "001-normal", "002-normal"]
        # dispatch_rank starts at 1 and matches list order.
        assert [t["dispatch_rank"] for t in payload["tasks"]] == [1, 2, 3]

    def test_order_by_dispatch_default_filename_order(
        self, runner: CliRunner, queue_dir: Path
    ) -> None:
        """Without the flag, ordering remains filename-sorted (legacy behavior)."""
        _seed_task(queue_dir, "001-normal", priority="normal")
        _seed_task(queue_dir, "999-high", priority="high")
        result = runner.invoke(app, ["list", "--queue", str(queue_dir), "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        ids = [t["id"] for t in payload["tasks"]]
        assert ids == ["001-normal", "999-high"]
        # No dispatch_rank field when the flag is off.
        assert all("dispatch_rank" not in t for t in payload["tasks"])


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

    def test_add_dir_persisted(
        self,
        runner: CliRunner,
        queue_dir: Path,
        tmp_path: Path,
    ) -> None:
        d1 = tmp_path / "shared"
        d1.mkdir()
        d2 = tmp_path / "data"
        d2.mkdir()
        result = runner.invoke(
            app,
            [
                "add",
                "--queue",
                str(queue_dir),
                "--id",
                "060-ad",
                "--title",
                "Add dir task",
                "--prompt",
                "x",
                "--add-dir",
                str(d1),
                "--add-dir",
                str(d2),
            ],
        )
        assert result.exit_code == 0, result.stdout
        text = task_path_for(queue_dir, "060-ad").read_text()
        # YAML serialized list contains both paths.
        assert str(d1) in text
        assert str(d2) in text
        assert "additional_dirs" in text

    def test_add_dir_missing_path_warns_but_succeeds(
        self,
        runner: CliRunner,
        queue_dir: Path,
        tmp_path: Path,
    ) -> None:
        absent = tmp_path / "ghost"  # never created
        result = runner.invoke(
            app,
            [
                "add",
                "--queue",
                str(queue_dir),
                "--id",
                "061-ghost",
                "--title",
                "x",
                "--prompt",
                "x",
                "--add-dir",
                str(absent),
            ],
        )
        # Non-existing dir is a warning, not a failure: the
        # pre-dispatch hook may create the dir before runtime.
        assert result.exit_code == 0, result.stdout
        assert "warning" in result.stdout.lower()
        text = task_path_for(queue_dir, "061-ghost").read_text()
        assert str(absent) in text


# ---------------------------------------------------------------------------
# ADR-0023: [queue].working_dir_template — `queue add` writes a per-task
# working_dir derived from the template (when set) so a pre-dispatch hook
# that depends on it (e.g. a per-task git-worktree setup script) doesn't
# silently short-circuit at runtime.
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, template: str) -> Path:
    """Write a minimal per-queue claude_runner.toml carrying the template.

    Other sections inherit the package defaults via load_settings's
    deep-merge, so the test config stays small.
    """
    cfg = tmp_path / "claude_runner.toml"
    cfg.write_text(f'[queue]\nworking_dir_template = "{template}"\n')
    return cfg


class TestAddWorkingDir:
    def test_template_substitutes_task_id(
        self,
        runner: CliRunner,
        queue_dir: Path,
        tmp_path: Path,
    ) -> None:
        cfg = _write_config(tmp_path, "/repos/foo/.wt/{task_id}")
        result = runner.invoke(
            app,
            [
                "add",
                "--config",
                str(cfg),
                "--queue",
                str(queue_dir),
                "--id",
                "070-templated",
                "--title",
                "templated",
                "--prompt",
                "x",
            ],
        )
        assert result.exit_code == 0, result.stdout
        text = task_path_for(queue_dir, "070-templated").read_text()
        assert "working_dir: /repos/foo/.wt/070-templated" in text

    def test_empty_template_leaves_null(
        self,
        runner: CliRunner,
        queue_dir: Path,
    ) -> None:
        """No --config (and thus no template) preserves the historical
        behavior: working_dir serialises as null."""
        result = runner.invoke(
            app,
            [
                "add",
                "--queue",
                str(queue_dir),
                "--id",
                "071-null",
                "--title",
                "x",
                "--prompt",
                "x",
            ],
        )
        assert result.exit_code == 0, result.stdout
        text = task_path_for(queue_dir, "071-null").read_text()
        assert "working_dir: null" in text

    def test_explicit_flag_overrides_template(
        self,
        runner: CliRunner,
        queue_dir: Path,
        tmp_path: Path,
    ) -> None:
        cfg = _write_config(tmp_path, "/repos/foo/.wt/{task_id}")
        explicit = "/repos/bar/something-else"
        result = runner.invoke(
            app,
            [
                "add",
                "--config",
                str(cfg),
                "--queue",
                str(queue_dir),
                "--id",
                "072-explicit",
                "--title",
                "x",
                "--prompt",
                "x",
                "--working-dir",
                explicit,
            ],
        )
        assert result.exit_code == 0, result.stdout
        text = task_path_for(queue_dir, "072-explicit").read_text()
        assert f"working_dir: {explicit}" in text
        # The templated value MUST NOT have leaked through.
        assert "/repos/foo/.wt/072-explicit" not in text

    def test_no_working_dir_flag_forces_null(
        self,
        runner: CliRunner,
        queue_dir: Path,
        tmp_path: Path,
    ) -> None:
        cfg = _write_config(tmp_path, "/repos/foo/.wt/{task_id}")
        result = runner.invoke(
            app,
            [
                "add",
                "--config",
                str(cfg),
                "--queue",
                str(queue_dir),
                "--id",
                "073-suppressed",
                "--title",
                "x",
                "--prompt",
                "x",
                "--no-working-dir",
            ],
        )
        assert result.exit_code == 0, result.stdout
        text = task_path_for(queue_dir, "073-suppressed").read_text()
        assert "working_dir: null" in text

    def test_working_dir_and_no_working_dir_conflict(
        self,
        runner: CliRunner,
        queue_dir: Path,
    ) -> None:
        result = runner.invoke(
            app,
            [
                "add",
                "--queue",
                str(queue_dir),
                "--id",
                "074-conflict",
                "--title",
                "x",
                "--prompt",
                "x",
                "--working-dir",
                "/tmp/anything",
                "--no-working-dir",
            ],
        )
        assert result.exit_code != 0
        assert "either --working-dir OR --no-working-dir" in result.stdout

    def test_template_with_bad_placeholder_fails_clearly(
        self,
        runner: CliRunner,
        queue_dir: Path,
        tmp_path: Path,
    ) -> None:
        cfg = _write_config(tmp_path, "/repos/{taskid}")  # typo: missing underscore
        result = runner.invoke(
            app,
            [
                "add",
                "--config",
                str(cfg),
                "--queue",
                str(queue_dir),
                "--id",
                "075-typo",
                "--title",
                "x",
                "--prompt",
                "x",
            ],
        )
        assert result.exit_code != 0
        assert "unknown placeholder" in result.stdout
        assert "taskid" in result.stdout


class TestBackfillWorkingDir:
    def test_refuses_when_template_unset(
        self,
        runner: CliRunner,
        queue_dir: Path,
    ) -> None:
        _seed_task(queue_dir, "100-pending")
        result = runner.invoke(
            app,
            ["backfill-working-dir", "--queue", str(queue_dir)],
        )
        assert result.exit_code == 2
        assert "not set" in result.stdout

    def test_fills_null_working_dir(
        self,
        runner: CliRunner,
        queue_dir: Path,
        tmp_path: Path,
    ) -> None:
        cfg = _write_config(tmp_path, "/wts/{task_id}")
        _seed_task(queue_dir, "101-null")
        _seed_task(queue_dir, "102-preset", working_dir="/already/set")
        result = runner.invoke(
            app,
            [
                "backfill-working-dir",
                "--config",
                str(cfg),
                "--queue",
                str(queue_dir),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.stdout
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        ids_updated = {u["id"] for u in payload["updated"]}
        assert ids_updated == {"101-null"}
        # 102-preset stays unchanged.
        text = task_path_for(queue_dir, "102-preset").read_text()
        assert "working_dir: /already/set" in text
        # 101-null now carries the templated value.
        text = task_path_for(queue_dir, "101-null").read_text()
        assert "working_dir: /wts/101-null" in text

    def test_is_idempotent(
        self,
        runner: CliRunner,
        queue_dir: Path,
        tmp_path: Path,
    ) -> None:
        """A second run after the first does nothing — all working_dirs
        are already set, so updated=0 and skipped=N."""
        cfg = _write_config(tmp_path, "/wts/{task_id}")
        _seed_task(queue_dir, "110-a")
        _seed_task(queue_dir, "111-b")
        first = runner.invoke(
            app,
            [
                "backfill-working-dir",
                "--config",
                str(cfg),
                "--queue",
                str(queue_dir),
                "--json",
            ],
        )
        assert first.exit_code == 0
        second = runner.invoke(
            app,
            [
                "backfill-working-dir",
                "--config",
                str(cfg),
                "--queue",
                str(queue_dir),
                "--json",
            ],
        )
        assert second.exit_code == 0
        payload = json.loads(second.stdout)
        assert payload["updated"] == []
        assert len(payload["skipped"]) == 2

    def test_dry_run_does_not_write(
        self,
        runner: CliRunner,
        queue_dir: Path,
        tmp_path: Path,
    ) -> None:
        cfg = _write_config(tmp_path, "/wts/{task_id}")
        _seed_task(queue_dir, "120-dry")
        result = runner.invoke(
            app,
            [
                "backfill-working-dir",
                "--config",
                str(cfg),
                "--queue",
                str(queue_dir),
                "--dry-run",
                "--json",
            ],
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["dry_run"] is True
        assert len(payload["updated"]) == 1
        # File on disk untouched.
        text = task_path_for(queue_dir, "120-dry").read_text()
        assert "working_dir: null" in text
