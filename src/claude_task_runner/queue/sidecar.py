"""Sidecar request/response handler.

A *sidecar* is the runner's stop-and-ask protocol. When a dispatched task
needs an operator decision it cannot make autonomously (per-paper
extraction ambiguity, novel canonical covariate, etc.), it writes a
``SidecarRequest`` JSON file under
``<queue>/.claude_task_runner/sidecar/<task_id>/request-NNN.json``. The
runner then transitions the task to ``awaiting_sidecar`` and stops
working on it. The operator answers via ``/runner-answer-sidecar``,
producing a sibling ``response-NNN.json``. The runner detects the
response on its next tick and resumes the task.

This module provides:

* Sequence numbering: a task may have multiple request/response rounds.
* Atomic JSON writes (mirror of ``queue.store``).
* Pairing helpers: list open sidecars. Openness is decided PER QUESTION --
  a request is open while any question id it asked is missing from its
  response's ``answers``, so a partial answer leaves the rest visible.

JSON is used for sidecar files (rather than YAML for tasks/state)
because the format flows through the ``/runner-answer-sidecar`` skill
which needs structured option lists, and JSON parses faster /
unambiguously inside Claude Code.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from claude_task_runner.queue.schema import (
    CURRENT_SCHEMA_VERSION,
    SidecarRequest,
    SidecarResponse,
)
from claude_task_runner.queue.store import (
    QueueIOError,
    QueueSchemaError,
    queue_runtime_dir,
)

_REQUEST_RE = re.compile(r"^request-(\d+)\.json$")
_RESPONSE_RE = re.compile(r"^response-(\d+)\.json$")


def sidecar_dir_for(queue_dir: Path, task_id: str) -> Path:
    """Resolve ``<queue>/.claude_task_runner/sidecar/<task_id>/``."""
    base = queue_runtime_dir(queue_dir) / "sidecar" / task_id
    base.mkdir(parents=True, exist_ok=True)
    return base


def request_path(queue_dir: Path, task_id: str, sequence: int) -> Path:
    return sidecar_dir_for(queue_dir, task_id) / f"request-{sequence:03d}.json"


def response_path(queue_dir: Path, task_id: str, sequence: int) -> Path:
    return sidecar_dir_for(queue_dir, task_id) / f"response-{sequence:03d}.json"


def next_sequence(queue_dir: Path, task_id: str) -> int:
    """Compute the next free request sequence number for a task.

    Walks the existing ``request-NNN.json`` files and returns ``max+1``,
    or ``1`` if none exist. Sequence is task-scoped, not queue-scoped.
    """
    base = sidecar_dir_for(queue_dir, task_id)
    existing: list[int] = []
    for p in base.iterdir():
        m = _REQUEST_RE.match(p.name)
        if m is not None:
            existing.append(int(m.group(1)))
    return max(existing, default=0) + 1


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    parent = path.parent
    if not parent.exists():
        raise QueueIOError(f"parent dir does not exist: {parent}")
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=parent,
        delete=False,
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=False, default=str)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def write_request(queue_dir: Path, request: SidecarRequest) -> Path:
    """Write a SidecarRequest JSON. Returns the resulting file path.

    Uses ``request.sequence`` as-is. Caller is responsible for using
    :func:`next_sequence` if they want monotonic numbering.
    """
    if request.schema_version != CURRENT_SCHEMA_VERSION:
        raise QueueSchemaError(
            f"refusing to write SidecarRequest with schema_version="
            f"{request.schema_version}, current is {CURRENT_SCHEMA_VERSION}"
        )
    path = request_path(queue_dir, request.task_id, request.sequence)
    _write_json_atomic(path, request.model_dump(mode="json"))
    return path


def write_response(queue_dir: Path, response: SidecarResponse) -> Path:
    """Write a SidecarResponse JSON. Returns the resulting file path."""
    if response.schema_version != CURRENT_SCHEMA_VERSION:
        raise QueueSchemaError(
            f"refusing to write SidecarResponse with schema_version="
            f"{response.schema_version}, current is {CURRENT_SCHEMA_VERSION}"
        )
    path = response_path(queue_dir, response.task_id, response.sequence)
    _write_json_atomic(path, response.model_dump(mode="json"))
    return path


def read_request(path: Path) -> SidecarRequest:
    try:
        with path.open("rb") as fh:
            payload = json.load(fh)
    except OSError as exc:
        raise QueueIOError(f"failed to read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise QueueSchemaError(f"{path}: invalid JSON: {exc}") from exc
    try:
        return SidecarRequest.model_validate(payload)
    except ValidationError as exc:
        raise QueueSchemaError(f"{path}: {exc}") from exc


def read_response(path: Path) -> SidecarResponse:
    try:
        with path.open("rb") as fh:
            payload = json.load(fh)
    except OSError as exc:
        raise QueueIOError(f"failed to read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise QueueSchemaError(f"{path}: invalid JSON: {exc}") from exc
    try:
        return SidecarResponse.model_validate(payload)
    except ValidationError as exc:
        raise QueueSchemaError(f"{path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Per-question accounting
# ---------------------------------------------------------------------------
#
# Openness is a *per-question* property, not a per-file one. A request may
# ask several questions; a response that answers only the first leaves the
# rest outstanding. Testing "does a response file exist?" reports such a
# request as closed and the unanswered questions become invisible to every
# counter -- the runner's readiness gate, ``sidecar list``, and the
# operator's answering skill alike.
#
# These helpers read the two fields that decide the question -- the request's
# ``questions[].id`` and the response's ``answers[].id`` -- straight from the
# raw JSON rather than going through :class:`SidecarRequest` /
# :class:`SidecarResponse`. That is deliberate: those models are
# ``extra="forbid"`` and validate the *whole* payload, so cosmetic drift that
# says nothing about answeredness (a legacy request missing ``created_at``,
# an answer carrying a per-answer ``notes`` key) would fail validation and
# force a request to be classified with no evidence. Narrow reads keep the
# accounting correct across schema drift while still failing loud -- an id
# that genuinely cannot be determined raises, and the caller surfaces the
# request as open-with-error rather than silently dropping it.

_ID_KEYS = ("id", "question_id")
"""Accepted spellings of a question/answer identifier, newest first.

``question_id`` is a legacy spelling some agent-written requests used. It is
an unambiguous rename, so honouring it keeps those requests accountable
instead of erroring. Prompt text keys (``question``, ``prompt``, ``text``)
are deliberately NOT accepted: they are not identifiers, and guessing one
would risk reporting a question answered when it was not."""

_ID_KEY_HELP = "/".join(_ID_KEYS)


@dataclass(frozen=True)
class OpenSidecar:
    """A request with at least one question the operator has not answered.

    ``outstanding`` holds the asked question ids missing from the response,
    in the order the request asked them, so ``sidecar list`` can tell the
    operator *what* is still missing rather than only which task is stuck.

    ``outstanding`` is empty only for a notification-style request (no
    ``questions`` at all) that has no response file yet, and for the
    ``error`` case where the asked ids could not be determined.
    """

    task_id: str
    sequence: int
    request_path: Path
    outstanding: tuple[str, ...] = ()
    answered: tuple[str, ...] = ()
    response_path: Path | None = None
    error: str | None = None
    """Set when the request or response could not be read well enough to
    decide. Such a request is reported OPEN: an undecidable sidecar must be
    surfaced to the operator, never silently treated as answered."""

    @property
    def partial(self) -> bool:
        """True when a response exists but leaves questions unanswered.

        This is the case the file-existence test could not see at all.
        """
        return self.response_path is not None and bool(self.outstanding)


def _identifier(entry: Any) -> str | None:
    """The id an entry carries, or ``None`` if it carries none."""
    if not isinstance(entry, dict):
        return None
    for key in _ID_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def asked_question_ids(payload: Any) -> list[str]:
    """Question ids a raw request payload asks, in order.

    Returns ``[]`` for a notification-style request (``file_and_exit``) that
    asks nothing. Raises :class:`QueueSchemaError` when the payload asks
    something but the ids cannot be determined -- the caller must surface
    that rather than assume the request is answerable.
    """
    if not isinstance(payload, dict):
        raise QueueSchemaError("request payload is not an object")
    questions = payload.get("questions")
    if questions is None:
        # v1 (legacy) flat shape: a single question stored as top-level
        # ``question``/``options``. ``fetch_all.sh`` synthesises the id
        # ``q1`` for these and operators answered them under that id, so
        # match that convention rather than inventing a second one.
        return ["q1"] if "question" in payload else []
    if not isinstance(questions, list):
        raise QueueSchemaError(f"request 'questions' is not a list: {type(questions).__name__}")
    asked: list[str] = []
    for q in questions:
        qid = _identifier(q)
        if qid is None:
            # An asked question we cannot name is exactly the undecidable
            # case: we can never tell whether it was answered, so refuse to
            # guess and let the caller surface the request.
            raise QueueSchemaError(f"question entry has no usable id (looked for {_ID_KEY_HELP})")
        asked.append(qid)
    return asked


def answered_question_ids(payload: Any) -> set[str]:
    """Question ids a raw response payload answers.

    Answer entries carrying no usable id are skipped rather than raising:
    unlike an unnameable *question*, an unnameable *answer* is decidable --
    it credits nothing, so every asked id stays outstanding and the request
    stays open. Skipping errs toward reporting work, which is the direction
    this whole mechanism is meant to err in. (Live queues do contain such
    entries, e.g. an ``{"id": "", "value": "A"}`` stub written alongside a
    correct ``q1`` answer.) A response with no ``answers`` array at all is a
    different matter and still raises: nothing about it is decidable.
    """
    if not isinstance(payload, dict):
        raise QueueSchemaError("response payload is not an object")
    answers = payload.get("answers")
    if answers is None:
        raise QueueSchemaError("response has no 'answers' array")
    if not isinstance(answers, list):
        raise QueueSchemaError(f"response 'answers' is not a list: {type(answers).__name__}")
    return {aid for aid in (_identifier(a) for a in answers) if aid is not None}


def outstanding_question_ids(request_payload: Any, response_payload: Any | None) -> list[str]:
    """Asked ids that ``response_payload`` does not answer, in asked order.

    ``response_payload=None`` means no response file exists, so every asked
    id is outstanding.
    """
    asked = asked_question_ids(request_payload)
    if response_payload is None:
        return asked
    answered = answered_question_ids(response_payload)
    return [qid for qid in asked if qid not in answered]


def load_sidecar_payload(path: Path) -> Any:
    """Read a request or response file as raw JSON (no schema validation)."""
    try:
        with path.open("rb") as fh:
            return json.load(fh)
    except OSError as exc:
        raise QueueIOError(f"failed to read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise QueueSchemaError(f"{path}: invalid JSON: {exc}") from exc


def request_outstanding(
    queue_dir: Path,
    task_id: str,
    sequence: int,
) -> tuple[list[str], list[str], Path | None]:
    """Per-question state of one request.

    Returns ``(outstanding_ids, asked_ids, response_path_or_None)``. Raises
    :class:`QueueIOError` if the request file is missing and
    :class:`QueueSchemaError` if either file cannot be read well enough to
    decide -- callers that write responses must not proceed past that.
    """
    req_path = request_path(queue_dir, task_id, sequence)
    if not req_path.exists():
        raise QueueIOError(f"sidecar request not found: {req_path}")
    asked = asked_question_ids(load_sidecar_payload(req_path))
    resp_path = response_path(queue_dir, task_id, sequence)
    if not resp_path.exists():
        return list(asked), asked, None
    answered = answered_question_ids(load_sidecar_payload(resp_path))
    return [qid for qid in asked if qid not in answered], asked, resp_path


def open_sidecars(queue_dir: Path) -> Iterator[OpenSidecar]:
    """Yield an :class:`OpenSidecar` for every request still owed an answer.

    A request is open when any asked question id is missing from its
    response's ``answers`` -- so a 3-question request answered only for
    ``q1`` is open on ``q2``/``q3``. A request that asks nothing (a
    ``file_and_exit`` notification) is closed by the mere presence of a
    response file, and open while none exists.

    Order: by task_id then sequence, both lexicographic, for determinism.
    """
    sidecar_root = queue_runtime_dir(queue_dir) / "sidecar"
    if not sidecar_root.exists():
        return
    for task_dir in sorted(sidecar_root.iterdir()):
        if not task_dir.is_dir():
            continue
        responses: set[int] = set()
        requests: list[tuple[int, Path]] = []
        for p in task_dir.iterdir():
            req_m = _REQUEST_RE.match(p.name)
            if req_m is not None:
                requests.append((int(req_m.group(1)), p))
                continue
            resp_m = _RESPONSE_RE.match(p.name)
            if resp_m is not None:
                responses.add(int(resp_m.group(1)))
        for seq, req_path in sorted(requests):
            resp_path = task_dir / f"response-{seq:03d}.json" if seq in responses else None
            try:
                asked = asked_question_ids(load_sidecar_payload(req_path))
                answered = (
                    answered_question_ids(load_sidecar_payload(resp_path))
                    if resp_path is not None
                    else set()
                )
            except (QueueIOError, QueueSchemaError) as exc:
                # Undecidable, so fail loud rather than closed: an operator
                # can repair a sidecar they can see, not one that was
                # silently counted as answered.
                yield OpenSidecar(
                    task_id=task_dir.name,
                    sequence=seq,
                    request_path=req_path,
                    response_path=resp_path,
                    error=str(exc),
                )
                continue
            outstanding = [qid for qid in asked if qid not in answered]
            if resp_path is not None and not outstanding:
                continue
            yield OpenSidecar(
                task_id=task_dir.name,
                sequence=seq,
                request_path=req_path,
                outstanding=tuple(outstanding),
                answered=tuple(qid for qid in asked if qid in answered),
                response_path=resp_path,
            )


def list_open_sidecars(queue_dir: Path) -> Iterator[tuple[str, int, Path]]:
    """Yield ``(task_id, sequence, request_path)`` for every open request.

    Back-compatible projection of :func:`open_sidecars` for callers that only
    need "does this task owe the operator an answer?" -- the readiness gate,
    the orchestrator's eligibility sweep and the dispatcher's post-run status
    override. They all get the per-question openness test for free.
    """
    for item in open_sidecars(queue_dir):
        yield (item.task_id, item.sequence, item.request_path)
