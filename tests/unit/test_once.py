"""``once.first_time``: the one process-wide record of warnings already given."""

from __future__ import annotations

import threading
import time

import pytest

from claude_task_runner import once
from claude_task_runner.once import first_time


def test_true_the_first_time_only() -> None:
    about = object()

    assert [
        first_time("test_once", about),
        first_time("test_once", about),
        first_time("test_once", object()),
        first_time("another_warning", about),
        first_time("test_once", about),
    ] == [True, False, True, True, False]


def test_past_the_cap_the_oldest_key_is_forgotten_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """First in, first out: asking about the oldest key again does not keep
    it. Forgotten, it counts as new; the newer key is still remembered."""
    monkeypatch.setattr(once, "_seen", {})
    monkeypatch.setattr(once, "MAX_KEYS", 2)

    assert [first_time("w", "a"), first_time("w", "b"), first_time("w", "a")] == [
        True,
        True,
        False,
    ]
    assert first_time("w", "c") is True
    assert [first_time("w", "b"), first_time("w", "a")] == [False, True]


class _SlowHash:
    """A key part whose hash takes a while, so that threads asking about it
    at once overlap between the check and the insert unless the record is
    locked."""

    def __hash__(self) -> int:
        time.sleep(0.01)
        return 0


def test_threads_asking_together_get_one_true() -> None:
    about = _SlowHash()
    answers: list[bool] = []
    start = threading.Barrier(8)

    def _ask() -> None:
        start.wait()
        answers.append(first_time("test_once", about))

    threads = [threading.Thread(target=_ask) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(answers) == [False] * 7 + [True]
