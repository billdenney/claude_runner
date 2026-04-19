"""Persistent per-task state living under .claude_runner/state/<id>.yaml."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from claude_runner.models import RunRecord, StopReason, TaskState, TaskStatus, TokenUsage


def _isoformat(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def state_to_dict(state: TaskState) -> dict[str, Any]:
    return {
        "task_id": state.task_id,
        "status": state.status.value,
        "session_id": state.session_id,
        "attempts": state.attempts,
        "last_started_at": _isoformat(state.last_started_at),
        "last_finished_at": _isoformat(state.last_finished_at),
        "stop_reason": state.stop_reason.value if state.stop_reason else None,
        "error": state.error,
        "runs": [
            {
                "attempt": r.attempt,
                "started_at": _isoformat(r.started_at),
                "finished_at": _isoformat(r.finished_at),
                "stop_reason": r.stop_reason.value if r.stop_reason else None,
                "error": r.error,
                "usage": {
                    "input_tokens": r.usage.input_tokens,
                    "output_tokens": r.usage.output_tokens,
                    "cache_read_tokens": r.usage.cache_read_tokens,
                    "cache_creation_tokens": r.usage.cache_creation_tokens,
                    "cost_usd": r.usage.cost_usd,
                },
                "duration_s": r.duration_s,
            }
            for r in state.runs
        ],
    }


def state_from_dict(task_id: str, data: dict[str, Any] | None) -> TaskState:
    if not data:
        return TaskState(task_id=task_id)
    runs: list[RunRecord] = []
    for r in data.get("runs") or []:
        usage = TokenUsage(**(r.get("usage") or {}))
        runs.append(
            RunRecord(
                attempt=int(r["attempt"]),
                started_at=_parse_dt(r["started_at"]) or datetime.min,
                finished_at=_parse_dt(r.get("finished_at")),
                usage=usage,
                stop_reason=StopReason(r["stop_reason"]) if r.get("stop_reason") else None,
                error=r.get("error"),
            )
        )
    return TaskState(
        task_id=data.get("task_id", task_id),
        status=TaskStatus(data.get("status", TaskStatus.PENDING.value)),
        session_id=data.get("session_id"),
        attempts=int(data.get("attempts", 0)),
        last_started_at=_parse_dt(data.get("last_started_at")),
        last_finished_at=_parse_dt(data.get("last_finished_at")),
        stop_reason=StopReason(data["stop_reason"]) if data.get("stop_reason") else None,
        runs=runs,
        error=data.get("error"),
    )


class StateStore:
    """Read/write per-task YAML state files atomically."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._state_dir = root / "state"
        self._logs_dir = root / "logs"
        self._locks_dir = root / "locks"
        for d in (self._state_dir, self._logs_dir, self._locks_dir):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def state_path(self, task_id: str) -> Path:
        return self._state_dir / f"{task_id}.yaml"

    def lock_path(self, task_id: str) -> Path:
        return self._locks_dir / f"{task_id}.lock"

    def events_path(self) -> Path:
        return self._root / "events.ndjson"

    def log_path(self, task_id: str) -> Path:
        return self._logs_dir / f"{task_id}.ndjson"

    def load(self, task_id: str) -> TaskState:
        path = self.state_path(task_id)
        if not path.is_file():
            return TaskState(task_id=task_id)
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return state_from_dict(task_id, data)

    def save(self, state: TaskState) -> None:
        """Atomic write via tempfile + rename."""
        path = self.state_path(state.task_id)
        payload = yaml.safe_dump(state_to_dict(state), sort_keys=False)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{state.task_id}.", suffix=".yaml.tmp", dir=str(self._state_dir)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            finally:
                raise

    def write_session_id(self, task_id: str, session_id: str) -> None:
        """Persist the session_id as soon as it is captured."""
        state = self.load(task_id)
        if state.session_id == session_id:
            return
        state.session_id = session_id
        self.save(state)

    def iter_states(self) -> list[TaskState]:
        states: list[TaskState] = []
        if not self._state_dir.is_dir():
            return states
        for p in sorted(self._state_dir.glob("*.yaml")):
            states.append(self.load(p.stem))
        return states
