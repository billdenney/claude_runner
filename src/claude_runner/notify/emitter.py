"""NDJSON event emitter — stdout + events.ndjson + per-task log."""

from __future__ import annotations

import json
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class EventEmitter:
    def __init__(self, *, events_path: Path, log_dir: Path, stdout: bool = True) -> None:
        self._events_path = events_path
        self._log_dir = log_dir
        self._stdout = stdout
        self._lock = threading.Lock()
        self._events_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_dir.mkdir(parents=True, exist_ok=True)

    def emit(self, kind: str, **fields: Any) -> None:
        payload = {
            "ts": datetime.now(tz=UTC).isoformat(),
            "event": kind,
            **fields,
        }
        line = json.dumps(payload, default=_default)
        with self._lock:
            with self._events_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            task_id = fields.get("task_id")
            if isinstance(task_id, str) and task_id:
                with (self._log_dir / f"{task_id}.ndjson").open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            if self._stdout:
                print(line, file=sys.stdout, flush=True)


def _default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "value"):
        return obj.value
    return str(obj)
