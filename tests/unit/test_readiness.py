"""Tests for mechanical readiness gates (ADR-0030).

Covers the schema (``ReadinessRequirement`` validation + ``Task.requires``
round-trip) and the evaluator (``runner.readiness.unmet_requirements`` /
``is_ready``). The selector integration (``_eligible_candidates`` skips a task
with unmet requirements without dispatching, and re-admits it once satisfied)
lives in ``test_orchestrator_sidecar_resume.py`` alongside the other
selector-gating tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from claude_task_runner.queue.schema import ReadinessRequirement, Task
from claude_task_runner.queue.sidecar import write_request
from claude_task_runner.queue.store import queue_runtime_dir
from claude_task_runner.runner.readiness import (
    HOLD_REASON_PREFIX,
    hold_reason,
    is_hold_reason,
    is_ready,
    unmet_requirements,
)


def _task(**overrides: object) -> Task:
    payload: dict[str, object] = {"id": "t1", "title": "T", "prompt": "do it"}
    payload.update(overrides)
    return Task.model_validate(payload)


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    queue_runtime_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    return tmp_path


# --- schema validation --------------------------------------------------


def test_file_requirement_needs_path() -> None:
    with pytest.raises(ValidationError, match="requires a non-empty 'path'"):
        ReadinessRequirement(kind="file")


def test_file_requirement_rejects_empty_path() -> None:
    with pytest.raises(ValidationError, match="requires a non-empty 'path'"):
        ReadinessRequirement(kind="file", path="")


def test_sidecar_requirement_rejects_path() -> None:
    with pytest.raises(ValidationError, match="takes no 'path'"):
        ReadinessRequirement(kind="sidecar_response", path="nope")


def test_unknown_kind_rejected() -> None:
    with pytest.raises(ValidationError):
        ReadinessRequirement(kind="carrier_pigeon")  # type: ignore[arg-type]


def test_task_requires_defaults_empty() -> None:
    assert _task().requires == []


def test_task_requires_round_trips_through_dump() -> None:
    t = _task(requires=[{"kind": "file", "path": "papers/x_trimmed.md", "note": "trim"}])
    again = Task.model_validate(t.model_dump(mode="json"))
    assert again.requires[0].kind == "file"
    assert again.requires[0].path == "papers/x_trimmed.md"
    assert again.requires[0].note == "trim"


# --- evaluator: no requirements / file gate -----------------------------


def test_no_requirements_is_ready(queue_dir: Path) -> None:
    assert unmet_requirements(_task(), queue_dir) == []
    assert is_ready(_task(), queue_dir)


def test_file_present_is_ready(queue_dir: Path) -> None:
    (queue_dir / "papers").mkdir()
    (queue_dir / "papers" / "x_trimmed.md").write_text("content", encoding="utf-8")
    task = _task(requires=[{"kind": "file", "path": "papers/x_trimmed.md"}])
    assert unmet_requirements(task, queue_dir) == []
    assert is_ready(task, queue_dir)


def test_file_missing_is_unmet(queue_dir: Path) -> None:
    task = _task(requires=[{"kind": "file", "path": "papers/x_trimmed.md"}])
    reasons = unmet_requirements(task, queue_dir)
    assert len(reasons) == 1
    assert "missing file" in reasons[0]
    assert str(queue_dir / "papers" / "x_trimmed.md") in reasons[0]
    assert not is_ready(task, queue_dir)


def test_file_absolute_path_used_as_is(tmp_path: Path, queue_dir: Path) -> None:
    ext = tmp_path / "elsewhere" / "input.pdf"
    ext.parent.mkdir(parents=True)
    task = _task(requires=[{"kind": "file", "path": str(ext)}])
    assert not is_ready(task, queue_dir)  # absolute path, not yet present
    ext.write_text("x", encoding="utf-8")
    assert is_ready(task, queue_dir)  # appears -> ready, no dispatch involved


def test_note_included_in_reason(queue_dir: Path) -> None:
    task = _task(requires=[{"kind": "file", "path": "a.md", "note": "the trimmed paper"}])
    (reason,) = unmet_requirements(task, queue_dir)
    assert "the trimmed paper" in reason


def test_multiple_requirements_reports_only_unmet(queue_dir: Path) -> None:
    (queue_dir / "present.md").write_text("x", encoding="utf-8")
    task = _task(
        requires=[
            {"kind": "file", "path": "present.md"},
            {"kind": "file", "path": "missing_a.md"},
            {"kind": "file", "path": "missing_b.md"},
        ]
    )
    reasons = unmet_requirements(task, queue_dir)
    assert len(reasons) == 2
    assert all("missing_" in r for r in reasons)


# --- evaluator: sidecar_response gate -----------------------------------


def test_sidecar_response_ready_when_no_open_request(queue_dir: Path) -> None:
    task = _task(requires=[{"kind": "sidecar_response"}])
    assert is_ready(task, queue_dir)  # no open sidecar for t1


def test_sidecar_response_unmet_when_request_open(queue_dir: Path) -> None:
    # An unanswered request makes t1 "sidecar-open".
    write_request(queue_dir, _sidecar_request("t1", 1))
    task = _task(requires=[{"kind": "sidecar_response"}])
    reasons = unmet_requirements(task, queue_dir)
    assert reasons == ["awaiting sidecar response"]


def test_sidecar_response_uses_precomputed_set(queue_dir: Path) -> None:
    # When the caller passes the open-sidecar set, no directory scan is needed;
    # the task id's membership decides the gate.
    task = _task(requires=[{"kind": "sidecar_response"}])
    assert unmet_requirements(task, queue_dir, open_sidecar_task_ids={"t1"}) == [
        "awaiting sidecar response"
    ]
    assert unmet_requirements(task, queue_dir, open_sidecar_task_ids=set()) == []


def test_sidecar_response_unmet_when_only_some_questions_answered(queue_dir: Path) -> None:
    # The readiness gate consumes list_open_sidecars, so per-question
    # openness has to reach it: a response answering q1 of a q1/q2 request
    # must NOT release the task. Before the per-question fix, the mere
    # existence of the response file cleared this gate and the task was
    # re-dispatched with q2 never answered.
    from datetime import UTC, datetime

    from claude_task_runner.queue.schema import SidecarAnswer, SidecarResponse
    from claude_task_runner.queue.sidecar import write_response

    write_request(queue_dir, _sidecar_request("t1", 1, question_ids=("q1", "q2")))
    write_response(
        queue_dir,
        SidecarResponse(
            task_id="t1",
            sequence=1,
            responded_at=datetime(2026, 7, 8, 1, tzinfo=UTC),
            answers=[SidecarAnswer(id="q1", value="A")],
        ),
    )
    task = _task(requires=[{"kind": "sidecar_response"}])
    assert unmet_requirements(task, queue_dir) == ["awaiting sidecar response"]


def test_sidecar_response_ready_once_every_question_answered(queue_dir: Path) -> None:
    from datetime import UTC, datetime

    from claude_task_runner.queue.schema import SidecarAnswer, SidecarResponse
    from claude_task_runner.queue.sidecar import write_response

    write_request(queue_dir, _sidecar_request("t1", 1, question_ids=("q1", "q2")))
    write_response(
        queue_dir,
        SidecarResponse(
            task_id="t1",
            sequence=1,
            responded_at=datetime(2026, 7, 8, 1, tzinfo=UTC),
            answers=[SidecarAnswer(id="q1", value="A"), SidecarAnswer(id="q2", value="B")],
        ),
    )
    assert is_ready(_task(requires=[{"kind": "sidecar_response"}]), queue_dir)


def _sidecar_request(task_id: str, seq: int, question_ids: tuple[str, ...] = ("q1",)):
    from datetime import UTC, datetime

    from claude_task_runner.queue.schema import (
        CURRENT_SCHEMA_VERSION,
        SidecarOption,
        SidecarQuestion,
        SidecarRequest,
    )

    return SidecarRequest(
        schema_version=CURRENT_SCHEMA_VERSION,
        task_id=task_id,
        sequence=seq,
        created_at=datetime(2026, 7, 8, tzinfo=UTC),
        summary="s",
        context="c",
        questions=[
            SidecarQuestion(
                id=qid,
                prompt="?",
                options=[
                    SidecarOption(value="A", label="a", description=""),
                    SidecarOption(value="B", label="b", description=""),
                ],
                recommended="A",
                multi_select=False,
            )
            for qid in question_ids
        ],
    )


# --- Hold-reason vocabulary ---------------------------------------------
#
# The marker is what lets the runner distinguish a hold IT parked (and may
# therefore clear on its own) from an operator's manual park or the
# pre-dispatch hook's exit-1 deferral, which it must never touch.


def test_hold_reason_carries_the_marker_and_every_reason() -> None:
    reason = hold_reason(["missing file: /q/a.md", "awaiting sidecar response"])
    assert reason.startswith(HOLD_REASON_PREFIX)
    assert "missing file: /q/a.md" in reason
    assert "awaiting sidecar response" in reason


def test_is_hold_reason_recognises_its_own_output() -> None:
    assert is_hold_reason(hold_reason(["missing file: /q/a.md"]))


@pytest.mark.parametrize(
    "reason",
    [
        None,
        "",
        "PARKED 2026-08-06: blocked on a missing parameter",
        "pre-dispatch hook deferred (exit 1): awaiting trim",
        # Near-miss: the words appear but not as the marker prefix.
        "operator note - readiness hold: not ours",
    ],
)
def test_is_hold_reason_rejects_reasons_it_did_not_write(reason: str | None) -> None:
    assert not is_hold_reason(reason)
