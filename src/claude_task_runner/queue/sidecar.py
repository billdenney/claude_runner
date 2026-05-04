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
* Pairing helpers: list open sidecars (request without response).

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


def list_open_sidecars(queue_dir: Path) -> Iterator[tuple[str, int, Path]]:
    """Yield ``(task_id, sequence, request_path)`` for every request that
    has no matching response file.

    Used by ``/runner-answer-sidecar`` to populate the operator's choice
    list. Order: by task_id then sequence. Both lexicographic for
    determinism.
    """
    sidecar_root = queue_runtime_dir(queue_dir) / "sidecar"
    if not sidecar_root.exists():
        return
    open_items: list[tuple[str, int, Path]] = []
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
            if seq not in responses:
                open_items.append((task_dir.name, seq, req_path))
    yield from open_items
