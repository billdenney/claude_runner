"""Additional coverage for SidecarStore paths not exercised in the main test file."""

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


def _req(
    *, seq: int = 1, options: tuple[str, ...] = ("A", "B"), multi: bool = False
) -> InteractionRequest:
    return InteractionRequest(
        task_id="t1",
        sequence=seq,
        created_at=datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC),
        summary="s",
        questions=[
            Question(
                id="q1",
                prompt="p",
                options=[Option(value=v, label=f"l-{v}") for v in options],
                multi_select=multi,
            )
        ],
    )


def test_root_property_returns_configured_path(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    assert store.root == tmp_path


def test_list_request_sequences_empty_when_dir_missing(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    assert store.list_request_sequences("never") == []


def test_list_response_sequences_filters_to_responses_only(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    req = _req(seq=1)
    store.write_request(req)
    resp = InteractionResponse(
        task_id="t1",
        sequence=1,
        responded_at=datetime(2026, 4, 19, 13, 0, tzinfo=UTC),
        answers=[Answer(id="q1", value="A")],
    )
    store.write_response(resp, request=req)
    assert store.list_request_sequences("t1") == [1]
    assert store.list_response_sequences("t1") == [1]


def test_next_request_sequence_starts_at_1(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    assert store.next_request_sequence("fresh") == 1


def test_iter_interactions_returns_one_per_sequence(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    store.write_request(_req(seq=1))
    store.write_request(_req(seq=2))
    interactions = store.iter_interactions("t1")
    assert [i.request.sequence for i in interactions] == [1, 2]
    assert all(i.response is None for i in interactions)


def test_find_open_request_when_no_dir(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    assert store.find_open_request("nonexistent") is None


def test_list_awaiting_task_ids_empty_when_root_missing(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path / "does_not_exist")
    assert store.list_awaiting_task_ids() == []


def test_list_awaiting_task_ids_skips_non_directory_entries(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    store.write_request(_req(seq=1))
    # Put a stray file next to the task dir — should be ignored.
    (tmp_path / "stray.txt").write_text("noise")
    assert store.list_awaiting_task_ids() == ["t1"]


def test_load_response_returns_none_when_file_missing(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    store.write_request(_req(seq=1))
    assert store.load_response("t1", 1) is None


def test_load_response_rejects_wrong_task_id(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    # Write response file manually with a mismatched task_id.
    path = store.response_path("t1", 1)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "task_id": "not-t1",
                "sequence": 1,
                "responded_at": "2026-04-19T13:00:00+00:00",
                "answers": [],
                "state": "cancelled",
            }
        )
    )
    with pytest.raises(SidecarValidationError):
        store.load_response("t1", 1)


def test_load_response_rejects_wrong_sequence(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    path = store.response_path("t1", 1)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "task_id": "t1",
                "sequence": 99,
                "responded_at": "2026-04-19T13:00:00+00:00",
                "answers": [],
                "state": "cancelled",
            }
        )
    )
    with pytest.raises(SidecarValidationError):
        store.load_response("t1", 1)


def test_load_request_rejects_wrong_sequence(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    path = store.request_path("t1", 1)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "task_id": "t1",
                "sequence": 99,
                "created_at": "2026-04-19T12:00:00+00:00",
                "summary": "s",
                "questions": [],
                "state": "open",
            }
        )
    )
    with pytest.raises(SidecarValidationError):
        store.load_request("t1", 1)


def test_write_request_rejects_empty_task_id(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    req = InteractionRequest(
        task_id="",
        sequence=1,
        created_at=datetime.now(tz=UTC),
        summary="s",
        questions=[Question(id="q1", prompt="p", options=[Option(value="A", label="A")])],
    )
    with pytest.raises(SidecarValidationError):
        store.write_request(req)


def test_validate_request_rejects_empty_question_id(tmp_path: Path) -> None:
    """Constructing a Question with '' id is allowed at the dataclass level;
    the store validates on write."""
    store = SidecarStore(tmp_path)
    # Build a request with an empty-id question. Question is a frozen dataclass
    # that doesn't validate id itself, but the store does.
    req = InteractionRequest(
        task_id="t1",
        sequence=1,
        created_at=datetime(2026, 4, 19, tzinfo=UTC),
        summary="s",
        questions=[Question(id="", prompt="p", options=[Option(value="A", label="A")])],
    )
    with pytest.raises(SidecarValidationError):
        store.write_request(req)


def test_write_request_rejects_sequence_zero(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    req = InteractionRequest(
        task_id="t1",
        sequence=0,
        created_at=datetime(2026, 4, 19, tzinfo=UTC),
        summary="s",
        questions=[Question(id="q", prompt="p", options=[Option(value="A", label="A")])],
    )
    with pytest.raises(SidecarValidationError):
        store.write_request(req)


def test_write_request_rejects_empty_questions(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    req = InteractionRequest(
        task_id="t1",
        sequence=1,
        created_at=datetime(2026, 4, 19, tzinfo=UTC),
        summary="s",
        questions=[],
    )
    with pytest.raises(SidecarValidationError):
        store.write_request(req)


def test_write_response_rejects_mismatched_task_id(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    req = _req(seq=1)
    store.write_request(req)
    resp = InteractionResponse(
        task_id="other",
        sequence=1,
        responded_at=datetime(2026, 4, 19, 13, 0, tzinfo=UTC),
        answers=[Answer(id="q1", value="A")],
    )
    with pytest.raises(SidecarValidationError):
        store.write_response(resp, request=req)


def test_write_response_rejects_answered_with_no_answers(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    req = _req(seq=1)
    store.write_request(req)
    resp = InteractionResponse(
        task_id="t1",
        sequence=1,
        responded_at=datetime(2026, 4, 19, 13, 0, tzinfo=UTC),
        answers=[],
        state=RequestState.ANSWERED,
    )
    with pytest.raises(SidecarValidationError):
        store.write_response(resp, request=req)


def test_write_response_loads_request_when_not_supplied(tmp_path: Path) -> None:
    """If caller omits the ``request`` keyword, the store loads it from disk."""
    store = SidecarStore(tmp_path)
    req = _req(seq=1)
    store.write_request(req)
    resp = InteractionResponse(
        task_id="t1",
        sequence=1,
        responded_at=datetime(2026, 4, 19, 13, 0, tzinfo=UTC),
        answers=[Answer(id="q1", value="A")],
    )
    store.write_response(resp)  # no `request=` kwarg
    loaded = store.load_response("t1", 1)
    assert loaded is not None


def test_write_response_multi_select_validates_each_value(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    req = _req(seq=1, options=("A", "B", "C"), multi=True)
    store.write_request(req)
    bad = InteractionResponse(
        task_id="t1",
        sequence=1,
        responded_at=datetime(2026, 4, 19, 13, 0, tzinfo=UTC),
        answers=[Answer(id="q1", value=["A", "Z"])],
    )
    with pytest.raises(SidecarValidationError):
        store.write_response(bad, request=req)


def test_write_response_multi_select_requires_list_value(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    req = _req(seq=1, options=("A", "B"), multi=True)
    store.write_request(req)
    bad = InteractionResponse(
        task_id="t1",
        sequence=1,
        responded_at=datetime(2026, 4, 19, 13, 0, tzinfo=UTC),
        answers=[Answer(id="q1", value="A")],  # should be ["A"]
    )
    with pytest.raises(SidecarValidationError):
        store.write_response(bad, request=req)


def test_cancel_request_is_idempotent_for_resp_state(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    req = _req(seq=1)
    store.write_request(req)
    store.cancel_request("t1", 1, notes="first")
    # Call it again — should not crash; overwrites response file with new notes.
    store.cancel_request("t1", 1, notes="second")
    resp = store.load_response("t1", 1)
    assert resp is not None and resp.notes == "second"


def test_load_interaction_returns_request_only_when_no_response(tmp_path: Path) -> None:
    store = SidecarStore(tmp_path)
    store.write_request(_req(seq=1))
    inter = store.load_interaction("t1", 1)
    assert inter.request.sequence == 1
    assert inter.response is None
