from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from claude_runner.notify.emitter import EventEmitter


def test_events_and_per_task_log_written(tmp_path: Path) -> None:
    emitter = EventEmitter(
        events_path=tmp_path / "events.ndjson", log_dir=tmp_path / "logs", stdout=False
    )
    emitter.emit("task_started", task_id="a", detail="hello")
    emitter.emit("task_completed", task_id="a")
    emitter.emit("run_finished")

    lines = (tmp_path / "events.ndjson").read_text().splitlines()
    assert len(lines) == 3
    first = json.loads(lines[0])
    assert first["event"] == "task_started"
    assert first["task_id"] == "a"
    assert "ts" in first

    per_task = (tmp_path / "logs" / "a.ndjson").read_text().splitlines()
    assert len(per_task) == 2  # only task-scoped events
    assert all(json.loads(line)["task_id"] == "a" for line in per_task)


def test_datetime_serializes(tmp_path: Path) -> None:
    emitter = EventEmitter(
        events_path=tmp_path / "e.ndjson", log_dir=tmp_path / "logs", stdout=False
    )
    emitter.emit("x", when=datetime(2026, 1, 2, 3, 4, tzinfo=UTC))
    line = (tmp_path / "e.ndjson").read_text().splitlines()[0]
    assert "2026-01-02T03:04:00" in line
