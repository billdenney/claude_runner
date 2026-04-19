"""JSON schema / data classes for sidecar request and response files."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

SCHEMA_VERSION = 1


class RequestState(str, Enum):
    """Lifecycle state of a sidecar interaction, stored in both request and response."""

    OPEN = "open"
    """Task has written the request; operator has not yet responded."""
    ANSWERED = "answered"
    """Operator has written a matching response; task is queued for resume."""
    CANCELLED = "cancelled"
    """Operator cancelled the interaction; task will be marked FAILED."""


@dataclass(slots=True)
class Option:
    """A single multiple-choice option inside a :class:`Question`."""

    value: str
    label: str
    description: str | None = None


@dataclass(slots=True)
class Question:
    """A single question the task wants the operator to answer."""

    id: str
    prompt: str
    options: list[Option] = field(default_factory=list)
    multi_select: bool = False
    recommended: str | None = None
    """If set, must match one of ``options[*].value``."""

    allow_free_text: bool = False
    """If True the operator may provide a free-text answer in addition to / instead of options."""


@dataclass(slots=True)
class InteractionRequest:
    """The contents of a ``request-<seq>.json`` file written by the task."""

    task_id: str
    sequence: int
    created_at: datetime
    summary: str
    questions: list[Question]
    state: RequestState = RequestState.OPEN
    context: str | None = None
    schema_version: int = SCHEMA_VERSION

    def question_ids(self) -> set[str]:
        return {q.id for q in self.questions}


@dataclass(slots=True)
class Answer:
    """A single operator-supplied answer to one :class:`Question`."""

    id: str
    value: str | list[str]


@dataclass(slots=True)
class InteractionResponse:
    """The contents of a ``response-<seq>.json`` file written by the operator."""

    task_id: str
    sequence: int
    responded_at: datetime
    answers: list[Answer]
    state: RequestState = RequestState.ANSWERED
    notes: str | None = None
    schema_version: int = SCHEMA_VERSION

    def answer_ids(self) -> set[str]:
        return {a.id for a in self.answers}


@dataclass(slots=True)
class Interaction:
    """A paired (request, optional response) record for one sequence number."""

    request: InteractionRequest
    response: InteractionResponse | None = None

    @property
    def sequence(self) -> int:
        return self.request.sequence

    @property
    def is_answered(self) -> bool:
        return self.response is not None and self.response.state is RequestState.ANSWERED

    @property
    def is_open(self) -> bool:
        return self.request.state is RequestState.OPEN and self.response is None

    @property
    def is_cancelled(self) -> bool:
        return self.request.state is RequestState.CANCELLED or (
            self.response is not None and self.response.state is RequestState.CANCELLED
        )


def request_to_dict(req: InteractionRequest) -> dict[str, Any]:
    return {
        "schema_version": req.schema_version,
        "task_id": req.task_id,
        "sequence": req.sequence,
        "created_at": req.created_at.isoformat(),
        "summary": req.summary,
        "context": req.context,
        "questions": [
            {
                "id": q.id,
                "prompt": q.prompt,
                "options": [
                    {"value": o.value, "label": o.label, "description": o.description}
                    for o in q.options
                ],
                "multi_select": q.multi_select,
                "recommended": q.recommended,
                "allow_free_text": q.allow_free_text,
            }
            for q in req.questions
        ],
        "state": req.state.value,
    }


def request_from_dict(data: dict[str, Any]) -> InteractionRequest:
    questions = [
        Question(
            id=str(q["id"]),
            prompt=str(q["prompt"]),
            options=[
                Option(
                    value=str(o["value"]),
                    label=str(o["label"]),
                    description=o.get("description"),
                )
                for o in q.get("options", [])
            ],
            multi_select=bool(q.get("multi_select", False)),
            recommended=q.get("recommended"),
            allow_free_text=bool(q.get("allow_free_text", False)),
        )
        for q in data.get("questions", [])
    ]
    return InteractionRequest(
        task_id=str(data["task_id"]),
        sequence=int(data["sequence"]),
        created_at=datetime.fromisoformat(str(data["created_at"])),
        summary=str(data.get("summary", "")),
        context=data.get("context"),
        questions=questions,
        state=RequestState(str(data.get("state", RequestState.OPEN.value))),
        schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
    )


def response_to_dict(resp: InteractionResponse) -> dict[str, Any]:
    return {
        "schema_version": resp.schema_version,
        "task_id": resp.task_id,
        "sequence": resp.sequence,
        "responded_at": resp.responded_at.isoformat(),
        "state": resp.state.value,
        "notes": resp.notes,
        "answers": [{"id": a.id, "value": a.value} for a in resp.answers],
    }


def response_from_dict(data: dict[str, Any]) -> InteractionResponse:
    answers = [Answer(id=str(a["id"]), value=a["value"]) for a in data.get("answers", [])]
    return InteractionResponse(
        task_id=str(data["task_id"]),
        sequence=int(data["sequence"]),
        responded_at=datetime.fromisoformat(str(data["responded_at"])),
        answers=answers,
        state=RequestState(str(data.get("state", RequestState.ANSWERED.value))),
        notes=data.get("notes"),
        schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
    )
