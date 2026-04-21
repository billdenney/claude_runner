"""Tests for `status --active` / `status --compact` / `status --filter` and
the new `awaiting` subcommand for sidecar-request inspection."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_runner.cli import main
from claude_runner.config import Settings
from claude_runner.models import TaskState, TaskStatus
from claude_runner.scaffold import init_project
from claude_runner.sidecar.schema import (
    InteractionRequest,
    Option,
    Question,
    RequestState,
)
from claude_runner.sidecar.store import SidecarStore
from claude_runner.state.store import StateStore


def _scaffold(tmp_path: Path) -> Path:
    init_project(tmp_path, settings=Settings())
    # init_project drops an example task YAML; delete so tests can assert on
    # exact counts without worrying about the boilerplate.
    for p in (tmp_path / "todo").glob("*.yaml"):
        p.unlink()
    return tmp_path


def _seed_state(
    tmp_path: Path, task_id: str, status: TaskStatus, *, with_sidecar: bool = False
) -> None:
    """Create a task YAML and state entry at the given status."""
    import yaml

    todo = tmp_path / "todo" / f"{task_id}.yaml"
    todo.write_text(
        yaml.safe_dump({"prompt": "do a thing", "working_dir": str(tmp_path), "id": task_id})
    )
    state_store = StateStore(tmp_path / ".claude_runner")
    state_store.save(TaskState(task_id=task_id, status=status))
    if with_sidecar:
        sc = SidecarStore(tmp_path / ".claude_runner" / "sidecar")
        sc.write_request(
            InteractionRequest(
                task_id=task_id,
                sequence=1,
                created_at=datetime(2026, 4, 20, 13, 0, tzinfo=UTC),
                summary=f"sidecar summary for {task_id}",
                context=f"context blob for {task_id}",
                questions=[
                    Question(
                        id="pick",
                        prompt=f"Which option for {task_id}?",
                        options=[
                            Option(value="A", label="First option"),
                            Option(value="B", label="Second option"),
                        ],
                        recommended="A",
                    )
                ],
                state=RequestState.OPEN,
            )
        )


def test_status_active_hides_completed_tasks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _scaffold(tmp_path)
    _seed_state(tmp_path, "done-task", TaskStatus.COMPLETED)
    _seed_state(tmp_path, "running-task", TaskStatus.RUNNING)
    _seed_state(tmp_path, "pending-task", TaskStatus.PENDING)

    rc = main(["status", str(tmp_path), "--active"])
    assert rc == 0
    out = capsys.readouterr().out
    # The per-status counts line shows everything regardless.
    assert "3 tasks" in out
    # Only non-completed should be in the table body — completed hidden.
    assert "running-task" in out
    assert "pending-task" in out
    assert "done-task" not in out


def test_status_filter_limits_to_explicit_statuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _scaffold(tmp_path)
    _seed_state(tmp_path, "done-task", TaskStatus.COMPLETED)
    _seed_state(tmp_path, "running-task", TaskStatus.RUNNING)
    _seed_state(tmp_path, "failed-task", TaskStatus.FAILED)

    rc = main(["status", str(tmp_path), "--filter", "failed"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "failed-task" in out
    assert "done-task" not in out
    assert "running-task" not in out


def test_status_filter_overrides_active(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--filter takes precedence over --active (explicit > implicit)."""
    _scaffold(tmp_path)
    _seed_state(tmp_path, "done-task", TaskStatus.COMPLETED)
    _seed_state(tmp_path, "running-task", TaskStatus.RUNNING)

    # completed is NOT in --active's default set, but --filter says show it.
    rc = main(["status", str(tmp_path), "--active", "--filter", "completed"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "done-task" in out
    assert "running-task" not in out


def test_status_compact_skips_table(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _scaffold(tmp_path)
    _seed_state(tmp_path, "alpha", TaskStatus.RUNNING)
    _seed_state(tmp_path, "beta", TaskStatus.COMPLETED)

    rc = main(["status", str(tmp_path), "-c"])
    assert rc == 0
    out = capsys.readouterr().out
    # Count line present.
    assert "2 tasks" in out
    # Per-task rows NOT present (table suppressed).
    assert "alpha" not in out
    assert "beta" not in out


def test_status_counts_line_always_prints(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _scaffold(tmp_path)
    _seed_state(tmp_path, "a", TaskStatus.COMPLETED)
    _seed_state(tmp_path, "b", TaskStatus.COMPLETED)
    _seed_state(tmp_path, "c", TaskStatus.RUNNING)

    rc = main(["status", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    # Sorted alphabetically: completed then running.
    assert "2 completed" in out
    assert "1 running" in out


def test_awaiting_list_shows_open_sidecar_requests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _scaffold(tmp_path)
    _seed_state(tmp_path, "foo", TaskStatus.AWAITING_INPUT, with_sidecar=True)
    _seed_state(tmp_path, "bar", TaskStatus.AWAITING_INPUT, with_sidecar=True)
    _seed_state(tmp_path, "baz", TaskStatus.COMPLETED)  # no sidecar, not listed

    rc = main(["awaiting", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Tasks awaiting operator input (2):" in out
    assert "foo" in out
    assert "bar" in out
    assert "sidecar summary for foo" in out
    assert "baz" not in out


def test_awaiting_empty_when_nothing_awaiting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _scaffold(tmp_path)
    _seed_state(tmp_path, "done", TaskStatus.COMPLETED)

    rc = main(["awaiting", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "No tasks awaiting operator input" in out


def test_awaiting_show_pretty_prints_request_body(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _scaffold(tmp_path)
    _seed_state(tmp_path, "foo", TaskStatus.AWAITING_INPUT, with_sidecar=True)

    rc = main(["awaiting", str(tmp_path), "--show", "foo"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Task foo — request seq 1" in out
    # Context and question prompt should appear.
    assert "context blob for foo" in out
    assert "Which option for foo?" in out
    # Options with labels and recommended marker.
    assert "First option" in out
    assert "Second option" in out
    assert "RECOMMENDED" in out
    # Help-line suggesting the answer command.
    assert "claude-runner input foo" in out


def test_awaiting_show_reports_missing_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _scaffold(tmp_path)
    rc = main(["awaiting", str(tmp_path), "--show", "nonexistent"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "No open sidecar request for task" in out
    assert "nonexistent" in out
