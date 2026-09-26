"""A stop or drain signal ends the supervisor's inter-tick sleep (ADR-0025).

``start_daemon`` sleeps ``[usage].poll_interval_s`` (60 s by default)
between ticks, and its signal handlers only set flags. ``time.sleep``
resumes after a handler that does not raise (PEP 475), so until
2026-09-26 a stop requested during the sleep waited the sleep out. On the
live runner ``supervisor stop`` took 46 s, and a ``systemctl stop`` that
waits longer than the unit's ``TimeoutStopSec=30`` ends in SIGKILL.

These tests run the real loop with its handlers installed and a poll
interval of :data:`_POLL_INTERVAL_S`, send each signal the way an operator
or systemd does, and time how long ``start_daemon`` takes to return. The
old loop returned only when the sleep ran out, so each of them failed
after about :data:`_POLL_INTERVAL_S` seconds.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from claude_task_runner.config.loader import load_settings
from claude_task_runner.config.schema import Settings
from claude_task_runner.queue.store import queue_runtime_dir, todo_dir
from claude_task_runner.runner import force_dispatch as fd_mod
from claude_task_runner.runner import orchestrator as orch_mod
from claude_task_runner.runner.in_flight import DispatchSlot
from claude_task_runner.supervisor import daemon as daemon_mod
from claude_task_runner.supervisor.daemon import start_daemon
from claude_task_runner.usage.models import UsageReading, WindowReading
from claude_task_runner.usage.source import FakeUsageSource

# Long enough that a loop which sleeps it out fails every bound below.
_POLL_INTERVAL_S = 30.0
# How soon start_daemon must return. The sleep sees a flag within one
# SIGNAL_CHECK_INTERVAL_S (0.5 s; test_daemon_helpers pins the slicing).
# The rest is slack for a loaded host, where the fsync in each tick's
# snapshot write can take a second or two, and a drain runs one more
# tick after its signal.
_PROMPT_S = 5.0
# How long a test watches for a tick that must not happen.
_WATCH_S = 1.5

pytestmark = pytest.mark.usefixtures("harmless_signal_handlers", "private_global_lock")


def _queue(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


def _settings() -> Settings:
    base = load_settings(None)
    usage = base.usage.model_copy(update={"poll_interval_s": _POLL_INTERVAL_S})
    return base.model_copy(update={"usage": usage})


def _reading() -> UsageReading:
    captured = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    return UsageReading(
        captured_at=captured,
        five_hour=WindowReading(
            utilization_pct=20,
            resets_at_raw="x",
            resets_at=datetime(2026, 9, 26, 17, 0, tzinfo=UTC),
        ),
        seven_day=WindowReading(
            utilization_pct=20,
            resets_at_raw="x",
            resets_at=datetime(2026, 9, 30, 12, 0, tzinfo=UTC),
        ),
    )


class _CountingSource(FakeUsageSource):
    """One usage poll per tick, so ``reads`` counts the ticks that ran.

    ``on_read`` runs inside the poll with the new count, which puts a
    signal sent from it in the middle of a tick."""

    def __init__(self, on_read: Callable[[int], None] | None = None) -> None:
        super().__init__([_reading()])
        self.reads = 0
        self._on_read = on_read

    def read(self) -> UsageReading:
        self.reads += 1
        if self._on_read is not None:
            self._on_read(self.reads)
        return super().read()


class _SleepSpy:
    """Stands in for ``sleep_for_next_poll``: counts the calls and passes
    each one to the real function, so the daemon still really sleeps."""

    def __init__(self) -> None:
        self._real = daemon_mod.sleep_for_next_poll
        self._cond = threading.Condition()
        self.calls = 0

    def __call__(self, **kwargs: Any) -> None:
        with self._cond:
            self.calls += 1
            self._cond.notify_all()
        self._real(**kwargs)

    def wait_for_call(self, n: int) -> bool:
        """Block until the daemon has started its ``n``-th sleep."""
        with self._cond:
            return self._cond.wait_for(lambda: self.calls >= n, timeout=10.0)


class _CallCounter:
    """Counts calls to a daemon collaborator and passes them on."""

    def __init__(self, real: Callable[..., Any]) -> None:
        self._real = real
        self.calls = 0

    def __call__(self, **kwargs: Any) -> Any:
        self.calls += 1
        return self._real(**kwargs)


def _spy_on_sleep(monkeypatch: pytest.MonkeyPatch) -> _SleepSpy:
    spy = _SleepSpy()
    monkeypatch.setattr(daemon_mod, "sleep_for_next_poll", spy)
    return spy


def _send(signum: int, sent_at: dict[int, float]) -> None:
    """Signal this process, as ``supervisor stop`` / ``drain`` / ``kill`` do."""
    sent_at[signum] = time.monotonic()
    os.kill(os.getpid(), signum)


def _run(
    tmp_path: Path,
    source: _CountingSource,
    *,
    max_ticks: int,
    fire: Callable[[], None] | None = None,
) -> float:
    """Run ``start_daemon`` in this (the main) thread, with ``fire`` in a
    thread beside it; return the monotonic time it returned at.

    ``max_ticks`` is only a backstop that ends the loop if a signal is
    lost. The thread is joined before the test goes on, so no signal
    arrives after the fixture has put pytest's handlers back."""
    thread = threading.Thread(target=fire, daemon=True) if fire is not None else None
    if thread is not None:
        thread.start()
    try:
        start_daemon(
            queue_dir=_queue(tmp_path),
            settings=_settings(),
            source=source,
            pending_count_fn=lambda: 0,
            in_flight_count_fn=lambda: 0,
            install_signal_handlers=True,
            max_ticks=max_ticks,
        )
        return time.monotonic()
    finally:
        if thread is not None:
            thread.join(timeout=2 * _POLL_INTERVAL_S)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_a_stop_signal_ends_the_sleep_promptly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signum: int
) -> None:
    """``supervisor stop`` (SIGTERM) or Ctrl-C (SIGINT) during the sleep
    returns at once, without another tick."""
    sleep_spy = _spy_on_sleep(monkeypatch)
    source = _CountingSource()
    sent_at: dict[int, float] = {}

    def fire() -> None:
        if sleep_spy.wait_for_call(1):
            time.sleep(0.2)
            _send(signum, sent_at)

    returned_at = _run(tmp_path, source, max_ticks=1, fire=fire)

    assert signum in sent_at, "the daemon never reached its sleep"
    assert returned_at - sent_at[signum] < _PROMPT_S
    assert source.reads == 1
    assert sleep_spy.calls == 1


def test_sigusr1_starts_the_drain_without_waiting_out_the_sleep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``supervisor drain`` (SIGUSR1) with nothing in flight: the drain
    tick runs at once and the daemon exits from it."""
    sleep_spy = _spy_on_sleep(monkeypatch)
    source = _CountingSource()
    sent_at: dict[int, float] = {}

    def fire() -> None:
        if sleep_spy.wait_for_call(1):
            time.sleep(0.2)
            _send(signal.SIGUSR1, sent_at)

    with caplog.at_level("INFO", logger="claude_task_runner.supervisor.daemon"):
        # max_ticks=2 leaves room for the drain tick, so the exit below
        # comes from the drain and not from the backstop.
        returned_at = _run(tmp_path, source, max_ticks=2, fire=fire)

    assert signal.SIGUSR1 in sent_at, "the daemon never reached its sleep"
    assert returned_at - sent_at[signal.SIGUSR1] < _PROMPT_S
    assert source.reads == 2
    assert any("drain complete: in_flight=0" in r.message for r in caplog.records)


def test_a_drain_with_work_in_flight_keeps_the_poll_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGUSR1 wakes the loop once. While the drain waits for a task still
    in flight, it ticks at the poll interval, not back to back."""
    sleep_spy = _spy_on_sleep(monkeypatch)
    source = _CountingSource()
    sent_at: dict[int, float] = {}
    release = threading.Event()
    worker = threading.Thread(target=release.wait, daemon=True)
    worker.start()

    def dispatch_keeping_one_in_flight(**kwargs: Any) -> Any:
        slots: dict[str, DispatchSlot] = kwargs["in_flight_slots"]
        slots.setdefault(
            "t1",
            DispatchSlot(
                task_id="t1",
                account="personal",
                started_at=datetime(2026, 9, 26, 12, 0, tzinfo=UTC),
                thread=worker,
            ),
        )
        return kwargs["snapshot"]

    monkeypatch.setattr(orch_mod, "tick_dispatch", dispatch_keeping_one_in_flight)
    reads_after_drain: list[int] = []

    def fire() -> None:
        if not sleep_spy.wait_for_call(1):
            return
        time.sleep(0.2)
        _send(signal.SIGUSR1, sent_at)
        # The drain tick runs, finds t1 still in flight, and sleeps again.
        if sleep_spy.wait_for_call(2):
            time.sleep(_WATCH_S)
            reads_after_drain.append(source.reads)
        _send(signal.SIGTERM, sent_at)

    try:
        returned_at = _run(tmp_path, source, max_ticks=4, fire=fire)
    finally:
        release.set()
        worker.join(timeout=5)

    assert reads_after_drain == [2]
    assert returned_at - sent_at[signal.SIGTERM] < _PROMPT_S
    assert source.reads == 2


def test_sighup_waits_for_the_next_scheduled_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SIGHUP only marks a reload for the next tick; it does not end the
    sleep, so no extra usage poll runs. A later SIGTERM still ends it."""
    sleep_spy = _spy_on_sleep(monkeypatch)
    source = _CountingSource()
    sent_at: dict[int, float] = {}
    reads_after_sighup: list[int] = []

    def fire() -> None:
        if not sleep_spy.wait_for_call(1):
            return
        time.sleep(0.2)
        _send(signal.SIGHUP, sent_at)
        time.sleep(_WATCH_S)
        reads_after_sighup.append(source.reads)
        _send(signal.SIGTERM, sent_at)

    with caplog.at_level("INFO", logger="claude_task_runner.supervisor.daemon"):
        returned_at = _run(tmp_path, source, max_ticks=1, fire=fire)

    assert any("caught SIGHUP; reload pending" in r.message for r in caplog.records)
    assert reads_after_sighup == [1]
    assert returned_at - sent_at[signal.SIGTERM] < _PROMPT_S
    assert sleep_spy.calls == 1


def test_a_stop_during_the_usage_poll_skips_dispatch_and_the_sleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop that lands while the tick polls usage ends the tick before
    anything is dispatched. A dispatch then would start work, and with
    adoption on a dispatch thread, only for the exit to abandon it."""
    sleep_spy = _spy_on_sleep(monkeypatch)
    tick_dispatch = _CallCounter(orch_mod.tick_dispatch)
    monkeypatch.setattr(orch_mod, "tick_dispatch", tick_dispatch)
    tick_consume = _CallCounter(fd_mod.tick_consume)
    monkeypatch.setattr(fd_mod, "tick_consume", tick_consume)

    def stop_mid_poll(reads: int) -> None:
        if reads == 1:
            signal.raise_signal(signal.SIGTERM)

    source = _CountingSource(on_read=stop_mid_poll)
    started_at = time.monotonic()
    returned_at = _run(tmp_path, source, max_ticks=1)

    assert returned_at - started_at < _PROMPT_S
    assert source.reads == 1
    assert tick_consume.calls == 0
    assert tick_dispatch.calls == 0
    assert sleep_spy.calls == 0


def test_a_stop_after_dispatch_skips_the_sleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop that lands after the dispatch phase is checked for again
    just before the sleep, so the tick ends without sleeping."""
    sleep_spy = _spy_on_sleep(monkeypatch)

    def dispatch_then_stop(**kwargs: Any) -> Any:
        signal.raise_signal(signal.SIGTERM)
        return kwargs["snapshot"]

    monkeypatch.setattr(orch_mod, "tick_dispatch", dispatch_then_stop)
    source = _CountingSource()
    started_at = time.monotonic()
    returned_at = _run(tmp_path, source, max_ticks=1)

    assert returned_at - started_at < _PROMPT_S
    assert source.reads == 1
    assert sleep_spy.calls == 0
