"""Tests for queue/store.py — atomic YAML I/O for tasks and state."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_task_runner.queue.schema import (
    CURRENT_SCHEMA_VERSION,
    RunRecord,
    Task,
    TaskState,
    TokenUsage,
)
from claude_task_runner.queue.store import (
    MAX_YAML_BYTES,
    QueueIOError,
    QueueSchemaError,
    list_pending_tasks,
    list_state_files,
    load_state,
    load_task,
    queue_runtime_dir,
    state_path_for,
    task_path_for,
    todo_dir,
    write_state_atomic,
    write_task_atomic,
)


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "myqueue"
    qd.mkdir()
    return qd


class TestRuntimeDir:
    def test_creates_runtime_subdirs(self, queue_dir: Path) -> None:
        runtime = queue_runtime_dir(queue_dir)
        assert runtime.is_dir()
        assert (runtime / "state").is_dir()
        assert (runtime / "sidecar").is_dir()
        assert (runtime / "logs").is_dir()

    def test_idempotent(self, queue_dir: Path) -> None:
        first = queue_runtime_dir(queue_dir)
        second = queue_runtime_dir(queue_dir)
        assert first == second


class TestRoundTrip:
    def test_task_round_trip(self, queue_dir: Path) -> None:
        t = Task(
            id="001-fiedler",
            title="Extract Fiedler 2019 fremanezumab",
            prompt="...",
            allowed_tools=["Read", "Write"],
            tags=["paper", "popPK"],
            effort="high",
        )
        path = task_path_for(queue_dir, t.id)
        write_task_atomic(t, path)
        loaded = load_task(path)
        assert loaded == t

    def test_state_round_trip(self, queue_dir: Path) -> None:
        when = datetime(2026, 5, 3, 18, 0, tzinfo=UTC)
        run = RunRecord(
            attempt=1,
            started_at=when,
            finished_at=when,
            stop_reason="end_turn",
            duration_s=1.5,
            usage=TokenUsage(input_tokens=10, output_tokens=20),
        )
        s = TaskState(
            task_id="001",
            status="completed",
            attempts=1,
            session_id="sess-abc",
            last_started_at=when,
            last_finished_at=when,
            stop_reason="end_turn",
            runs=[run],
        )
        path = state_path_for(queue_dir, s.task_id)
        write_state_atomic(s, path)
        loaded = load_state(path)
        assert loaded == s
        assert loaded.runs[0].usage.total_tokens == 30


class TestAtomicity:
    def test_write_uses_replace_not_truncate(self, queue_dir: Path) -> None:
        """Concurrent reads should never see a partial file. We simulate
        by writing twice and verifying the file is always loadable as
        a complete TaskState."""
        path = state_path_for(queue_dir, "001")
        # First write
        write_state_atomic(TaskState(task_id="001", status="pending"), path)
        first = load_state(path)
        # Second write with mutation
        write_state_atomic(TaskState(task_id="001", status="running", attempts=1), path)
        second = load_state(path)
        assert first.status == "pending"
        assert second.status == "running"

    def test_no_tmp_files_left_behind(self, queue_dir: Path) -> None:
        path = state_path_for(queue_dir, "001")
        write_state_atomic(TaskState(task_id="001"), path)
        leftovers = list(path.parent.glob(".*tmp*"))
        assert leftovers == []


class TestSchemaVersionGuard:
    def test_unversioned_yaml_uses_default(self, queue_dir: Path) -> None:
        path = task_path_for(queue_dir, "x")
        path.write_text("id: x\ntitle: T\nprompt: P\n")
        loaded = load_task(path)
        assert loaded.schema_version == CURRENT_SCHEMA_VERSION

    def test_wrong_schema_version_rejected(self, queue_dir: Path) -> None:
        path = task_path_for(queue_dir, "x")
        path.write_text("schema_version: 99\nid: x\ntitle: T\nprompt: P\n")
        with pytest.raises(QueueSchemaError, match="schema_version=99"):
            load_task(path)


class TestErrorHandling:
    def test_invalid_yaml_raises(self, queue_dir: Path) -> None:
        path = task_path_for(queue_dir, "x")
        path.write_text(":\n: : :\n")
        with pytest.raises(QueueSchemaError, match="invalid YAML"):
            load_task(path)

    def test_non_mapping_yaml_rejected(self, queue_dir: Path) -> None:
        path = task_path_for(queue_dir, "x")
        path.write_text("- just\n- a\n- list\n")
        with pytest.raises(QueueSchemaError, match="mapping"):
            load_task(path)

    def test_validation_failure_surfaces(self, queue_dir: Path) -> None:
        path = task_path_for(queue_dir, "x")
        path.write_text("id: x\ntitle: T\nprompt: P\npriority: urgent\n")
        with pytest.raises(QueueSchemaError):
            load_task(path)

    def test_write_to_missing_dir_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "nope" / "x.yaml"
        with pytest.raises(QueueIOError, match="parent dir"):
            write_state_atomic(TaskState(task_id="x"), path)

    def test_oversized_yaml_rejected_before_parse(self, queue_dir: Path) -> None:
        """A pathological YAML larger than the size limit is rejected on
        stat, before ``yaml.safe_load`` can expand it and stall a tick."""
        path = task_path_for(queue_dir, "x")
        # Valid YAML mapping, but padded with a comment past the limit so
        # the rejection is purely size-driven (not a parse/validation fail).
        padding = "#" + ("y" * (MAX_YAML_BYTES + 1))
        path.write_text(f"id: x\ntitle: T\nprompt: P\n{padding}\n")
        assert path.stat().st_size > MAX_YAML_BYTES
        with pytest.raises(QueueSchemaError, match="exceeds limit"):
            load_task(path)

    def test_at_limit_yaml_loads(self, queue_dir: Path) -> None:
        """A file at exactly the limit is accepted — the guard rejects
        only strictly-larger files."""
        path = task_path_for(queue_dir, "x")
        body = "id: x\ntitle: T\nprompt: P\n"
        # body + "#" (1) + pad_len "y"s + "\n" (1) == MAX_YAML_BYTES
        pad_len = MAX_YAML_BYTES - len(body.encode()) - 2
        path.write_text(f"{body}#{'y' * pad_len}\n")
        assert path.stat().st_size == MAX_YAML_BYTES
        loaded = load_task(path)
        assert loaded.id == "x"


class TestListing:
    def test_list_pending_returns_sorted(self, queue_dir: Path) -> None:
        td = todo_dir(queue_dir)
        for tid in ("003-c", "001-a", "002-b"):
            (td / f"{tid}.yaml").write_text(f"id: {tid}\ntitle: T\nprompt: P\n")
        names = [p.stem for p in list_pending_tasks(queue_dir)]
        assert names == ["001-a", "002-b", "003-c"]

    def test_list_states(self, queue_dir: Path) -> None:
        for tid in ("001-a", "002-b"):
            write_state_atomic(
                TaskState(task_id=tid),
                state_path_for(queue_dir, tid),
            )
        names = [p.stem for p in list_state_files(queue_dir)]
        assert names == ["001-a", "002-b"]
