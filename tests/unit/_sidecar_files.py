"""Write sidecar request files the way a dispatched agent does.

The runner never writes a request: the ``agent-stop-and-ask`` skill has the
agent write ``request-NNN.json`` itself. Tests that need an open sidecar
write the same file with :func:`write_request`. :func:`make_request` and
:func:`make_response` build the payloads when a test cares only about which
questions are asked and answered.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from claude_task_runner.queue.schema import (
    SidecarAnswer,
    SidecarOption,
    SidecarQuestion,
    SidecarRequest,
    SidecarResponse,
)
from claude_task_runner.queue.sidecar import request_path

_WHEN = datetime(2026, 7, 8, tzinfo=UTC)


def write_request(queue_dir: Path, request: SidecarRequest) -> Path:
    """Write ``request`` to its ``request-NNN.json`` path and return the path."""
    path = request_path(queue_dir, request.task_id, request.sequence)
    path.write_text(json.dumps(request.model_dump(mode="json"), indent=2), encoding="utf-8")
    return path


def make_request(
    task_id: str, sequence: int = 1, question_ids: tuple[str, ...] = ("q1",)
) -> SidecarRequest:
    """A request asking ``question_ids``, each with options A and B; an empty
    tuple makes it a zero-question notification."""
    return SidecarRequest(
        task_id=task_id,
        sequence=sequence,
        created_at=_WHEN,
        summary="s",
        context="c",
        questions=[
            SidecarQuestion(
                id=qid,
                prompt=f"{qid}?",
                options=[
                    SidecarOption(value="A", label="a"),
                    SidecarOption(value="B", label="b"),
                ],
                recommended="A",
            )
            for qid in question_ids
        ],
    )


def make_response(
    task_id: str, sequence: int = 1, answer_ids: tuple[str, ...] = ("q1",)
) -> SidecarResponse:
    """A response answering ``answer_ids`` with option A."""
    return SidecarResponse(
        task_id=task_id,
        sequence=sequence,
        responded_at=_WHEN,
        answers=[SidecarAnswer(id=aid, value="A") for aid in answer_ids],
    )
