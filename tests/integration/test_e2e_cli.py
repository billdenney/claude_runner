"""End-to-end CLI walk-through.

Exercises the operator-facing surface of the runner against a fresh
queue, without spawning real ``claude``. Proves that:

* ``queue add`` writes a parseable task YAML.
* ``queue list``/``states`` see the task.
* ``watchdog register`` + ``watchdog tick --dry-run`` decide
  ``RESTART`` for a queue with no live supervisor.
* ``install-skills --copy`` materializes the four packaged skills.
* ``doctor`` runs end-to-end with no FAILures on a clean queue.

The dispatcher's full lifecycle (claude-shim → state machine →
persistence) is covered by ``tests/integration/test_dispatcher.py``;
this test fills the gap between the unit-tested CLI subcommands and
the dispatcher's already-tested core.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from claude_task_runner.cli import app as root_app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect HOME so the test doesn't touch the real
    ``~/.claude`` / ``~/.claude_task_runner``.

    This is critical: ``install-skills`` and ``watchdog register``
    write into the user's home, and ``doctor``'s ``check_global_lock``
    inspects ``~/.claude_task_runner/global.lock``.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    # Pre-populate a fake claude binary so check_claude_binary passes.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_claude = fake_bin / "claude"
    fake_claude.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_claude.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")
    return tmp_path


@pytest.fixture
def queue_dir(isolated_home: Path) -> Path:
    qd = isolated_home / "queue"
    qd.mkdir()
    (qd / "todo").mkdir()
    return qd


def _invoke(runner: CliRunner, args: list[str]) -> tuple[int, str, str]:
    """Invoke the top-level CLI and return (exit_code, stdout, stderr)."""
    result = runner.invoke(root_app, args, catch_exceptions=False)
    return result.exit_code, result.stdout, result.stderr or ""


class TestQueueLifecycle:
    def test_add_then_list(self, runner: CliRunner, queue_dir: Path, tmp_path: Path) -> None:
        prompt = tmp_path / "prompt.txt"
        prompt.write_text("Trivial test task.\n")

        code, out, _ = _invoke(
            runner,
            [
                "queue",
                "add",
                "--queue",
                str(queue_dir),
                "--id",
                "001-smoke",
                "--title",
                "Smoke test",
                "--prompt-file",
                str(prompt),
                "--model",
                "claude-opus-4-7",
                "--effort",
                "medium",
            ],
        )
        assert code == 0, out

        # The task should appear in `queue list --json`.
        code, out, _ = _invoke(
            runner,
            ["queue", "list", "--queue", str(queue_dir), "--json"],
        )
        assert code == 0
        payload = json.loads(out)
        ids = [t["id"] for t in payload["tasks"]]
        assert "001-smoke" in ids

    def test_states_filter_empty_initially(self, runner: CliRunner, queue_dir: Path) -> None:
        # No state files yet → states list is empty.
        code, out, _ = _invoke(
            runner,
            [
                "queue",
                "states",
                "--queue",
                str(queue_dir),
                "--status",
                "running",
                "--json",
            ],
        )
        assert code == 0
        assert json.loads(out) == {"states": []}


class TestSkillInstall:
    def test_copy_yes_installs_all_four(self, runner: CliRunner, isolated_home: Path) -> None:
        code, out, _ = _invoke(
            runner,
            ["install-skills", "--yes", "--copy"],
        )
        assert code == 0, out
        skills_dir = isolated_home / ".claude" / "skills"
        for name in (
            "runner-status",
            "runner-usage",
            "runner-add-task",
            "runner-answer-sidecar",
        ):
            assert (skills_dir / name / "SKILL.md").is_file()


class TestWatchdogRegister:
    def test_register_then_tick_dry_run_restarts(
        self,
        runner: CliRunner,
        queue_dir: Path,
        isolated_home: Path,
    ) -> None:
        # Register the queue.
        code, out, _ = _invoke(
            runner,
            ["watchdog", "register", "--queue", str(queue_dir)],
        )
        assert code == 0
        assert str(queue_dir.resolve()) in out

        # Without a supervisor running, dry-run tick should emit RESTART
        # but not actually spawn anything.
        code, out, _ = _invoke(runner, ["watchdog", "tick", "--dry-run"])
        assert code == 0
        assert "verdict=restart" in out


class TestDoctor:
    def test_doctor_passes_on_clean_queue(
        self,
        runner: CliRunner,
        queue_dir: Path,
        isolated_home: Path,
    ) -> None:
        # Install skills first so check_skills_installed passes.
        _invoke(runner, ["install-skills", "--yes", "--copy"])

        code, out, _ = _invoke(
            runner,
            ["doctor", "--queue", str(queue_dir), "--json"],
        )
        # WARN is OK, FAIL is not.
        payload = json.loads(out)
        failures = [r for r in payload["results"] if r["status"] == "fail"]
        assert failures == [], f"unexpected failures: {failures}"
        assert code == 0

    def test_doctor_human_output_contains_each_check(
        self,
        runner: CliRunner,
        queue_dir: Path,
        isolated_home: Path,
    ) -> None:
        _, out, _ = _invoke(runner, ["doctor", "--queue", str(queue_dir)])
        # Some checks may fail (no claude binary configured, no skills),
        # so just verify the human-readable output contains expected
        # check names rather than exit code.
        assert "claude_binary" in out
        assert "queue_layout" in out
        assert "supervisor_state" in out

    def test_doctor_detects_corrupt_supervisor_json(
        self,
        runner: CliRunner,
        queue_dir: Path,
    ) -> None:
        # Plant a corrupt supervisor.json.
        runtime = queue_dir / ".claude_task_runner"
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "supervisor.json").write_text("{not json")

        code, out, _ = _invoke(
            runner,
            ["doctor", "--queue", str(queue_dir), "--json"],
        )
        assert code == 1, out
        payload = json.loads(out)
        sv = next(r for r in payload["results"] if r["name"] == "supervisor_state")
        assert sv["status"] == "fail"

    def test_doctor_detects_invalid_task_yaml(
        self,
        runner: CliRunner,
        queue_dir: Path,
    ) -> None:
        bad = queue_dir / "todo" / "999-bad.yaml"
        bad.write_text("id: 999-bad\nfake_field_does_not_exist: 1\ntitle: x\nprompt: x\n")

        code, out, _ = _invoke(
            runner,
            ["doctor", "--queue", str(queue_dir), "--json"],
        )
        assert code == 1, out
        payload = json.loads(out)
        ty = next(r for r in payload["results"] if r["name"] == "task_yamls")
        assert ty["status"] == "fail"


class TestCliTopLevelHelp:
    def test_help_lists_every_subcommand(self, runner: CliRunner) -> None:
        result = runner.invoke(root_app, ["--help"])
        assert result.exit_code == 0
        for name in (
            "usage",
            "supervisor",
            "queue",
            "sidecar",
            "install",
            "install-skills",
            "watchdog",
            "doctor",
        ):
            assert name in result.stdout, f"{name} missing from --help"
