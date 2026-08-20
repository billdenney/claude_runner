"""Tests for queue/sidecar.py — sidecar protocol storage."""

from __future__ import annotations

import json
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
    open_sidecars,
    read_request,
    read_response,
    request_outstanding,
    request_path,
    response_path,
    sidecar_dir_for,
    write_request,
    write_response,
)
from claude_task_runner.queue.store import QueueIOError, QueueSchemaError


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


def _three_question_request(task_id: str = "001", sequence: int = 1) -> SidecarRequest:
    return SidecarRequest(
        task_id=task_id,
        sequence=sequence,
        created_at=datetime(2026, 8, 9, 9, 0, tzinfo=UTC),
        summary="Three decisions before extraction can continue",
        context="Source is ambiguous in three separate places.",
        questions=[
            SidecarQuestion(
                id=qid,
                prompt=f"Decision {qid}?",
                options=[
                    SidecarOption(value="A", label="Option A"),
                    SidecarOption(value="B", label="Option B"),
                ],
                recommended="A",
            )
            for qid in ("q1", "q2", "q3")
        ],
    )


def _write_answers(queue_dir: Path, task_id: str, sequence: int, ids: list[str]) -> None:
    write_response(
        queue_dir,
        SidecarResponse(
            task_id=task_id,
            sequence=sequence,
            responded_at=datetime(2026, 8, 9, 10, 0, tzinfo=UTC),
            answers=[SidecarAnswer(id=qid, value="A") for qid in ids],
        ),
    )


class TestPerQuestionOpenness:
    """Openness is a per-QUESTION property, not per-file.

    The regression these lock in: a response file that answers only some of
    a request's questions used to close the request outright, hiding every
    remaining question from ``sidecar list``, the readiness gate and the
    operator's answering skill alike.
    """

    def test_partial_response_leaves_request_open(self, queue_dir: Path) -> None:
        write_request(queue_dir, _three_question_request())
        _write_answers(queue_dir, "001", 1, ["q1"])

        items = list(open_sidecars(queue_dir))
        assert len(items) == 1
        assert items[0].task_id == "001"
        assert items[0].sequence == 1
        assert items[0].outstanding == ("q2", "q3")
        assert items[0].answered == ("q1",)
        assert items[0].partial is True
        assert items[0].error is None

    def test_partial_response_visible_to_runner_gates(self, queue_dir: Path) -> None:
        # list_open_sidecars is what readiness / orchestrator / dispatcher
        # consume, so the per-question test has to reach them too.
        write_request(queue_dir, _three_question_request())
        _write_answers(queue_dir, "001", 1, ["q1"])
        assert [(tid, seq) for tid, seq, _ in list_open_sidecars(queue_dir)] == [("001", 1)]

    def test_out_of_order_answers_still_close_the_request(self, queue_dir: Path) -> None:
        write_request(queue_dir, _three_question_request())
        _write_answers(queue_dir, "001", 1, ["q3", "q1", "q2"])
        assert list(open_sidecars(queue_dir)) == []

    def test_fully_answered_request_is_closed(self, queue_dir: Path) -> None:
        write_request(queue_dir, _three_question_request())
        _write_answers(queue_dir, "001", 1, ["q1", "q2", "q3"])
        assert list(open_sidecars(queue_dir)) == []

    def test_no_response_leaves_every_question_outstanding(self, queue_dir: Path) -> None:
        write_request(queue_dir, _three_question_request())
        items = list(open_sidecars(queue_dir))
        assert len(items) == 1
        assert items[0].outstanding == ("q1", "q2", "q3")
        assert items[0].answered == ()
        assert items[0].response_path is None
        # No response file at all is not the "partial" case.
        assert items[0].partial is False

    def test_answers_for_unasked_ids_do_not_close_asked_ones(self, queue_dir: Path) -> None:
        # A typo'd answer id ("q22") must not be credited against q2.
        write_request(queue_dir, _three_question_request())
        _write_answers(queue_dir, "001", 1, ["q1", "q22", "q3"])
        items = list(open_sidecars(queue_dir))
        assert len(items) == 1
        assert items[0].outstanding == ("q2",)


class TestNotificationRequests:
    """A ``file_and_exit`` request asks nothing; a response closes it."""

    def _notification(self, queue_dir: Path) -> None:
        write_request(
            queue_dir,
            SidecarRequest(
                task_id="001",
                sequence=1,
                created_at=datetime(2026, 8, 9, 9, 0, tzinfo=UTC),
                summary="Filed for the record; no decision needed",
                context="Recording why this task stopped.",
                questions=[],
            ),
        )

    def test_open_until_acknowledged(self, queue_dir: Path) -> None:
        self._notification(queue_dir)
        items = list(open_sidecars(queue_dir))
        assert len(items) == 1
        assert items[0].outstanding == ()

    def test_closed_by_presence_of_a_response(self, queue_dir: Path) -> None:
        self._notification(queue_dir)
        _write_answers(queue_dir, "001", 1, [])
        assert list(open_sidecars(queue_dir)) == []


class TestUndecidableSidecarsFailLoud:
    """An unreadable sidecar is reported OPEN, never silently answered.

    A request whose asked ids cannot be determined carries no evidence that
    it was answered. Closing it on the strength of a response file being
    present is how questions went missing in the first place, so the
    ambiguous case surfaces with ``error`` set and the operator can repair
    a sidecar they can actually see.
    """

    def test_unparseable_request_with_response_is_open(self, queue_dir: Path) -> None:
        sidecar_dir_for(queue_dir, "001")
        request_path(queue_dir, "001", 1).write_text("{not json")
        _write_answers(queue_dir, "001", 1, ["q1"])
        items = list(open_sidecars(queue_dir))
        assert len(items) == 1
        assert items[0].error is not None
        assert "invalid JSON" in items[0].error

    def test_question_without_an_id_is_open(self, queue_dir: Path) -> None:
        sidecar_dir_for(queue_dir, "001")
        request_path(queue_dir, "001", 1).write_text(
            json.dumps({"task_id": "001", "questions": [{"prompt": "Which?", "options": []}]})
        )
        _write_answers(queue_dir, "001", 1, ["q1"])
        items = list(open_sidecars(queue_dir))
        assert len(items) == 1
        assert items[0].error is not None
        assert "no usable id" in items[0].error

    def test_response_without_answers_array_is_open(self, queue_dir: Path) -> None:
        write_request(queue_dir, _three_question_request())
        response_path(queue_dir, "001", 1).write_text(json.dumps({"task_id": "001"}))
        items = list(open_sidecars(queue_dir))
        assert len(items) == 1
        assert items[0].error is not None
        assert "no 'answers'" in items[0].error


class TestLegacyRequestShapes:
    """Schema drift that says nothing about answeredness must not decide it.

    Openness reads only ``questions[].id`` and ``answers[].id`` from raw
    JSON. Validating the whole payload instead would misclassify hundreds
    of live requests over cosmetic drift (a missing ``created_at``, a
    per-answer ``notes`` key) that carries no information about whether the
    operator answered.
    """

    def test_v1_flat_question_is_answerable_as_q1(self, queue_dir: Path) -> None:
        sidecar_dir_for(queue_dir, "001")
        request_path(queue_dir, "001", 1).write_text(
            json.dumps(
                {
                    "task_id": "001",
                    "sequence": 1,
                    "summary": "legacy",
                    "question": "Which encoding?",
                    "options": [{"id": "A", "label": "A"}],
                }
            )
        )
        assert [item.outstanding for item in open_sidecars(queue_dir)] == [("q1",)]
        _write_answers(queue_dir, "001", 1, ["q1"])
        assert list(open_sidecars(queue_dir)) == []

    def test_legacy_question_id_spelling_is_honoured(self, queue_dir: Path) -> None:
        sidecar_dir_for(queue_dir, "001")
        request_path(queue_dir, "001", 1).write_text(
            json.dumps(
                {
                    "task_id": "001",
                    "questions": [
                        {"question_id": "q1", "prompt": "First?"},
                        {"question_id": "q2", "prompt": "Second?"},
                    ],
                }
            )
        )
        _write_answers(queue_dir, "001", 1, ["q1"])
        items = list(open_sidecars(queue_dir))
        assert len(items) == 1
        assert items[0].outstanding == ("q2",)

    def test_request_missing_created_at_still_accounted(self, queue_dir: Path) -> None:
        # Fails strict SidecarRequest validation; says nothing about answers.
        sidecar_dir_for(queue_dir, "001")
        request_path(queue_dir, "001", 1).write_text(
            json.dumps({"task_id": "001", "questions": [{"id": "q1"}, {"id": "q2"}]})
        )
        _write_answers(queue_dir, "001", 1, ["q1", "q2"])
        assert list(open_sidecars(queue_dir)) == []

    def test_answer_carrying_extra_keys_still_counts(self, queue_dir: Path) -> None:
        write_request(queue_dir, _three_question_request())
        response_path(queue_dir, "001", 1).write_text(
            json.dumps(
                {
                    "task_id": "001",
                    "sequence": 1,
                    "answers": [
                        {"id": qid, "value": "A", "notes": "operator note"}
                        for qid in ("q1", "q2", "q3")
                    ],
                }
            )
        )
        assert list(open_sidecars(queue_dir)) == []


class TestRequestOutstanding:
    def test_reports_asked_and_outstanding(self, queue_dir: Path) -> None:
        write_request(queue_dir, _three_question_request())
        _write_answers(queue_dir, "001", 1, ["q2"])
        outstanding, asked, resp = request_outstanding(queue_dir, "001", 1)
        assert outstanding == ["q1", "q3"]
        assert asked == ["q1", "q2", "q3"]
        assert resp == response_path(queue_dir, "001", 1)

    def test_missing_request_raises(self, queue_dir: Path) -> None:
        with pytest.raises(QueueIOError, match="request not found"):
            request_outstanding(queue_dir, "nope", 1)


class TestUnusableAnswerEntries:
    """An unnameable ANSWER is decidable; an unnameable QUESTION is not.

    A junk answer entry credits nothing, so skipping it can only leave a
    question outstanding -- the safe direction. Raising instead would force
    an operator to repair a file whose accounting was never in doubt.
    """

    def test_id_less_answer_entry_is_skipped_not_fatal(self, queue_dir: Path) -> None:
        write_request(queue_dir, _three_question_request())
        response_path(queue_dir, "001", 1).write_text(
            json.dumps(
                {
                    "task_id": "001",
                    "answers": [
                        {"id": "", "value": "A"},
                        {"id": "q1", "value": "A"},
                        {"id": "q2", "value": "A"},
                    ],
                }
            )
        )
        items = list(open_sidecars(queue_dir))
        assert len(items) == 1
        assert items[0].error is None
        assert items[0].outstanding == ("q3",)

    def test_only_junk_answers_leaves_everything_outstanding(self, queue_dir: Path) -> None:
        write_request(queue_dir, _three_question_request())
        response_path(queue_dir, "001", 1).write_text(
            json.dumps({"task_id": "001", "answers": [{"value": "A"}, "nonsense"]})
        )
        items = list(open_sidecars(queue_dir))
        assert len(items) == 1
        assert items[0].outstanding == ("q1", "q2", "q3")
        assert items[0].error is None
