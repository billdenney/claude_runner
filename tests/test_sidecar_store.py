"""Tests for the sidecar JSON store."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_runner.sidecar.schema import (
    Answer,
    InteractionRequest,
    InteractionResponse,
    Option,
    Question,
    RequestState,
)
from claude_runner.sidecar.store import SidecarStore, SidecarValidationError


def _make_request(
    *,
    task_id: str = "t1",
    seq: int = 1,
    state: RequestState = RequestState.OPEN,
    question_id: str = "q1",
    options: tuple[str, ...] = ("A", "B"),
) -> InteractionRequest:
    return InteractionRequest(
        task_id=task_id,
        sequence=seq,
        created_at=datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC),
        summary=f"summary for {task_id} seq {seq}",
        questions=[
            Question(
                id=question_id,
                prompt="pick one",
                options=[Option(value=v, label=f"label {v}") for v in options],
            )
        ],
        state=state,
    )


def test_round_trip_request(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    req = _make_request()
    store.write_request(req)

    loaded = store.load_request("t1", 1)
    assert loaded.task_id == "t1"
    assert loaded.sequence == 1
    assert loaded.state is RequestState.OPEN
    assert len(loaded.questions) == 1
    assert loaded.questions[0].options[0].value == "A"


def test_write_request_is_atomic(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    req = _make_request()
    path = store.write_request(req)
    # Atomic write should NOT leave a visible .tmp sibling.
    leftover = [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftover == []


def test_next_request_sequence_monotonic(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    assert store.next_request_sequence("t1") == 1
    store.write_request(_make_request(seq=1))
    assert store.next_request_sequence("t1") == 2
    store.write_request(_make_request(seq=2))
    assert store.next_request_sequence("t1") == 3


def test_write_response_validates_and_closes_request(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    req = _make_request()
    store.write_request(req)

    resp = InteractionResponse(
        task_id="t1",
        sequence=1,
        responded_at=datetime(2026, 4, 19, 13, 0, 0, tzinfo=UTC),
        answers=[Answer(id="q1", value="A")],
    )
    store.write_response(resp, request=req)

    # Round-trip check.
    loaded_resp = store.load_response("t1", 1)
    assert loaded_resp is not None
    assert loaded_resp.state is RequestState.ANSWERED
    assert loaded_resp.answers[0].value == "A"

    # The request file should also be flipped to ANSWERED on disk.
    re_req = store.load_request("t1", 1)
    assert re_req.state is RequestState.ANSWERED


def test_write_response_rejects_unknown_answer_id(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    req = _make_request()
    store.write_request(req)
    resp = InteractionResponse(
        task_id="t1",
        sequence=1,
        responded_at=datetime(2026, 4, 19, 13, 0, 0, tzinfo=UTC),
        answers=[Answer(id="unknown_question", value="A")],
    )
    with pytest.raises(SidecarValidationError):
        store.write_response(resp, request=req)


def test_write_response_rejects_value_outside_options(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    req = _make_request(options=("A", "B"))
    store.write_request(req)
    resp = InteractionResponse(
        task_id="t1",
        sequence=1,
        responded_at=datetime(2026, 4, 19, 13, 0, 0, tzinfo=UTC),
        answers=[Answer(id="q1", value="Z")],  # Z is not A or B
    )
    with pytest.raises(SidecarValidationError):
        store.write_response(resp, request=req)


def test_write_request_rejects_duplicate_question_ids(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    req = InteractionRequest(
        task_id="t1",
        sequence=1,
        created_at=datetime(2026, 4, 19, tzinfo=UTC),
        summary="dup",
        questions=[
            Question(id="same", prompt="a?", options=[Option(value="x", label="x")]),
            Question(id="same", prompt="b?", options=[Option(value="y", label="y")]),
        ],
    )
    with pytest.raises(SidecarValidationError):
        store.write_request(req)


def test_write_request_rejects_recommended_not_in_options(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    req = InteractionRequest(
        task_id="t1",
        sequence=1,
        created_at=datetime(2026, 4, 19, tzinfo=UTC),
        summary="bad recommended",
        questions=[
            Question(
                id="q1",
                prompt="pick",
                options=[Option(value="A", label="A")],
                recommended="Z",  # not in options
            )
        ],
    )
    with pytest.raises(SidecarValidationError):
        store.write_request(req)


def test_find_open_request_returns_newest_only_if_open(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    req1 = _make_request(seq=1, state=RequestState.ANSWERED)
    req2 = _make_request(seq=2, state=RequestState.OPEN)
    store.write_request(req1)
    store.write_request(req2)
    open_req = store.find_open_request("t1")
    assert open_req is not None
    assert open_req.sequence == 2


def test_find_open_request_returns_none_when_newest_is_answered(tmp_path: Path) -> None:
    """If the newest request is already answered we don't look at older ones."""
    store = SidecarStore(tmp_path)
    req1 = _make_request(seq=1, state=RequestState.OPEN)
    req2 = _make_request(seq=2, state=RequestState.ANSWERED)
    store.write_request(req1)
    store.write_request(req2)
    assert store.find_open_request("t1") is None


def test_cancel_request_writes_cancelled_state_on_both_files(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    req = _make_request()
    store.write_request(req)
    store.cancel_request("t1", 1, notes="operator said no")

    r = store.load_request("t1", 1)
    assert r.state is RequestState.CANCELLED
    resp = store.load_response("t1", 1)
    assert resp is not None
    assert resp.state is RequestState.CANCELLED
    assert resp.notes == "operator said no"


def test_list_awaiting_task_ids_reports_only_open(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    # tA has an open request
    store.write_request(_make_request(task_id="tA", seq=1))
    # tB has an already-answered newest request
    store.write_request(_make_request(task_id="tB", seq=1, state=RequestState.ANSWERED))
    # tC is missing (no sidecar dir)
    assert sorted(store.list_awaiting_task_ids()) == ["tA"]


def test_load_request_rejects_mismatched_task_id(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    path = store.request_path("t1", 1)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "task_id": "wrong",
                "sequence": 1,
                "created_at": "2026-04-19T12:00:00+00:00",
                "summary": "x",
                "questions": [],
                "state": "open",
            }
        )
    )
    with pytest.raises(SidecarValidationError):
        store.load_request("t1", 1)


def test_load_request_rejects_non_object_payload(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    path = store.request_path("t1", 1)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(["not", "a", "dict"]))
    with pytest.raises(SidecarValidationError):
        store.load_request("t1", 1)
