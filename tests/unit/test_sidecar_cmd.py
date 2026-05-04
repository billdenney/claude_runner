"""Tests for cli.sidecar_cmd — list / show / answer."""

from __future__ import annotations

import json
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
from claude_task_runner.queue.sidecar import (
    response_path,
    write_request,
)
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
    multi_select: bool = False,
    allow_free_text: bool = False,
) -> SidecarRequest:
    req = SidecarRequest(
        task_id=task_id,
        sequence=sequence,
        created_at=datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
        summary=summary,
        context="Both A and B options visible in source.",
        questions=[
            SidecarQuestion(
                id="encoding",
                prompt="Which encoding?",
                options=[
                    SidecarOption(value="A", label="Encoding A"),
                    SidecarOption(value="B", label="Encoding B", description="alt"),
                ],
                recommended="A",
                multi_select=multi_select,
                allow_free_text=allow_free_text,
            ),
        ],
    )
    write_request(qd, req)
    return req


class TestList:
    def test_empty(self, runner: CliRunner, queue_dir: Path) -> None:
        result = runner.invoke(app, ["list", "--queue", str(queue_dir), "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {"sidecars": []}

    def test_lists_open(self, runner: CliRunner, queue_dir: Path) -> None:
        _seed_request(queue_dir, task_id="001", sequence=1)
        _seed_request(queue_dir, task_id="002", sequence=1)
        result = runner.invoke(app, ["list", "--queue", str(queue_dir), "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        ids = [s["task_id"] for s in payload["sidecars"]]
        assert ids == ["001", "002"]


class TestShow:
    def test_full_payload(self, runner: CliRunner, queue_dir: Path) -> None:
        _seed_request(queue_dir, task_id="001", sequence=1)
        result = runner.invoke(
            app,
            ["show", "001", "1", "--queue", str(queue_dir), "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["task_id"] == "001"
        assert payload["questions"][0]["recommended"] == "A"
        assert len(payload["questions"][0]["options"]) == 2

    def test_missing_returns_error(self, runner: CliRunner, queue_dir: Path) -> None:
        result = runner.invoke(
            app,
            ["show", "no-such", "1", "--queue", str(queue_dir), "--json"],
        )
        assert result.exit_code == 1
        assert "not found" in result.stderr or "not found" in result.stdout


class TestAnswer:
    def test_writes_response(self, runner: CliRunner, queue_dir: Path) -> None:
        _seed_request(queue_dir, task_id="001", sequence=1)
        answers = json.dumps([{"id": "encoding", "value": "A"}])
        result = runner.invoke(
            app,
            [
                "answer",
                "001",
                "1",
                "--queue",
                str(queue_dir),
                "--answers",
                answers,
                "--notes",
                "operator chose recommended",
            ],
        )
        assert result.exit_code == 0, result.stdout
        path = response_path(queue_dir, "001", 1)
        assert path.exists()
        body = json.loads(path.read_text())
        assert body["answers"][0]["value"] == "A"
        assert body["notes"] == "operator chose recommended"
        assert body["state"] == "answered"

    def test_multi_select_value_is_list(self, runner: CliRunner, queue_dir: Path) -> None:
        _seed_request(queue_dir, task_id="001", sequence=1, multi_select=True)
        answers = json.dumps([{"id": "encoding", "value": ["A", "B"]}])
        result = runner.invoke(
            app,
            [
                "answer",
                "001",
                "1",
                "--queue",
                str(queue_dir),
                "--answers",
                answers,
            ],
        )
        assert result.exit_code == 0, result.stdout
        body = json.loads(response_path(queue_dir, "001", 1).read_text())
        assert body["answers"][0]["value"] == ["A", "B"]

    def test_answers_from_file(
        self,
        runner: CliRunner,
        queue_dir: Path,
        tmp_path: Path,
    ) -> None:
        _seed_request(queue_dir, task_id="001", sequence=1)
        answers_file = tmp_path / "ans.json"
        answers_file.write_text(json.dumps([{"id": "encoding", "value": "B"}]))
        result = runner.invoke(
            app,
            [
                "answer",
                "001",
                "1",
                "--queue",
                str(queue_dir),
                "--answers-file",
                str(answers_file),
            ],
        )
        assert result.exit_code == 0, result.stdout

    def test_invalid_json_rejected(self, runner: CliRunner, queue_dir: Path) -> None:
        _seed_request(queue_dir, task_id="001", sequence=1)
        result = runner.invoke(
            app,
            [
                "answer",
                "001",
                "1",
                "--queue",
                str(queue_dir),
                "--answers",
                "{not json",
            ],
        )
        assert result.exit_code != 0
        assert "invalid JSON" in (result.stdout + result.stderr)

    def test_must_supply_one_answers_source(self, runner: CliRunner, queue_dir: Path) -> None:
        _seed_request(queue_dir, task_id="001", sequence=1)
        result = runner.invoke(
            app,
            ["answer", "001", "1", "--queue", str(queue_dir)],
        )
        assert result.exit_code != 0
        assert "exactly one" in (result.stdout + result.stderr)

    def test_must_be_array(self, runner: CliRunner, queue_dir: Path) -> None:
        _seed_request(queue_dir, task_id="001", sequence=1)
        result = runner.invoke(
            app,
            [
                "answer",
                "001",
                "1",
                "--queue",
                str(queue_dir),
                "--answers",
                '{"id":"encoding","value":"A"}',
            ],
        )
        assert result.exit_code != 0
        assert "array" in (result.stdout + result.stderr)
