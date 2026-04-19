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


def test_enum_and_unknown_object_serialize(tmp_path: Path) -> None:
    """_default handles enum-like objects via .value and falls back to str() otherwise."""
    from enum import Enum

    from claude_runner.notify.emitter import _default

    class Color(str, Enum):
        RED = "red"

    # Enum has a .value attr -> serialized as the value.
    assert _default(Color.RED) == "red"

    # Unknown object with no .value and not a datetime -> str() fallback.
    class Opaque:
        def __str__(self) -> str:
            return "opaque-repr"

    assert _default(Opaque()) == "opaque-repr"

    # End-to-end through the emitter.
    emitter = EventEmitter(
        events_path=tmp_path / "e.ndjson", log_dir=tmp_path / "logs", stdout=False
    )
    emitter.emit("x", color=Color.RED, blob=Opaque())
    line = (tmp_path / "e.ndjson").read_text().splitlines()[0]
    assert '"color": "red"' in line
    assert '"blob": "opaque-repr"' in line


def test_emit_to_stdout_prints(tmp_path: Path, capsys) -> None:
    """stdout=True path writes each event as an NDJSON line to stdout as well."""
    emitter = EventEmitter(
        events_path=tmp_path / "e.ndjson", log_dir=tmp_path / "logs", stdout=True
    )
    emitter.emit("hello", note="world")
    captured = capsys.readouterr().out.splitlines()
    assert any('"event": "hello"' in line for line in captured)
