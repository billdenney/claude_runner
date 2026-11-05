"""What ``dispatch()`` and ``adopt_worker()`` do with a worker once their
dispatch loop raised: :func:`dispatcher._stop_after_loop_error`, and on the
pipe path :func:`dispatcher._drain_pipes`."""

from __future__ import annotations

import logging
import os
import subprocess
from types import SimpleNamespace

import pytest

from claude_task_runner.runner import dispatcher as dispatcher_mod
from claude_task_runner.runner.dispatcher import _AfterLoopError, _stop_after_loop_error


class _Worker:
    """A worker's answers to ``gone``, in order, and the stops it gets."""

    def __init__(self, *, gone: list[bool], stop_raises: Exception | None = None) -> None:
        self._gone = list(gone)
        self._stop_raises = stop_raises
        self.stops = 0

    def gone(self) -> bool:
        return self._gone.pop(0)

    def stop(self) -> None:
        self.stops += 1
        if self._stop_raises is not None:
            raise self._stop_raises


def _after(worker: _Worker) -> _AfterLoopError:
    return _stop_after_loop_error(
        "t1", 4242, RuntimeError("boom"), stop=worker.stop, gone=worker.gone
    )


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == dispatcher_mod.logger.name]


def test_a_worker_already_gone_is_not_stopped(caplog: pytest.LogCaptureFixture) -> None:
    """It exited on its own, and its pid may be another process's by now."""
    worker = _Worker(gone=[True])
    with caplog.at_level(logging.ERROR, logger=dispatcher_mod.logger.name):
        assert _after(worker) is _AfterLoopError.EXITED
    assert worker.stops == 0
    assert _messages(caplog) == [
        "task t1: dispatch loop raised RuntimeError: boom; worker pid=4242 had already exited"
    ]


def test_a_worker_the_stop_ends_is_stopped(caplog: pytest.LogCaptureFixture) -> None:
    worker = _Worker(gone=[False, True])
    with caplog.at_level(logging.ERROR, logger=dispatcher_mod.logger.name):
        assert _after(worker) is _AfterLoopError.STOPPED
    assert worker.stops == 1
    assert _messages(caplog) == [
        "task t1: dispatch loop raised RuntimeError: boom; stopping worker pid=4242"
    ]


def test_a_worker_the_stop_does_not_end_still_runs() -> None:
    worker = _Worker(gone=[False, False])
    assert _after(worker) is _AfterLoopError.RUNNING
    assert worker.stops == 1


@pytest.mark.parametrize(
    ("gone_after", "expected"),
    [(True, _AfterLoopError.STOPPED), (False, _AfterLoopError.RUNNING)],
)
def test_a_stop_that_raises_is_logged_and_liveness_decides(
    caplog: pytest.LogCaptureFixture, gone_after: bool, expected: _AfterLoopError
) -> None:
    worker = _Worker(
        gone=[False, gone_after],
        stop_raises=dispatcher_mod.TerminateFailed("task pid 4242 survived SIGKILL"),
    )
    with caplog.at_level(logging.ERROR, logger=dispatcher_mod.logger.name):
        assert _after(worker) is expected
    assert _messages(caplog) == [
        "task t1: dispatch loop raised RuntimeError: boom; stopping worker pid=4242",
        "task t1: could not stop worker pid=4242 after its dispatch loop failed: "
        "task pid 4242 survived SIGKILL",
    ]


def test_drain_keeps_read_ahead_lines_and_gives_up_on_a_held_pipe(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reading the first line through the iterator reads the second ahead;
    the drain still returns it. Another process keeps the pipe open, so the
    drain returns after its timeout with what it read."""
    read_fd, write_fd = os.pipe()
    straggler = subprocess.Popen(["sleep", "30"], stdout=write_fd)
    os.write(write_fd, b"first\nsecond\n")
    os.close(write_fd)
    stdout = open(read_fd, encoding="utf-8", errors="replace")  # noqa: SIM115 - the drain closes it
    worker = SimpleNamespace(pid=4242, stdout=stdout, stderr=None)
    try:
        first = next(iter(stdout))
        with caplog.at_level(logging.WARNING, logger=dispatcher_mod.logger.name):
            out, err = dispatcher_mod._drain_pipes(worker, task_id="t1", timeout_s=1)  # type: ignore[arg-type]
    finally:
        straggler.kill()
        straggler.wait(timeout=10)

    assert (first, out, err) == ("first\n", "second\n", "")
    assert _messages(caplog) == [
        "task t1: worker pid=4242's output is still open 1s after it stopped; "
        "recording its run from what was read"
    ]
