"""Extra tests for cli/sidecar_cmd.py — error paths, human-readable
output branches, and answer-validation edge cases.

Augments tests/unit/test_sidecar_cmd.py (the existing happy-path
coverage) to hit the lines that handle malformed inputs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from claude_task_runner.cli.sidecar_cmd import app
from claude_task_runner.queue.schema import (
    SidecarOption,
    SidecarQuestion,
    SidecarRequest,
)
from claude_task_runner.queue.sidecar import write_request
from claude_task_runner.queue.store import queue_runtime_dir


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    return qd


def _seed_request(
    qd: Path,
    *,
    task_id: str,
    sequence: int = 1,
    summary: str = "Pick the right encoding",
    context: str = "",
    multi_select: bool = False,
    allow_free_text: bool = False,
    recommended: str | None = "A",
) -> SidecarRequest:
    req = SidecarRequest(
        task_id=task_id,
        sequence=sequence,
        created_at=datetime(2026, 5, 16, 12, 0, tzinfo=UTC),
        summary=summary,
        context=context,
        questions=[
            SidecarQuestion(
                id="encoding",
                prompt="Which encoding?",
                options=[
                    SidecarOption(value="A", label="Encoding A", description="annotated"),
                    SidecarOption(value="B", label="Encoding B"),
                ],
                recommended=recommended,
                multi_select=multi_select,
                allow_free_text=allow_free_text,
            )
        ],
    )
    write_request(qd, req)
    return req


# ---------------------------------------------------------------------------
# `list` — error path on unparseable request
# ---------------------------------------------------------------------------


def test_list_shows_error_item_for_unparseable_request(runner: CliRunner, queue_dir: Path) -> None:
    """A request-NNN.json that doesn't validate against the schema must
    appear in the listing with an error marker, not crash the command."""
    # Drop a malformed request alongside a valid one.
    _seed_request(queue_dir, task_id="t-good", sequence=1)
    bad_dir = queue_dir / ".claude_task_runner" / "sidecar" / "t-bad"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "request-001.json").write_text("not json{}", encoding="utf-8")

    result = runner.invoke(app, ["list", "--queue", str(queue_dir)])
    assert result.exit_code == 0
    assert "t-good" in result.stdout
    # Bad item must surface in human output (error coloring includes the task id).
    assert "t-bad" in result.stdout


def test_list_json_includes_error_field_for_unparseable(runner: CliRunner, queue_dir: Path) -> None:
    bad_dir = queue_dir / ".claude_task_runner" / "sidecar" / "t-bad"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "request-001.json").write_text("not json{}", encoding="utf-8")
    result = runner.invoke(app, ["list", "--queue", str(queue_dir), "--json"])
    assert result.exit_code == 0
    import json as _json

    payload = _json.loads(result.stdout)
    assert any("error" in item for item in payload["sidecars"])


# ---------------------------------------------------------------------------
# `show` — human-readable branch coverage
# ---------------------------------------------------------------------------


def test_show_human_readable_full(runner: CliRunner, queue_dir: Path) -> None:
    """Cover the human-readable output branches: context lines,
    recommended option marker, option description, both flags."""
    _seed_request(
        queue_dir,
        task_id="t1",
        sequence=1,
        context="line 1\nline 2",
        multi_select=True,
        allow_free_text=True,
        recommended="A",
    )
    result = runner.invoke(app, ["show", "t1", "1", "--queue", str(queue_dir)])
    assert result.exit_code == 0
    # task / seq / summary / context lines
    assert "t1" in result.stdout
    assert "line 1" in result.stdout
    assert "line 2" in result.stdout
    # Question, option marker (recommended), and description.
    assert "Encoding A" in result.stdout
    assert "annotated" in result.stdout
    # Flag advisories.
    assert "free-text" in result.stdout
    assert "multi-select" in result.stdout


def test_show_unparseable_request_returns_2(runner: CliRunner, queue_dir: Path) -> None:
    bad_dir = queue_dir / ".claude_task_runner" / "sidecar" / "t-bad"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "request-001.json").write_text("not json{}", encoding="utf-8")
    result = runner.invoke(app, ["show", "t-bad", "1", "--queue", str(queue_dir)])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# `answer` — input-validation error branches
# ---------------------------------------------------------------------------


def test_answer_requires_exactly_one_of_answers_or_file(runner: CliRunner, queue_dir: Path) -> None:
    _seed_request(queue_dir, task_id="t1", sequence=1)
    # Neither flag: exit 2.
    result = runner.invoke(app, ["answer", "t1", "1", "--queue", str(queue_dir)])
    assert result.exit_code == 2

    # Both flags: also exit 2.
    answers_file = queue_dir / "answers.json"
    answers_file.write_text("[]", encoding="utf-8")
    result_both = runner.invoke(
        app,
        [
            "answer",
            "t1",
            "1",
            "--queue",
            str(queue_dir),
            "--answers",
            "[]",
            "--answers-file",
            str(answers_file),
        ],
    )
    assert result_both.exit_code == 2


def test_answer_answers_file_unreadable(runner: CliRunner, queue_dir: Path) -> None:
    """answers_file that doesn't exist → OSError → exit 2."""
    _seed_request(queue_dir, task_id="t1", sequence=1)
    result = runner.invoke(
        app,
        [
            "answer",
            "t1",
            "1",
            "--queue",
            str(queue_dir),
            "--answers-file",
            str(queue_dir / "does-not-exist.json"),
        ],
    )
    assert result.exit_code == 2


def test_answer_invalid_json(runner: CliRunner, queue_dir: Path) -> None:
    _seed_request(queue_dir, task_id="t1", sequence=1)
    result = runner.invoke(
        app,
        [
            "answer",
            "t1",
            "1",
            "--queue",
            str(queue_dir),
            "--answers",
            "{not json}",
        ],
    )
    assert result.exit_code == 2


def test_answer_not_a_list(runner: CliRunner, queue_dir: Path) -> None:
    _seed_request(queue_dir, task_id="t1", sequence=1)
    result = runner.invoke(
        app,
        [
            "answer",
            "t1",
            "1",
            "--queue",
            str(queue_dir),
            "--answers",
            '{"id": "encoding", "value": "A"}',  # object, not array
        ],
    )
    assert result.exit_code == 2


def test_answer_entry_missing_keys(runner: CliRunner, queue_dir: Path) -> None:
    _seed_request(queue_dir, task_id="t1", sequence=1)
    result = runner.invoke(
        app,
        [
            "answer",
            "t1",
            "1",
            "--queue",
            str(queue_dir),
            "--answers",
            '[{"id": "encoding"}]',  # no value
        ],
    )
    assert result.exit_code == 2


def test_answer_entry_non_object(runner: CliRunner, queue_dir: Path) -> None:
    _seed_request(queue_dir, task_id="t1", sequence=1)
    result = runner.invoke(
        app,
        [
            "answer",
            "t1",
            "1",
            "--queue",
            str(queue_dir),
            "--answers",
            '["just a string"]',
        ],
    )
    assert result.exit_code == 2


def test_answer_via_answers_file(runner: CliRunner, queue_dir: Path) -> None:
    _seed_request(queue_dir, task_id="t1", sequence=1)
    af = queue_dir / "answers.json"
    af.write_text('[{"id": "encoding", "value": "A"}]', encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "answer",
            "t1",
            "1",
            "--queue",
            str(queue_dir),
            "--answers-file",
            str(af),
            "--notes",
            "operator chose recommended option",
        ],
    )
    assert result.exit_code == 0
    # Response file must have been written.
    from claude_task_runner.queue.sidecar import response_path

    assert response_path(queue_dir.resolve(), "t1", 1).exists()
