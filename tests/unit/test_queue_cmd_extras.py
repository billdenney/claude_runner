"""Extra tests for cli/queue_cmd.py — error paths and human-readable
rendering branches.

Augments tests/unit/test_queue_cmd.py (the existing happy-path
coverage) to hit the lines that handle invalid input / human formatting.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from claude_task_runner.cli.queue_cmd import app
from claude_task_runner.queue.schema import Task, TaskState
from claude_task_runner.queue.store import (
    load_state,
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


def _make_task(qd: Path, task_id: str) -> Task:
    task = Task.model_validate({"id": task_id, "title": f"Task {task_id}", "prompt": "do thing"})
    write_task_atomic(task, task_path_for(qd, task_id))
    return task


# ---------------------------------------------------------------------------
# `list` — empty / human / error item
# ---------------------------------------------------------------------------


def test_list_empty_queue_human(runner: CliRunner, queue_dir: Path) -> None:
    result = runner.invoke(app, ["list", "--queue", str(queue_dir)])
    assert result.exit_code == 0
    assert "No pending tasks" in result.stdout


def test_list_with_unparseable_task_yaml(runner: CliRunner, queue_dir: Path) -> None:
    _make_task(queue_dir, "good")
    (queue_dir / "todo" / "bad.yaml").write_text("not yaml: ][", encoding="utf-8")
    result = runner.invoke(app, ["list", "--queue", str(queue_dir)])
    assert result.exit_code == 0
    assert "good" in result.stdout
    assert "bad" in result.stdout  # error item rendered too


def test_list_human_renders_title_and_model(runner: CliRunner, queue_dir: Path) -> None:
    _make_task(queue_dir, "t1")
    result = runner.invoke(app, ["list", "--queue", str(queue_dir)])
    assert result.exit_code == 0
    assert "Task t1" in result.stdout


# ---------------------------------------------------------------------------
# `states` — empty / filter / unparseable / human color branches
# ---------------------------------------------------------------------------


def test_states_empty_human(runner: CliRunner, queue_dir: Path) -> None:
    result = runner.invoke(app, ["states", "--queue", str(queue_dir)])
    assert result.exit_code == 0
    assert "no matching states" in result.stdout


def test_states_with_filter_no_match(runner: CliRunner, queue_dir: Path) -> None:
    _make_task(queue_dir, "t1")
    write_state_atomic(
        TaskState(task_id="t1", status="running"),
        state_path_for(queue_dir, "t1"),
    )
    result = runner.invoke(app, ["states", "--queue", str(queue_dir), "--status", "failed"])
    assert result.exit_code == 0
    assert "no matching states" in result.stdout


def test_states_with_unparseable_file(runner: CliRunner, queue_dir: Path) -> None:
    state_dir = queue_dir / ".claude_task_runner" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "bad.yaml").write_text("not yaml: ][", encoding="utf-8")
    result = runner.invoke(app, ["states", "--queue", str(queue_dir), "--json"])
    assert result.exit_code == 0
    import json as _json

    payload = _json.loads(result.stdout)
    assert any("error" in s for s in payload["states"])


def test_states_human_color_branches(runner: CliRunner, queue_dir: Path) -> None:
    """Each status category lights up a different color: green (completed),
    red (failed/failed_circuit_breaker), yellow (anything else)."""
    for tid, status in [
        ("done", "completed"),
        ("brk", "failed_circuit_breaker"),
        ("wait", "pending"),
    ]:
        _make_task(queue_dir, tid)
        write_state_atomic(
            TaskState(task_id=tid, status=status),
            state_path_for(queue_dir, tid),
        )
    result = runner.invoke(app, ["states", "--queue", str(queue_dir)])
    assert result.exit_code == 0
    for tid in ["done", "brk", "wait"]:
        assert tid in result.stdout


# ---------------------------------------------------------------------------
# `show` — JSON and human paths, with missing task / corrupt state
# ---------------------------------------------------------------------------


def test_show_task_only_no_state(runner: CliRunner, queue_dir: Path) -> None:
    _make_task(queue_dir, "t1")
    result = runner.invoke(app, ["show", "t1", "--queue", str(queue_dir)])
    assert result.exit_code == 0
    assert "Task t1" in result.stdout


def test_show_state_only_no_task(runner: CliRunner, queue_dir: Path) -> None:
    """State file exists but no task YAML — show still renders."""
    write_state_atomic(
        TaskState(task_id="orphan", status="completed"),
        state_path_for(queue_dir, "orphan"),
    )
    result = runner.invoke(app, ["show", "orphan", "--queue", str(queue_dir)])
    assert result.exit_code == 0
    assert "orphan" in result.stdout
    assert "completed" in result.stdout


def test_show_corrupt_state(runner: CliRunner, queue_dir: Path) -> None:
    _make_task(queue_dir, "t1")
    sp = state_path_for(queue_dir, "t1")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("not yaml: ][", encoding="utf-8")
    result = runner.invoke(app, ["show", "t1", "--queue", str(queue_dir)])
    assert result.exit_code == 0
    assert "state error" in result.stdout


def test_show_corrupt_task(runner: CliRunner, queue_dir: Path) -> None:
    (queue_dir / "todo" / "t1.yaml").write_text("not yaml: ][", encoding="utf-8")
    result = runner.invoke(app, ["show", "t1", "--queue", str(queue_dir)])
    assert result.exit_code == 0
    assert "task error" in result.stdout


def _make_task_with_requires(qd: Path, task_id: str, rel_path: str) -> Task:
    task = Task.model_validate(
        {
            "id": task_id,
            "title": f"Task {task_id}",
            "prompt": "do thing",
            "requires": [{"kind": "file", "path": rel_path, "note": "the trimmed input"}],
        }
    )
    write_task_atomic(task, task_path_for(qd, task_id))
    return task


def test_show_reports_unmet_requirement(runner: CliRunner, queue_dir: Path) -> None:
    """`queue show` surfaces an unmet readiness requirement (ADR-0030)."""
    _make_task_with_requires(queue_dir, "t1", "papers/x_trimmed.md")
    result = runner.invoke(app, ["show", "t1", "--queue", str(queue_dir)])
    assert result.exit_code == 0
    assert "0/1 satisfied" in result.stdout
    assert "missing file" in result.stdout
    assert "the trimmed input" in result.stdout


def test_show_reports_satisfied_requirement(runner: CliRunner, queue_dir: Path) -> None:
    _make_task_with_requires(queue_dir, "t1", "papers/x_trimmed.md")
    (queue_dir / "papers").mkdir()
    (queue_dir / "papers" / "x_trimmed.md").write_text("x", encoding="utf-8")
    result = runner.invoke(app, ["show", "t1", "--queue", str(queue_dir)])
    assert result.exit_code == 0
    assert "1/1 satisfied" in result.stdout


def test_show_requirement_status_in_json(runner: CliRunner, queue_dir: Path) -> None:
    _make_task_with_requires(queue_dir, "t1", "papers/x_trimmed.md")
    result = runner.invoke(app, ["show", "t1", "--queue", str(queue_dir), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["readiness"] == {
        "required": 1,
        "satisfied": 0,
        "unmet": [f"missing file: {queue_dir / 'papers' / 'x_trimmed.md'} (the trimmed input)"],
    }


# ---------------------------------------------------------------------------
# `add` — input validation branches
# ---------------------------------------------------------------------------


def test_add_invalid_id(runner: CliRunner, queue_dir: Path) -> None:
    result = runner.invoke(
        app,
        [
            "add",
            "--queue",
            str(queue_dir),
            "--id",
            "invalid id with spaces",
            "--title",
            "test",
            "--prompt",
            "do it",
        ],
    )
    assert result.exit_code == 2
    assert "invalid task id" in result.stdout


def test_add_invalid_priority(runner: CliRunner, queue_dir: Path) -> None:
    result = runner.invoke(
        app,
        [
            "add",
            "--queue",
            str(queue_dir),
            "--id",
            "t1",
            "--title",
            "test",
            "--prompt",
            "do it",
            "--priority",
            "wat",
        ],
    )
    assert result.exit_code == 2
    assert "invalid priority" in result.stdout


def test_add_requires_prompt_or_prompt_file(runner: CliRunner, queue_dir: Path) -> None:
    """Neither --prompt nor --prompt-file → exit 2."""
    result = runner.invoke(
        app,
        [
            "add",
            "--queue",
            str(queue_dir),
            "--id",
            "t1",
            "--title",
            "test",
        ],
    )
    assert result.exit_code == 2
    assert "--prompt or --prompt-file" in result.stdout


def test_add_rejects_both_prompt_flags(runner: CliRunner, queue_dir: Path, tmp_path: Path) -> None:
    pfile = tmp_path / "prompt.txt"
    pfile.write_text("do it from file", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "add",
            "--queue",
            str(queue_dir),
            "--id",
            "t1",
            "--title",
            "test",
            "--prompt",
            "inline",
            "--prompt-file",
            str(pfile),
        ],
    )
    assert result.exit_code == 2
    assert "not both" in result.stdout


def test_add_prompt_file_unreadable(runner: CliRunner, queue_dir: Path, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "add",
            "--queue",
            str(queue_dir),
            "--id",
            "t1",
            "--title",
            "test",
            "--prompt-file",
            str(tmp_path / "missing.txt"),
        ],
    )
    assert result.exit_code == 2
    assert "read failed" in result.stdout


def test_add_unknown_effort_level(runner: CliRunner, queue_dir: Path) -> None:
    """A model-known but invalid effort level fails with a helpful msg."""
    result = runner.invoke(
        app,
        [
            "add",
            "--queue",
            str(queue_dir),
            "--id",
            "t1",
            "--title",
            "test",
            "--prompt",
            "do it",
            "--effort",
            "very-yes-much-effort",
        ],
    )
    assert result.exit_code == 2
    assert "invalid effort" in result.stdout


def test_add_unknown_model(runner: CliRunner, queue_dir: Path) -> None:
    """A model with no effort entries fails (the error path includes
    both UnknownModel and UnknownEffortLevel-by-way-of-missing-config —
    either flavor counts here as long as exit is 2 and the model name
    appears in the message)."""
    result = runner.invoke(
        app,
        [
            "add",
            "--queue",
            str(queue_dir),
            "--id",
            "t1",
            "--title",
            "test",
            "--prompt",
            "do it",
            "--model",
            "claude-totally-not-a-real-model",
        ],
    )
    assert result.exit_code == 2
    assert "claude-totally-not-a-real-model" in result.stdout


def test_add_exists_without_overwrite(runner: CliRunner, queue_dir: Path) -> None:
    """Pre-existing YAML at the target path blocks add (no --overwrite)."""
    _make_task(queue_dir, "t1")
    result = runner.invoke(
        app,
        [
            "add",
            "--queue",
            str(queue_dir),
            "--id",
            "t1",
            "--title",
            "different",
            "--prompt",
            "do something else",
        ],
    )
    assert result.exit_code == 2
    assert "already exists" in result.stdout


def test_add_exists_with_overwrite(runner: CliRunner, queue_dir: Path) -> None:
    _make_task(queue_dir, "t1")
    result = runner.invoke(
        app,
        [
            "add",
            "--queue",
            str(queue_dir),
            "--id",
            "t1",
            "--title",
            "different",
            "--prompt",
            "do something else",
            "--overwrite",
        ],
    )
    assert result.exit_code == 0
    # File on disk is the new content.
    new_task = task_path_for(queue_dir, "t1")
    assert "different" in new_task.read_text()


def test_add_via_prompt_file(runner: CliRunner, queue_dir: Path, tmp_path: Path) -> None:
    pfile = tmp_path / "prompt.txt"
    pfile.write_text("long-form prompt text", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "add",
            "--queue",
            str(queue_dir),
            "--id",
            "t-pfile",
            "--title",
            "from file",
            "--prompt-file",
            str(pfile),
        ],
    )
    assert result.exit_code == 0
    assert "wrote" in result.stdout


# ---------------------------------------------------------------------------
# `restart-fresh` — ADR-0024 escape hatch for stuck session affinity
# ---------------------------------------------------------------------------


def _seed_state(qd: Path, task_id: str, **overrides: object) -> TaskState:
    payload: dict[str, object] = {"task_id": task_id, "status": "pending", **overrides}
    state = TaskState.model_validate(payload)
    write_state_atomic(state, state_path_for(qd, task_id))
    return state


def test_restart_fresh_clears_session_human(runner: CliRunner, queue_dir: Path) -> None:
    _seed_state(
        queue_dir,
        "t1",
        session_id="sess-abc",
        session_account="work",
        resume_attempts=3,
    )
    result = runner.invoke(app, ["restart-fresh", "t1", "--queue", str(queue_dir)])
    assert result.exit_code == 0
    assert "cleared session for task t1" in result.stdout
    # State on disk has cleared fields.
    reloaded = load_state(state_path_for(queue_dir, "t1"))
    assert reloaded.session_id is None
    assert reloaded.session_account is None
    assert reloaded.resume_attempts == 0


def test_restart_fresh_json_payload(runner: CliRunner, queue_dir: Path) -> None:
    _seed_state(queue_dir, "t2", session_id="sess-xyz", session_account="personal")
    result = runner.invoke(app, ["restart-fresh", "t2", "--queue", str(queue_dir), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {
        "ok": True,
        "noop": False,
        "task_id": "t2",
        "cleared_session_id": "sess-xyz",
        "cleared_session_account": "personal",
    }


def test_restart_fresh_noop_when_already_clear(runner: CliRunner, queue_dir: Path) -> None:
    _seed_state(queue_dir, "t3")  # no session_id, no session_account
    result = runner.invoke(app, ["restart-fresh", "t3", "--queue", str(queue_dir), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["noop"] is True
    assert payload["task_id"] == "t3"


def test_restart_fresh_noop_human(runner: CliRunner, queue_dir: Path) -> None:
    _seed_state(queue_dir, "t4")
    result = runner.invoke(app, ["restart-fresh", "t4", "--queue", str(queue_dir)])
    assert result.exit_code == 0
    assert "no active session" in result.stdout


def test_restart_fresh_missing_state_yaml_human(runner: CliRunner, queue_dir: Path) -> None:
    result = runner.invoke(app, ["restart-fresh", "ghost", "--queue", str(queue_dir)])
    assert result.exit_code == 2
    assert "no state YAML" in result.stdout


def test_restart_fresh_missing_state_yaml_json(runner: CliRunner, queue_dir: Path) -> None:
    result = runner.invoke(app, ["restart-fresh", "ghost", "--queue", str(queue_dir), "--json"])
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "no state YAML" in payload["error"]


def test_restart_fresh_corrupt_state_yaml_human(runner: CliRunner, queue_dir: Path) -> None:
    state_path_for(queue_dir, "broken").write_text("not yaml: ][", encoding="utf-8")
    result = runner.invoke(app, ["restart-fresh", "broken", "--queue", str(queue_dir)])
    assert result.exit_code == 2
    assert "cannot parse state YAML" in result.stdout


def test_restart_fresh_corrupt_state_yaml_json(runner: CliRunner, queue_dir: Path) -> None:
    state_path_for(queue_dir, "broken").write_text("not yaml: ][", encoding="utf-8")
    result = runner.invoke(
        app,
        ["restart-fresh", "broken", "--queue", str(queue_dir), "--json"],
    )
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "cannot parse state YAML" in payload["error"]
