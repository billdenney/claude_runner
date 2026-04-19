from __future__ import annotations

from datetime import UTC
from pathlib import Path
from typing import Any

import pytest

from claude_runner.cli import main
from claude_runner.config import load_settings
from claude_runner.state.store import StateStore


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0


def test_init_creates_project(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["init", str(tmp_path)])
    assert rc == 0
    assert (tmp_path / "claude_runner.toml").exists()
    assert (tmp_path / "todo").is_dir()
    # Example task must validate.
    settings = load_settings(tmp_path)
    assert settings.plan in {"pro", "max5", "max20", "team", "custom"}


def test_new_creates_task_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    main(["init", "."])
    rc = main(["new", "Example", "--dir", str(tmp_path / "todo"), "--effort", "low"])
    assert rc == 0
    created = list((tmp_path / "todo").glob("*-example.yaml"))
    assert created


def test_validate_reports_errors(tmp_path: Path) -> None:
    main(["init", str(tmp_path)])
    # Write a broken task file.
    (tmp_path / "todo" / "bad.yaml").write_text(
        "prompt: x\n", encoding="utf-8"
    )  # missing working_dir
    rc = main(["validate", str(tmp_path)])
    assert rc != 0


def test_validate_passes_on_valid_dir(tmp_path: Path) -> None:
    main(["init", str(tmp_path)])
    rc = main(["validate", str(tmp_path)])
    assert rc == 0


def test_run_end_to_end_with_fake_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the full `run` command with a patched backend factory."""
    main(["init", str(tmp_path)])
    main(["new", "first", "--dir", str(tmp_path / "todo")])
    main(["new", "second", "--dir", str(tmp_path / "todo")])

    from claude_runner import cli as cli_mod
    from claude_runner.models import DispatchResult, StopReason, TaskStatus, TokenUsage

    class FakeBackend:
        name = "fake"

        def __init__(self, *, state_store: StateStore, emitter: Any) -> None:
            self._state = state_store

        async def run_task(self, spec):
            import asyncio
            from datetime import datetime

            state = self._state.load(spec.id)
            state.status = TaskStatus.COMPLETED
            state.stop_reason = StopReason.END_TURN
            state.session_id = f"sid-{spec.id}"
            state.last_finished_at = datetime.now(tz=UTC)
            self._state.save(state)
            await asyncio.sleep(0)
            return DispatchResult(
                task_id=spec.id,
                success=True,
                usage=TokenUsage(input_tokens=10, output_tokens=5),
                stop_reason=StopReason.END_TURN,
                session_id=f"sid-{spec.id}",
                duration_s=0.1,
            )

    monkeypatch.setattr(
        cli_mod,
        "_build_backend",
        lambda settings, *, state_store, emitter: FakeBackend(
            state_store=state_store, emitter=emitter
        ),
    )
    rc = main(["run", str(tmp_path)])
    assert rc == 0
    store = StateStore(tmp_path / ".claude_runner")
    statuses = {s.task_id: s.status for s in store.iter_states()}
    assert all(v.value == "completed" for v in statuses.values())
    # Events file populated.
    events = store.events_path().read_text().splitlines()
    assert any('"run_started"' in line for line in events)
    assert any('"run_finished"' in line for line in events)


def test_status_command_runs(tmp_path: Path) -> None:
    main(["init", str(tmp_path)])
    rc = main(["status", str(tmp_path)])
    assert rc == 0


def test_resume_command(tmp_path: Path) -> None:
    main(["init", str(tmp_path)])
    # Pre-write state as failed.
    from claude_runner.models import TaskState, TaskStatus

    store = StateStore(tmp_path / ".claude_runner")
    store.save(TaskState(task_id="x", status=TaskStatus.FAILED, error="prev"))
    rc = main(["resume", "x", str(tmp_path)])
    assert rc == 0
    assert store.load("x").status is TaskStatus.PENDING
    assert store.load("x").error is None


def test_verbose_flag_sets_logging() -> None:
    # -vv should not raise.
    with pytest.raises(SystemExit):
        main(["-vv", "--version"])
