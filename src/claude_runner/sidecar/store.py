"""Filesystem-backed store for sidecar request / response files.

Layout (per task)::

    <sidecar_root>/<task_id>/
    ├── request-001.json
    ├── response-001.json
    ├── request-002.json
    └── ...

All writes are atomic (``tempfile`` + ``os.replace``) and all reads re-read
from disk on every call, so concurrent writers on the same project dir
cannot corrupt state. The project-level fcntl lock (see
:mod:`claude_runner.state.lock`) still serialises two runner processes
against the same project, but this store is intentionally usable from
unrelated processes (for example the ``claude-runner input`` CLI invoked
while the scheduler is running).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from claude_runner.sidecar.schema import (
    Interaction,
    InteractionRequest,
    InteractionResponse,
    RequestState,
    request_from_dict,
    request_to_dict,
    response_from_dict,
    response_to_dict,
)


class SidecarValidationError(ValueError):
    """Raised when a sidecar payload fails schema or cross-file validation."""


_REQUEST_RE = re.compile(r"^request-(\d+)\.json$")
_RESPONSE_RE = re.compile(r"^response-(\d+)\.json$")


class SidecarStore:
    """Reads and writes sidecar interaction files under a task-specific directory."""

    def __init__(self, sidecar_root: Path) -> None:
        self._root = Path(sidecar_root)

    @property
    def root(self) -> Path:
        return self._root

    # ------------------------------------------------------------------ paths

    def task_dir(self, task_id: str) -> Path:
        return self._root / task_id

    def request_path(self, task_id: str, sequence: int) -> Path:
        return self.task_dir(task_id) / f"request-{sequence:03d}.json"

    def response_path(self, task_id: str, sequence: int) -> Path:
        return self.task_dir(task_id) / f"response-{sequence:03d}.json"

    # ------------------------------------------------------------------ atomic IO

    @staticmethod
    def _atomic_write_json(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=False, default=str)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            finally:
                raise

    @staticmethod
    def _read_json(path: Path) -> dict[str, object]:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise SidecarValidationError(
                f"Sidecar file must be a JSON object, got {type(data).__name__}: {path}"
            )
        return data

    # ------------------------------------------------------------------ listings

    def _sorted_files(self, task_id: str, regex: re.Pattern[str]) -> list[tuple[int, Path]]:
        d = self.task_dir(task_id)
        if not d.is_dir():
            return []
        matches: list[tuple[int, Path]] = []
        for f in d.iterdir():
            m = regex.match(f.name)
            if m:
                matches.append((int(m.group(1)), f))
        matches.sort(key=lambda t: t[0])
        return matches

    def list_request_sequences(self, task_id: str) -> list[int]:
        return [seq for seq, _ in self._sorted_files(task_id, _REQUEST_RE)]

    def list_response_sequences(self, task_id: str) -> list[int]:
        return [seq for seq, _ in self._sorted_files(task_id, _RESPONSE_RE)]

    def next_request_sequence(self, task_id: str) -> int:
        seqs = self.list_request_sequences(task_id)
        return (max(seqs) + 1) if seqs else 1

    # ------------------------------------------------------------------ load

    def load_request(self, task_id: str, sequence: int) -> InteractionRequest:
        path = self.request_path(task_id, sequence)
        data = self._read_json(path)
        if str(data.get("task_id")) != task_id:
            raise SidecarValidationError(
                f"request file task_id='{data.get('task_id')}' != expected '{task_id}': {path}"
            )
        if int(data.get("sequence", -1)) != sequence:  # type: ignore[call-overload]
            raise SidecarValidationError(
                f"request file sequence={data.get('sequence')} != expected {sequence}: {path}"
            )
        return request_from_dict(data)

    def load_response(self, task_id: str, sequence: int) -> InteractionResponse | None:
        path = self.response_path(task_id, sequence)
        if not path.is_file():
            return None
        data = self._read_json(path)
        if str(data.get("task_id")) != task_id:
            raise SidecarValidationError(
                f"response file task_id='{data.get('task_id')}' != expected '{task_id}': {path}"
            )
        if int(data.get("sequence", -1)) != sequence:  # type: ignore[call-overload]
            raise SidecarValidationError(
                f"response file sequence={data.get('sequence')} != expected {sequence}: {path}"
            )
        return response_from_dict(data)

    def load_interaction(self, task_id: str, sequence: int) -> Interaction:
        return Interaction(
            request=self.load_request(task_id, sequence),
            response=self.load_response(task_id, sequence),
        )

    def iter_interactions(self, task_id: str) -> list[Interaction]:
        return [self.load_interaction(task_id, seq) for seq in self.list_request_sequences(task_id)]

    def find_open_request(self, task_id: str) -> InteractionRequest | None:
        """Return the newest request in state OPEN with no matching response, else None."""
        for seq in reversed(self.list_request_sequences(task_id)):
            inter = self.load_interaction(task_id, seq)
            if inter.is_open:
                return inter.request
            # If the newest interaction is already answered or cancelled we don't look at older ones.
            break
        return None

    def list_awaiting_task_ids(self) -> list[str]:
        """Return task_ids with at least one OPEN unanswered request.

        Used by the scheduler's reporting loop.
        """
        if not self._root.is_dir():
            return []
        awaiting: list[str] = []
        for sub in sorted(self._root.iterdir()):
            if not sub.is_dir():
                continue
            task_id = sub.name
            if self.find_open_request(task_id) is not None:
                awaiting.append(task_id)
        return awaiting

    # ------------------------------------------------------------------ write

    def write_request(self, req: InteractionRequest) -> Path:
        self._validate_request(req)
        path = self.request_path(req.task_id, req.sequence)
        self._atomic_write_json(path, request_to_dict(req))
        return path

    def write_response(
        self, resp: InteractionResponse, *, request: InteractionRequest | None = None
    ) -> Path:
        if request is None:
            request = self.load_request(resp.task_id, resp.sequence)
        self._validate_response(resp, request)
        # Also flip the on-disk request to ANSWERED so the runner sees it closed
        # even if it inspects the request alone.
        if request.state is not RequestState.ANSWERED:
            closed = InteractionRequest(
                task_id=request.task_id,
                sequence=request.sequence,
                created_at=request.created_at,
                summary=request.summary,
                questions=request.questions,
                state=RequestState.ANSWERED,
                context=request.context,
                schema_version=request.schema_version,
            )
            self._atomic_write_json(
                self.request_path(closed.task_id, closed.sequence), request_to_dict(closed)
            )
        path = self.response_path(resp.task_id, resp.sequence)
        self._atomic_write_json(path, response_to_dict(resp))
        return path

    def cancel_request(self, task_id: str, sequence: int, notes: str | None = None) -> None:
        """Mark a request as cancelled (operator gave up). Used by scheduler on task failure."""
        req = self.load_request(task_id, sequence)
        closed = InteractionRequest(
            task_id=req.task_id,
            sequence=req.sequence,
            created_at=req.created_at,
            summary=req.summary,
            questions=req.questions,
            state=RequestState.CANCELLED,
            context=req.context,
            schema_version=req.schema_version,
        )
        self._atomic_write_json(
            self.request_path(closed.task_id, closed.sequence), request_to_dict(closed)
        )
        resp = InteractionResponse(
            task_id=task_id,
            sequence=sequence,
            responded_at=datetime.now(tz=UTC),
            answers=[],
            state=RequestState.CANCELLED,
            notes=notes,
        )
        self._atomic_write_json(self.response_path(task_id, sequence), response_to_dict(resp))

    # ------------------------------------------------------------------ validation

    @staticmethod
    def _validate_request(req: InteractionRequest) -> None:
        if not req.task_id:
            raise SidecarValidationError("request.task_id must be non-empty")
        if req.sequence < 1:
            raise SidecarValidationError("request.sequence must be >= 1")
        if not req.questions:
            raise SidecarValidationError("request must contain at least one question")
        ids = [q.id for q in req.questions]
        if len(set(ids)) != len(ids):
            dups = sorted({i for i in ids if ids.count(i) > 1})
            raise SidecarValidationError(f"duplicate question ids: {dups}")
        for q in req.questions:
            if not q.id:
                raise SidecarValidationError("every question must have a non-empty id")
            if q.recommended is not None and q.options:
                option_values = {o.value for o in q.options}
                if q.recommended not in option_values:
                    raise SidecarValidationError(
                        f"question '{q.id}' recommended='{q.recommended}' "
                        f"is not among option values {sorted(option_values)}"
                    )

    @staticmethod
    def _validate_response(resp: InteractionResponse, req: InteractionRequest) -> None:
        if resp.task_id != req.task_id:
            raise SidecarValidationError(
                f"response.task_id '{resp.task_id}' != request.task_id '{req.task_id}'"
            )
        if resp.sequence != req.sequence:
            raise SidecarValidationError(
                f"response.sequence {resp.sequence} != request.sequence {req.sequence}"
            )
        if resp.state is RequestState.ANSWERED and not resp.answers:
            raise SidecarValidationError(
                "answered response must supply at least one answer; "
                "use state=cancelled for empty responses"
            )
        question_ids = req.question_ids()
        for a in resp.answers:
            if a.id not in question_ids:
                raise SidecarValidationError(
                    f"answer id '{a.id}' is not a question in request "
                    f"(known: {sorted(question_ids)})"
                )
        # For single-select questions with options, validate the answer is one of the options.
        q_by_id = {q.id: q for q in req.questions}
        for a in resp.answers:
            q = q_by_id[a.id]
            if q.options and not q.allow_free_text:
                allowed = {o.value for o in q.options}
                if q.multi_select:
                    if not isinstance(a.value, list):
                        raise SidecarValidationError(
                            f"question '{a.id}' is multi_select; answer must be a list"
                        )
                    bad = [v for v in a.value if v not in allowed]
                    if bad:
                        raise SidecarValidationError(
                            f"question '{a.id}' answers {bad} not among {sorted(allowed)}"
                        )
                else:
                    if isinstance(a.value, list):
                        raise SidecarValidationError(
                            f"question '{a.id}' is single-select; answer must be a string"
                        )
                    if a.value not in allowed:
                        raise SidecarValidationError(
                            f"question '{a.id}' answer '{a.value}' not among {sorted(allowed)}"
                        )
