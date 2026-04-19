from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from claude_runner.config import Settings
from claude_runner.models import DispatchResult, StopReason, TokenUsage
from claude_runner.notify.emitter import EventEmitter
from claude_runner.state.store import StateStore
from claude_runner.todo.schema import TaskSpec


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    (tmp_path / "todo").mkdir()
    return tmp_path


@pytest.fixture
def settings() -> Settings:
    return Settings(
        regime="pro_max",
        plan="max5",
        budget_source="static",
        backend="asyncio",
        max_concurrency=4,
        min_utilization=0.8,
        weekly_guard=0.9,
        discovery_cache_ttl_s=1,
    )


@pytest.fixture
def state_store(tmp_project: Path) -> StateStore:
    return StateStore(tmp_project / ".claude_runner")


@pytest.fixture
def emitter(state_store: StateStore) -> EventEmitter:
    return EventEmitter(
        events_path=state_store.events_path(),
        log_dir=state_store.root / "logs",
        stdout=False,
    )


class FakeBackend:
    """Scripted backend — each call pops the next result from `results`."""

    name = "fake"

    def __init__(
        self,
        *,
        state_store: StateStore,
        emitter: EventEmitter,
        outcomes: dict[str, DispatchResult] | None = None,
    ) -> None:
        self._state = state_store
        self._emitter = emitter
        self._outcomes = outcomes or {}
        self.calls: list[str] = []

    def set(self, task_id: str, result: DispatchResult) -> None:
        self._outcomes[task_id] = result

    async def run_task(self, spec: TaskSpec) -> DispatchResult:
        self.calls.append(spec.id)
        # Simulate the backend setting RUNNING then COMPLETED/FAILED.
        from claude_runner.models import TaskStatus

        state = self._state.load(spec.id)
        state.status = TaskStatus.RUNNING
        state.session_id = state.session_id or f"sid-{spec.id}"
        self._state.save(state)

        await asyncio.sleep(0)  # yield
        result = self._outcomes.get(
            spec.id,
            DispatchResult(
                task_id=spec.id,
                success=True,
                usage=TokenUsage(input_tokens=1000, output_tokens=500),
                stop_reason=StopReason.END_TURN,
                session_id=state.session_id,
                duration_s=1.0,
            ),
        )
        state = self._state.load(spec.id)
        state.status = TaskStatus.COMPLETED if result.success else TaskStatus.FAILED
        state.stop_reason = result.stop_reason
        state.error = result.error
        state.last_finished_at = datetime.now(tz=UTC)
        self._state.save(state)
        return result


@pytest.fixture
def fake_backend(state_store: StateStore, emitter: EventEmitter) -> FakeBackend:
    return FakeBackend(state_store=state_store, emitter=emitter)


def make_task_yaml(path: Path, **fields: Any) -> Path:
    import yaml

    data: dict[str, Any] = {
        "prompt": "do a thing",
        "working_dir": str(path.parent),
    }
    data.update(fields)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture
def make_task(tmp_project: Path):
    def _make(name: str, **fields: Any) -> Path:
        return make_task_yaml(tmp_project / "todo" / f"{name}.yaml", **fields)

    return _make


class MutableClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 4, 18, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()
