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


def test_running_task_is_filtered_from_ready(
    catalog_factory, make_task, state_store: StateStore
) -> None:
    make_task("001")
    make_task("002")
    # Flip 001 to RUNNING outside of the in-flight set — ready_tasks should skip it.
    state = state_store.load("001")
    state.status = TaskStatus.RUNNING
    state_store.save(state)
    cat = catalog_factory(StepClock())
    ids = {e.spec.id for e in cat.ready_tasks()}
    assert ids == {"002"}


def test_time_source_accepts_non_callable(
    tmp_project: Path, settings, state_store: StateStore, make_task
) -> None:
    """`time_source` can be a plain number; the catalog just coerces to float."""
    from claude_runner.todo.catalog import TodoCatalog

    make_task("001")
    cat = TodoCatalog(
        tmp_project / "todo",
        state_store=state_store,
        settings=settings,
        time_source=0,  # non-callable, goes through the fallback branch
    )
    assert [e.spec.id for e in cat.ready_tasks()] == ["001"]


def test_missing_todo_dir_surfaces_error(tmp_path: Path, settings, state_store: StateStore) -> None:
    """If the configured todo dir doesn't exist, the catalog records an error
    and returns an empty entry list rather than crashing."""
    from claude_runner.todo.catalog import TodoCatalog

    cat = TodoCatalog(
        tmp_path / "does-not-exist",
        state_store=state_store,
        settings=settings,
        time_source=lambda: 0.0,
    )
    assert cat.all_entries() == []
    assert any("not a directory" in err for err in cat.errors())


def test_cycle_between_tasks_is_reported_as_error(
    catalog_factory, make_task, tmp_project: Path
) -> None:
    make_task("a", depends_on=["b"])
    make_task("b", depends_on=["a"])
    cat = catalog_factory(StepClock())
    cat.all_entries()  # force refresh
    assert any("cycle" in err for err in cat.errors())


def test_duplicate_ids_across_files_surface_as_error(
    catalog_factory, make_task, tmp_project: Path
) -> None:
    make_task("first", id="shared")
    make_task("second", id="shared")
    cat = catalog_factory(StepClock())
    cat.all_entries()
    assert any("duplicate" in err for err in cat.errors())


def test_missing_dependency_keeps_task_blocked(catalog_factory, make_task) -> None:
    """Dep id that doesn't resolve to a spec keeps the task out of ready."""
    make_task("child", depends_on=["ghost"])
    cat = catalog_factory(StepClock())
    ids = {e.spec.id for e in cat.ready_tasks()}
    assert ids == set()


def test_file_disappearing_mid_scan_is_tolerated(
    catalog_factory, make_task, tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a YAML listed by glob vanishes before the `.stat()` call, the catalog
    skips it — we simulate the race by making stat() raise for a specific file."""
    from pathlib import Path as _Path

    make_task("001")
    make_task("002")

    original_stat = _Path.stat

    def vanishing_stat(self: _Path, *a, **kw):
        if self.stem == "001" and self.suffix == ".yaml":
            raise FileNotFoundError(self)
        return original_stat(self, *a, **kw)

    monkeypatch.setattr(_Path, "stat", vanishing_stat)
    cat = catalog_factory(StepClock())
    ids = {e.spec.id for e in cat.ready_tasks()}
    # 001 vanished, 002 still loaded.
    assert ids == {"002"}


def test_blocked_task_is_filtered_from_ready(
    catalog_factory, make_task, state_store: StateStore
) -> None:
    """A task whose state is BLOCKED should not appear in ready_tasks."""
    from claude_runner.models import TaskState, TaskStatus

    make_task("blocked-one")
    state_store.save(TaskState(task_id="blocked-one", status=TaskStatus.BLOCKED))
    cat = catalog_factory(StepClock())
    assert cat.ready_tasks() == []
