"""Cached dynamic view of the todo directory."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from claude_runner.config import Settings
from claude_runner.models import TaskState, TaskStatus
from claude_runner.state.store import StateStore
from claude_runner.todo.loader import LoadResult, load_task_file
from claude_runner.todo.schema import TaskSpec, detect_cycles

_log = logging.getLogger(__name__)


@dataclass(slots=True)
class CatalogEntry:
    spec: TaskSpec
    state: TaskState
    source_path: Path


@dataclass(slots=True)
class _CacheSlot:
    mtime_ns: int
    size: int
    spec: TaskSpec


@dataclass(slots=True)
class _CachedRefresh:
    built_at: float = 0.0
    entries: list[CatalogEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class TodoCatalog:
    """Snapshots the todo dir + state store, caching for `ttl_s` seconds.

    `ready_tasks()` is the scheduler-facing API: it returns tasks eligible to
    dispatch right now, filtered for status and dependencies. Callers MUST
    call `invalidate()` after writing state so a freshly-completed task is not
    re-offered within the TTL window.
    """

    def __init__(
        self,
        todo_dir: Path,
        *,
        state_store: StateStore,
        settings: Settings,
        time_source: object = time.monotonic,
    ) -> None:
        self._todo_dir = todo_dir
        self._state_store = state_store
        self._settings = settings
        self._ttl_s = settings.discovery_cache_ttl_s
        self._now: object = time_source
        self._cached = _CachedRefresh()
        self._file_cache: dict[Path, _CacheSlot] = {}

    # ----- public API ---------------------------------------------------

    def invalidate(self) -> None:
        self._cached = _CachedRefresh()

    def all_entries(self) -> list[CatalogEntry]:
        self._refresh_if_stale()
        return list(self._cached.entries)

    def ready_tasks(self, in_flight_ids: set[str] | None = None) -> list[CatalogEntry]:
        """Tasks currently runnable, ordered by priority then id."""
        self._refresh_if_stale()
        in_flight = in_flight_ids or set()

        by_id = {e.spec.id: e for e in self._cached.entries}
        ready: list[CatalogEntry] = []
        for entry in self._cached.entries:
            state = entry.state
            if state.status == TaskStatus.COMPLETED:
                continue
            if state.status == TaskStatus.FAILED or state.status == TaskStatus.BLOCKED:
                continue
            if entry.spec.id in in_flight:
                continue
            if state.status == TaskStatus.RUNNING:
                continue
            if not self._deps_satisfied(entry.spec, by_id):
                continue
            ready.append(entry)
        ready.sort(key=lambda e: (e.spec.priority_rank(), e.spec.id))
        return ready

    def errors(self) -> list[str]:
        self._refresh_if_stale()
        return list(self._cached.errors)

    # ----- internals ----------------------------------------------------

    def _current_time(self) -> float:
        clock = self._now
        if callable(clock):
            return float(clock())
        return float(clock)  # type: ignore[arg-type]

    def _refresh_if_stale(self) -> None:
        now = self._current_time()
        if self._cached.entries and (now - self._cached.built_at) < self._ttl_s:
            return
        self._refresh(now)

    def _refresh(self, now: float) -> None:
        entries: list[CatalogEntry] = []
        errors: list[str] = []
        seen_ids: dict[str, Path] = {}

        if not self._todo_dir.is_dir():
            self._cached = _CachedRefresh(
                built_at=now, entries=[], errors=[f"{self._todo_dir}: not a directory"]
            )
            return

        paths: list[Path] = []
        for pat in ("*.yaml", "*.yml"):
            paths.extend(sorted(self._todo_dir.glob(pat)))

        live_paths: set[Path] = set()
        specs: list[TaskSpec] = []

        for path in paths:
            live_paths.add(path)
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            slot = self._file_cache.get(path)
            if slot is not None and slot.mtime_ns == stat.st_mtime_ns and slot.size == stat.st_size:
                spec = slot.spec
            else:
                try:
                    spec = load_task_file(path, settings=self._settings)
                except Exception as exc:
                    errors.append(f"{path}: {exc}")
                    self._file_cache.pop(path, None)
                    continue
                self._file_cache[path] = _CacheSlot(stat.st_mtime_ns, stat.st_size, spec)

            if spec.id in seen_ids:
                errors.append(f"{path}: duplicate task id {spec.id!r}")
                continue
            seen_ids[spec.id] = path
            specs.append(spec)
            state = self._state_store.load(spec.id)
            entries.append(CatalogEntry(spec=spec, state=state, source_path=path))

        # Evict cache entries whose files disappeared.
        for stale in list(self._file_cache):
            if stale not in live_paths:
                self._file_cache.pop(stale, None)

        cycle = detect_cycles(specs)
        if cycle:
            errors.append(f"dependency cycle: {' -> '.join(cycle)}")

        self._cached = _CachedRefresh(built_at=now, entries=entries, errors=errors)

    def _deps_satisfied(self, spec: TaskSpec, by_id: dict[str, CatalogEntry]) -> bool:
        for dep_id in spec.depends_on:
            dep = by_id.get(dep_id)
            if dep is None:
                return False
            if dep.state.status != TaskStatus.COMPLETED:
                return False
        return True


def full_load(todo_dir: Path, *, settings: Settings) -> LoadResult:
    """Convenience wrapper for `validate` CLI (no caching, no state)."""
    from claude_runner.todo.loader import load_todo_dir

    return load_todo_dir(todo_dir, settings=settings)
