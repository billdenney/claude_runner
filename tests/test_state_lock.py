from __future__ import annotations

import multiprocessing
import time
from pathlib import Path

import pytest

from claude_runner.state.lock import TaskLockError, task_lock


def test_lock_released_after_context_exit(tmp_path: Path) -> None:
    path = tmp_path / "a.lock"
    with task_lock(path):
        pass
    # Should be acquirable again.
    with task_lock(path):
        pass


def test_reentrant_second_open_fails(tmp_path: Path) -> None:
    # flock is per-fd on Linux. Because task_lock opens a fresh fd each call,
    # a second acquisition in the same process also contends and fails — this
    # is the invariant the scheduler relies on to avoid double-dispatch.
    path = tmp_path / "b.lock"
    with task_lock(path), pytest.raises(TaskLockError), task_lock(path):
        pass


def _child(path: str, barrier_path: str) -> None:
    from claude_runner.state.lock import task_lock as _lock

    with _lock(Path(path)):
        Path(barrier_path).write_text("locked", encoding="utf-8")
        # Hold the lock for a bit so the parent can try to grab it.
        time.sleep(0.5)


def test_cross_process_contention_raises(tmp_path: Path) -> None:
    path = tmp_path / "c.lock"
    barrier = tmp_path / "ready.txt"
    ctx = multiprocessing.get_context("fork")
    proc = ctx.Process(target=_child, args=(str(path), str(barrier)))
    proc.start()
    try:
        # Wait up to 1s for the child to grab the lock.
        deadline = time.time() + 1.0
        while not barrier.exists() and time.time() < deadline:
            time.sleep(0.01)
        assert barrier.exists(), "child never took the lock"
        with pytest.raises(TaskLockError), task_lock(path):
            pass
    finally:
        proc.join(timeout=2)
