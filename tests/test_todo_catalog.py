from __future__ import annotations

from pathlib import Path

import pytest

from claude_runner.config import Settings
from claude_runner.models import TaskStatus
from claude_runner.state.store import StateStore
from claude_runner.todo.catalog import TodoCatalog


class StepClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, secs: float) -> None:
        self.t += secs


@pytest.fixture
def catalog_factory(tmp_project: Path, settings: Settings, state_store: StateStore):
    def _make(clock: StepClock) -> TodoCatalog:
        return TodoCatalog(
            tmp_project / "todo",
            state_store=state_store,
            settings=settings,
            time_source=clock,
        )

    return _make


def test_new_yaml_appears_after_ttl(catalog_factory, make_task) -> None:
    make_task("001-one")
    clock = StepClock()
    cat = catalog_factory(clock)
    first = cat.ready_tasks()
    assert [e.spec.id for e in first] == ["001-one"]

    make_task("002-two")
    # Within TTL — still cached.
    second = cat.ready_tasks()
    assert [e.spec.id for e in second] == ["001-one"]

    clock.advance(10)
    third = cat.ready_tasks()
    assert sorted(e.spec.id for e in third) == ["001-one", "002-two"]


def test_deleted_task_disappears_after_invalidate(catalog_factory, make_task, tmp_project) -> None:
    make_task("001")
    make_task("002")
    clock = StepClock()
    cat = catalog_factory(clock)
    assert len(cat.ready_tasks()) == 2

    (tmp_project / "todo" / "002.yaml").unlink()
    cat.invalidate()
    ids = {e.spec.id for e in cat.ready_tasks()}
    assert ids == {"001"}


def test_in_flight_filter(catalog_factory, make_task) -> None:
    make_task("001")
    make_task("002")
    cat = catalog_factory(StepClock())
    ready = cat.ready_tasks(in_flight_ids={"001"})
    assert {e.spec.id for e in ready} == {"002"}


def test_completed_task_not_returned(catalog_factory, make_task, state_store: StateStore) -> None:
    make_task("001")
    make_task("002")
    cat = catalog_factory(StepClock())
    # Mark 001 completed.
    state = state_store.load("001")
    state.status = TaskStatus.COMPLETED
    state_store.save(state)
    cat.invalidate()
    ready = cat.ready_tasks()
    assert {e.spec.id for e in ready} == {"002"}


def test_malformed_yaml_is_skipped(catalog_factory, make_task, tmp_project) -> None:
    (tmp_project / "todo" / "bad.yaml").write_text(": not valid yaml\n", encoding="utf-8")
    make_task("001")
    cat = catalog_factory(StepClock())
    ids = {e.spec.id for e in cat.ready_tasks()}
    assert ids == {"001"}
    # Malformed file should show up in errors without killing the load.
    assert any("bad.yaml" in err for err in cat.errors())


def test_dependency_blocks_until_parent_completes(
    catalog_factory, make_task, state_store: StateStore
) -> None:
    make_task("a")
    make_task("b", depends_on=["a"])
    cat = catalog_factory(StepClock())
    ids = {e.spec.id for e in cat.ready_tasks()}
    assert ids == {"a"}
    state = state_store.load("a")
    state.status = TaskStatus.COMPLETED
    state_store.save(state)
    cat.invalidate()
    ids = {e.spec.id for e in cat.ready_tasks()}
    assert ids == {"b"}
