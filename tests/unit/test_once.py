"""``once.first_time``: the one process-wide record of warnings already given."""

from __future__ import annotations

import threading

import pytest

from claude_task_runner import once
from claude_task_runner.once import first_time


def test_true_the_first_time_only() -> None:
    key = ("test_once", object())
    other = ("test_once", object())

    assert [first_time(key), first_time(key), first_time(other), first_time(key)] == [
        True,
        False,
        True,
        False,
    ]


def test_keys_past_the_cap_forget_the_oldest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forgotten to make room, the oldest key counts as new again; the
    newer ones are still remembered."""
    monkeypatch.setattr(once, "_seen", {})
    monkeypatch.setattr(once, "MAX_KEYS", 2)

    assert [first_time("a"), first_time("b"), first_time("c")] == [True, True, True]
    assert [first_time("c"), first_time("b"), first_time("a")] == [False, False, True]


def test_threads_asking_together_get_one_true() -> None:
    key = ("test_once", object())
    answers: list[bool] = []
    start = threading.Barrier(8)

    def _ask() -> None:
        start.wait()
        answers.append(first_time(key))

    threads = [threading.Thread(target=_ask) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(answers) == [False] * 7 + [True]
