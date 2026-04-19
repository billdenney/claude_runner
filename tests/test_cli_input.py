"""Tests for the ``claude-runner input`` subcommand."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from claude_runner.cli import main
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
    from claude_runner.config import Settings

    init_project(tmp_path, settings=Settings())
    return tmp_path


def _seed_open_request(
    tmp_path: Path,
    *,
    task_id: str = "t1",
    seq: int = 1,
) -> InteractionRequest:
    """Write an OPEN request to the sidecar dir and mark the task AWAITING_INPUT."""
    runner_root = tmp_path / ".claude_runner"
    runner_root.mkdir(exist_ok=True)
    sidecar = SidecarStore(runner_root / "sidecar")
    state_store = StateStore(runner_root)
    req = InteractionRequest(
        task_id=task_id,
        sequence=seq,
        created_at=datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC),
        summary="pick one",
        questions=[
            Question(
                id="decision",
                prompt="which?",
                options=[Option(value="A", label="A"), Option(value="B", label="B")],
                recommended="A",
            )
        ],
        state=RequestState.OPEN,
    )
    sidecar.write_request(req)
    state_store.save(TaskState(task_id=task_id, status=TaskStatus.AWAITING_INPUT))
    return req


def test_input_happy_path_writes_response_and_promotes(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _seed_open_request(tmp_path)

    rc = main(
        [
            "input",
            "t1",
            str(tmp_path),
            "--answers",
            json.dumps({"decision": "A"}),
            "--notes",
            "looks good",
        ]
    )
    assert rc == 0

    runner_root = tmp_path / ".claude_runner"
    sidecar = SidecarStore(runner_root / "sidecar")
    resp = sidecar.load_response("t1", 1)
    assert resp is not None
    assert resp.state is RequestState.ANSWERED
    assert resp.answers[0].value == "A"
    assert resp.notes == "looks good"

    state = StateStore(runner_root).load("t1")
    assert state.status is TaskStatus.READY_TO_RESUME


def test_input_rejects_unknown_question_id(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _seed_open_request(tmp_path)
    rc = main(
        [
            "input",
            "t1",
            str(tmp_path),
            "--answers",
            json.dumps({"not_a_real_question": "A"}),
        ]
    )
    assert rc == 2


def test_input_rejects_missing_answer(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _seed_open_request(tmp_path)
    # Empty answer dict — missing 'decision'.
    rc = main(["input", "t1", str(tmp_path), "--answers", "{}"])
    assert rc == 2


def test_input_rejects_value_outside_options(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _seed_open_request(tmp_path)
    rc = main(
        [
            "input",
            "t1",
            str(tmp_path),
            "--answers",
            json.dumps({"decision": "Z"}),
        ]
    )
    assert rc == 2


def test_input_rejects_malformed_json(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _seed_open_request(tmp_path)
    rc = main(["input", "t1", str(tmp_path), "--answers", "not json"])
    assert rc == 2


def test_input_from_file(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _seed_open_request(tmp_path)
    answers_file = tmp_path / "answers.json"
    answers_file.write_text(json.dumps({"decision": "B"}))
    rc = main(
        [
            "input",
            "t1",
            str(tmp_path),
            "--from-file",
            str(answers_file),
        ]
    )
    assert rc == 0
    resp = SidecarStore(tmp_path / ".claude_runner" / "sidecar").load_response("t1", 1)
    assert resp is not None
    assert resp.answers[0].value == "B"


def test_input_rejects_both_answers_and_from_file(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _seed_open_request(tmp_path)
    rc = main(
        [
            "input",
            "t1",
            str(tmp_path),
            "--answers",
            json.dumps({"decision": "A"}),
            "--from-file",
            str(tmp_path / "x.json"),
        ]
    )
    assert rc == 2


def test_input_requires_some_source(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _seed_open_request(tmp_path)
    rc = main(["input", "t1", str(tmp_path)])
    assert rc == 2


def test_input_cancel_marks_task_failed(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _seed_open_request(tmp_path)
    rc = main(["input", "t1", str(tmp_path), "--cancel", "--notes", "operator nope"])
    assert rc == 0
    state = StateStore(tmp_path / ".claude_runner").load("t1")
    assert state.status is TaskStatus.FAILED
    assert state.error == "operator nope"
    resp = SidecarStore(tmp_path / ".claude_runner" / "sidecar").load_response("t1", 1)
    assert resp is not None
    assert resp.state is RequestState.CANCELLED


def test_input_no_open_request_returns_error(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    # No sidecar request at all.
    rc = main(["input", "t1", str(tmp_path), "--answers", json.dumps({"x": "y"})])
    assert rc == 2
