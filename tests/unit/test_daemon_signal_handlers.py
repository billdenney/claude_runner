"""``start_daemon`` puts back the signal handlers it replaced.

With ``install_signal_handlers=True`` it installs handlers for SIGTERM,
SIGINT, SIGHUP and SIGUSR1. Until 2026-09-26 it never removed them: after
a test that ran it, SIGTERM and SIGINT went to the finished daemon's
handler, so neither Ctrl-C nor ``timeout`` could stop the pytest run. These
tests install a distinct stand-in handler for each signal, run the daemon,
and check that each stand-in is back, the same object, after it returns
and after it raises. The autouse fixture in ``tests/conftest.py`` fails any
other test that leaks one; the last tests here check that fixture.
"""

from __future__ import annotations

import signal
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_task_runner.config.loader import load_settings
from claude_task_runner.config.schema import Settings
from claude_task_runner.queue.store import queue_runtime_dir, todo_dir
from claude_task_runner.supervisor import pidfile as pidfile_mod
from claude_task_runner.supervisor.daemon import start_daemon
from claude_task_runner.usage.models import UsageReading, WindowReading
from claude_task_runner.usage.source import FakeUsageSource

pytest_plugins = ["pytester"]
pytestmark = pytest.mark.usefixtures("private_global_lock")

_DAEMON_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGUSR1)

# The handler start_daemon installs for each signal, by qualified name.
_DAEMON_HANDLERS = {
    "SIGTERM": "start_daemon.<locals>._on_signal",
    "SIGINT": "start_daemon.<locals>._on_signal",
    "SIGHUP": "start_daemon.<locals>._on_sighup",
    "SIGUSR1": "start_daemon.<locals>._on_sigusr1",
}


class _StandIn:
    """A handler the test installs. The daemon must put it back, and
    nothing should ever call it."""

    def __init__(self, signum: int) -> None:
        self.name = signal.Signals(signum).name

    def __call__(self, _signum: int, _frame: object) -> None:
        raise AssertionError(f"the test's {self.name} handler ran")

    def __repr__(self) -> str:
        return f"_StandIn({self.name})"


@pytest.fixture
def stand_ins() -> Iterator[dict[int, _StandIn]]:
    """Install a stand-in for each signal, then put back the handlers that
    were there, before the conftest fixture compares them."""
    saved = {signum: signal.getsignal(signum) for signum in _DAEMON_SIGNALS}
    installed = {signum: _StandIn(signum) for signum in _DAEMON_SIGNALS}
    for signum, handler in installed.items():
        signal.signal(signum, handler)
    yield installed
    for signum, previous in saved.items():
        if previous is not None:
            signal.signal(signum, previous)


def _handlers(signals: tuple[int, ...] = _DAEMON_SIGNALS) -> dict[int, object]:
    return {signum: signal.getsignal(signum) for signum in signals}


def _names(handlers: dict[int, object]) -> dict[str, str]:
    """``{"SIGTERM": "<qualified name of its handler>", ...}``."""
    return {
        signal.Signals(signum).name: getattr(handler, "__qualname__", repr(handler))
        for signum, handler in handlers.items()
    }


def _queue(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


def _settings() -> Settings:
    """A short poll interval, so a ``max_ticks=1`` run returns at once."""
    base = load_settings(None)
    usage = base.usage.model_copy(update={"poll_interval_s": 0.01})
    return base.model_copy(update={"usage": usage})


def _reading() -> UsageReading:
    return UsageReading(
        captured_at=datetime(2026, 9, 26, 12, 0, tzinfo=UTC),
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


class _RecordingSource(FakeUsageSource):
    """Records every valid signal's handler at each usage poll, that is,
    while the daemon's loop runs."""

    def __init__(self) -> None:
        super().__init__([_reading()])
        self.seen: list[dict[int, object]] = []

    def read(self) -> UsageReading:
        self.seen.append(_handlers(tuple(signal.valid_signals())))
        return super().read()


def _run(
    tmp_path: Path,
    *,
    source: FakeUsageSource | None,
    install_signal_handlers: bool = True,
    pending_count_fn: Callable[[], int] = lambda: 0,
) -> None:
    start_daemon(
        queue_dir=_queue(tmp_path),
        settings=_settings(),
        source=source,
        pending_count_fn=pending_count_fn,
        in_flight_count_fn=lambda: 0,
        install_signal_handlers=install_signal_handlers,
        max_ticks=1,
    )


def test_handlers_are_put_back_when_start_daemon_returns(
    tmp_path: Path, stand_ins: dict[int, _StandIn]
) -> None:
    before = _handlers(tuple(signal.valid_signals()))
    source = _RecordingSource()

    _run(tmp_path, source=source)

    # While it ran, the daemon's own handlers were installed, for exactly
    # these four signals: a fifth would need adding to the conftest guard.
    assert len(source.seen) == 1
    during = source.seen[0]
    changed = {signum: during[signum] for signum in during if during[signum] != before[signum]}
    assert _names(changed) == _DAEMON_HANDLERS
    # Now the stand-ins are back, the same objects.
    assert _handlers() == stand_ins


def test_handlers_are_put_back_when_start_daemon_raises(
    tmp_path: Path, stand_ins: dict[int, _StandIn]
) -> None:
    """An exception inside the loop's startup, after the handlers went in."""
    during: list[dict[int, object]] = []

    def failing_pending_count() -> int:
        during.append(_handlers())
        raise RuntimeError("pending count failed")

    with pytest.raises(RuntimeError, match=r"^pending count failed$"):
        _run(tmp_path, source=FakeUsageSource([_reading()]), pending_count_fn=failing_pending_count)

    assert [_names(handlers) for handlers in during] == [_DAEMON_HANDLERS]
    assert _handlers() == stand_ins


def test_handlers_are_put_back_when_another_supervisor_holds_the_lock(
    tmp_path: Path, stand_ins: dict[int, _StandIn], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock is taken after the handlers go in, so a refused lock must
    put them back too."""
    real_acquire = pidfile_mod.acquire_global_lock
    during: list[dict[int, object]] = []

    def recording_acquire(**kwargs: Path) -> object:
        during.append(_handlers())
        return real_acquire(**kwargs)

    with real_acquire():
        monkeypatch.setattr(pidfile_mod, "acquire_global_lock", recording_acquire)
        with pytest.raises(pidfile_mod.SupervisorAlreadyRunning):
            _run(tmp_path, source=FakeUsageSource([_reading()]))

    assert [_names(handlers) for handlers in during] == [_DAEMON_HANDLERS]
    assert _handlers() == stand_ins


def test_handlers_are_left_alone_without_install_signal_handlers(
    tmp_path: Path, stand_ins: dict[int, _StandIn]
) -> None:
    source = _RecordingSource()

    _run(tmp_path, source=source, install_signal_handlers=False)

    assert [{signum: seen[signum] for signum in _DAEMON_SIGNALS} for seen in source.seen] == [
        stand_ins
    ]
    assert _handlers() == stand_ins


@pytest.mark.parametrize("signum", _DAEMON_SIGNALS, ids=lambda s: signal.Signals(s).name)
def test_the_conftest_guard_fails_a_test_that_leaks_a_handler(
    pytester: pytest.Pytester, signum: int
) -> None:
    """A test that leaves a handler behind errors at teardown, and the test
    after it runs with the handler that was there before."""
    conftest = Path(__file__).parents[1] / "conftest.py"
    pytester.makeconftest(conftest.read_text(encoding="utf-8"))
    name = signal.Signals(signum).name
    pytester.makepyfile(
        f"""
        import signal

        BEFORE = signal.getsignal(signal.{name})

        def test_leaks():
            signal.signal(signal.{name}, lambda signum, frame: None)

        def test_after():
            assert signal.getsignal(signal.{name}) is BEFORE
        """
    )

    result = pytester.runpytest_inprocess("-p", "no:cacheprovider")

    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(["ERROR test_the_conftest_guard_*.py::test_leaks*"])
    result.stdout.fnmatch_lines([f"E *Failed: test left a signal handler installed for {name}"])
