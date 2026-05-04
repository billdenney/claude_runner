"""Tests for queue/sidecar.py — sidecar protocol storage."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_task_runner.queue.schema import (
    SidecarAnswer,
    SidecarOption,
    SidecarQuestion,
    SidecarRequest,
    SidecarResponse,
)
from claude_task_runner.queue.sidecar import (
    list_open_sidecars,
    next_sequence,
    read_request,
    read_response,
    request_path,
    response_path,
    sidecar_dir_for,
    write_request,
    write_response,
)
from claude_task_runner.queue.store import QueueSchemaError


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "myqueue"
    qd.mkdir()
    return qd


def _request(task_id: str, sequence: int = 1) -> SidecarRequest:
    return SidecarRequest(
        task_id=task_id,
        sequence=sequence,
        created_at=datetime(2026, 5, 3, 18, 0, tzinfo=UTC),
        summary="Ambiguity in covariate encoding",
        context="Both A and B encodings appear in the source",
        questions=[
            SidecarQuestion(
                id="encoding",
                prompt="Which encoding to use?",
                options=[
                    SidecarOption(value="A", label="Encoding A"),
                    SidecarOption(value="B", label="Encoding B"),
                ],
                recommended="A",
            ),
        ],
    )


class TestPaths:
    def test_request_path_format(self, queue_dir: Path) -> None:
        p = request_path(queue_dir, "001-foo", 7)
        assert p.name == "request-007.json"
        assert "001-foo" in str(p)

    def test_response_path_format(self, queue_dir: Path) -> None:
        p = response_path(queue_dir, "001-foo", 7)
        assert p.name == "response-007.json"

    def test_sidecar_dir_creates(self, queue_dir: Path) -> None:
        d = sidecar_dir_for(queue_dir, "abc")
        assert d.is_dir()


class TestSequence:
    def test_first_sequence_is_one(self, queue_dir: Path) -> None:
        assert next_sequence(queue_dir, "001-foo") == 1

    def test_increments_past_existing(self, queue_dir: Path) -> None:
        write_request(queue_dir, _request("001", sequence=1))
        write_request(queue_dir, _request("001", sequence=2))
        assert next_sequence(queue_dir, "001") == 3

    def test_isolated_per_task(self, queue_dir: Path) -> None:
        write_request(queue_dir, _request("001", sequence=1))
        write_request(queue_dir, _request("001", sequence=2))
        # Second task starts fresh.
        assert next_sequence(queue_dir, "002") == 1

    def test_handles_gaps(self, queue_dir: Path) -> None:
        # Files 1, 3 exist; next should be 4 not 2 (no gap-filling).
        write_request(queue_dir, _request("001", sequence=1))
        write_request(queue_dir, _request("001", sequence=3))
        assert next_sequence(queue_dir, "001") == 4


class TestRoundTrip:
    def test_write_read_request(self, queue_dir: Path) -> None:
        req = _request("001", sequence=1)
        path = write_request(queue_dir, req)
        assert path.exists()
        loaded = read_request(path)
        assert loaded == req

    def test_write_read_response(self, queue_dir: Path) -> None:
        when = datetime(2026, 5, 3, 19, 0, tzinfo=UTC)
        resp = SidecarResponse(
            task_id="001",
            sequence=1,
            responded_at=when,
            answers=[SidecarAnswer(id="encoding", value="A")],
            notes="quick pick",
        )
        path = write_response(queue_dir, resp)
        loaded = read_response(path)
        assert loaded == resp
        assert loaded.notes == "quick pick"


class TestListOpen:
    def test_empty_when_no_sidecar_dir(self, queue_dir: Path) -> None:
        assert list(list_open_sidecars(queue_dir)) == []

    def test_lists_only_unanswered(self, queue_dir: Path) -> None:
        write_request(queue_dir, _request("001", sequence=1))
        write_request(queue_dir, _request("001", sequence=2))
        write_request(queue_dir, _request("002", sequence=1))
        # Answer one of them
        when = datetime(2026, 5, 3, 19, 0, tzinfo=UTC)
        write_response(
            queue_dir,
            SidecarResponse(
                task_id="001",
                sequence=1,
                responded_at=when,
                answers=[SidecarAnswer(id="encoding", value="A")],
            ),
        )
        open_items = list(list_open_sidecars(queue_dir))
        assert {(tid, seq) for tid, seq, _ in open_items} == {
            ("001", 2),
            ("002", 1),
        }

    def test_results_sorted(self, queue_dir: Path) -> None:
        # Insert in non-sorted order
        for task_id, seq in (("002", 2), ("001", 3), ("002", 1), ("001", 1)):
            write_request(queue_dir, _request(task_id, sequence=seq))
        open_items = [(tid, seq) for tid, seq, _ in list_open_sidecars(queue_dir)]
        assert open_items == [
            ("001", 1),
            ("001", 3),
            ("002", 1),
            ("002", 2),
        ]


class TestErrors:
    def test_invalid_json_raises(self, queue_dir: Path) -> None:
        sidecar_dir_for(queue_dir, "001")
        path = request_path(queue_dir, "001", 1)
        path.write_text("{not json")
        with pytest.raises(QueueSchemaError, match="invalid JSON"):
            read_request(path)

    def test_validation_failure_surfaces(self, queue_dir: Path) -> None:
        sidecar_dir_for(queue_dir, "001")
        path = request_path(queue_dir, "001", 1)
        path.write_text('{"task_id": "001"}')
        with pytest.raises(QueueSchemaError):
            read_request(path)

    def test_write_request_with_wrong_schema_version_rejected(self, queue_dir: Path) -> None:
        # Construct via dict to force wrong version
        when = datetime(2026, 5, 3, 18, 0, tzinfo=UTC)
        bad = SidecarRequest.model_construct(
            schema_version=999,
            task_id="001",
            sequence=1,
            created_at=when,
            summary="s",
            context="c",
            questions=[],
            state="pending",
        )
        with pytest.raises(QueueSchemaError, match="schema_version=999"):
            write_request(queue_dir, bad)
