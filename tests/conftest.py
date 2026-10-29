"""Shared pytest fixtures."""

from __future__ import annotations

import os
import shutil
import signal
import tempfile
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.config.schema import Settings
from claude_task_runner.runner import dispatcher as dispatcher_mod

# Blocks and fails any signal a test sends to a process it did not start.
# pytester runs that gate in a subprocess for tests/unit/test_signal_gate.py.
pytest_plugins = ["pytester", "signal_gate"]

# The signals supervisor.daemon.start_daemon installs handlers for.
_DAEMON_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGUSR1)


@pytest.fixture(autouse=True)
def _no_leaked_signal_handlers() -> Iterator[None]:
    """Fail a test that leaves a handler installed for a signal the daemon uses.

    ``start_daemon(install_signal_handlers=True)`` used to leave its
    handlers behind. After a test that ran it, SIGTERM and SIGINT went to
    the finished daemon's handler, so neither Ctrl-C nor ``timeout`` could
    stop the run; only SIGKILL could. The handlers found before the test
    are put back whether or not it passes, so one leak does not fail every
    test after it.
    """
    before = {signum: signal.getsignal(signum) for signum in _DAEMON_SIGNALS}
    yield
    leaked = [
        signal.Signals(signum).name
        for signum in _DAEMON_SIGNALS
        if signal.getsignal(signum) != before[signum]
    ]
    for signum, handler in before.items():
        # None: a handler installed outside Python, which cannot be put back.
        if handler is not None:
            signal.signal(signum, handler)
    if leaked:
        pytest.fail(f"test left a signal handler installed for {', '.join(leaked)}")


_NO_PROC = Path(__file__).parent / "no-such-proc"
"""A ``/proc`` root that does not exist."""


@pytest.fixture(autouse=True)
def _worker_probe_off_host_proc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep :func:`dispatcher.probe_worker` off the host's ``/proc``.

    Tests record made-up worker pids and stub ``dispatcher._pid_alive`` for
    them. Read from the real ``/proc``, such a pid would be judged by
    whatever process has it on the machine running the tests. With no
    ``/proc`` the probe answers from ``_pid_alive`` alone. Tests of the
    identity checks set ``dispatcher._PROC_ROOT`` themselves: to a fake
    tree, or back to ``/proc`` for processes they start.
    """
    monkeypatch.setattr(dispatcher_mod, "_PROC_ROOT", _NO_PROC)


@pytest.fixture
def far_future_mtime_dir() -> Iterator[Path]:
    """A fresh directory whose filesystem stores an mtime past year 9999 as given.

    ext4 clamps such an mtime to year 2446, so the directory is on tmpfs
    (``/dev/shm``), which keeps 64-bit seconds. It is removed at teardown,
    and the test is skipped where no writable tmpfs keeps the mtime.
    """
    shm = Path("/dev/shm")
    if not (shm.is_dir() and os.access(shm, os.W_OK)):
        pytest.skip("no writable /dev/shm")
    root = Path(tempfile.mkdtemp(dir=shm, prefix="ctr-mtime-"))
    try:
        probe = root / "probe"
        probe.write_text("x")
        os.utime(probe, (253402300800, 253402300800))
        if probe.stat().st_mtime != 253402300800:
            pytest.skip("this /dev/shm does not keep an mtime past year 9999")
        probe.unlink()
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def raise_in_dispatch_loop(monkeypatch: pytest.MonkeyPatch) -> Callable[[int | None], None]:
    """Arm ``dispatcher._dispatch_loop`` to raise ``RuntimeError("boom")`` from
    its own iteration, after it has consumed ``lines_before`` lines, or after
    the last one for ``None``. The real loop runs until then."""

    def arm(lines_before: int | None) -> None:
        real_loop = dispatcher_mod._dispatch_loop

        def _loop(*, lines: Iterable[str], **kwargs: Any) -> Any:
            def _failing() -> Iterator[str]:
                for count, line in enumerate(lines, start=1):
                    yield line
                    if count == lines_before:
                        raise RuntimeError("boom")
                raise RuntimeError("boom")

            return real_loop(lines=_failing(), **kwargs)

        monkeypatch.setattr(dispatcher_mod, "_dispatch_loop", _loop)

    return arm


@pytest.fixture
def real_proc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let :func:`dispatcher.probe_worker` read the host's ``/proc``, for a
    test whose recorded workers are processes it started itself."""
    if not Path("/proc/self/stat").exists():
        pytest.skip("needs a Linux /proc")
    monkeypatch.setattr(dispatcher_mod, "_PROC_ROOT", Path("/proc"))


def _ignore_signal(_signum: int, _frame: object) -> None:
    """The handler :func:`harmless_signal_handlers` installs."""


@pytest.fixture
def harmless_signal_handlers() -> Iterator[None]:
    """Give the daemon's signals handlers that do nothing, for the test.

    ``start_daemon`` puts back the handlers it found when it returns. For a
    test whose thread signals this process while the daemon runs, those
    must be harmless: a signal that arrives after the daemon has returned,
    which only a regression would cause, then fails the test instead of
    ending the pytest run. Pytest's handlers are put back afterwards.
    """
    saved = {signum: signal.getsignal(signum) for signum in _DAEMON_SIGNALS}
    for signum in _DAEMON_SIGNALS:
        signal.signal(signum, _ignore_signal)
    yield
    for signum, handler in saved.items():
        if handler is not None:
            signal.signal(signum, handler)


@pytest.fixture
def private_global_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Take the host-wide supervisor lock in ``tmp_path``, so a supervisor
    running on this host does not block a test that runs ``start_daemon``.
    Returns the lock's path."""
    lock = tmp_path / "global.lock"
    monkeypatch.setattr(
        "claude_task_runner.supervisor.pidfile.global_lock_path",
        lambda: lock,
    )
    return lock


@pytest.fixture
def fake_clock() -> FakeClock:
    """A FakeClock anchored at 2026-05-03T18:00:00Z."""
    return FakeClock(datetime(2026, 5, 3, 18, 0, 0, tzinfo=UTC))


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def usage_fixtures_dir(fixtures_dir: Path) -> Path:
    return fixtures_dir / "usage"


@pytest.fixture
def default_settings() -> Settings:
    """The package defaults loaded with no per-queue overrides."""
    return load_settings(None)


@pytest.fixture(params=["libyaml", "pure-python"])
def yaml_backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run a test once per YAML loader ``queue.store`` can select.

    ``pure-python`` deletes ``yaml.CSafeLoader``, which is what a PyYAML
    built without LibYAML looks like, so the store falls back to
    ``SafeLoader``. ``libyaml`` is skipped on such a build.
    """
    if request.param == "pure-python":
        monkeypatch.delattr(yaml, "CSafeLoader", raising=False)
    elif not yaml.__with_libyaml__:
        pytest.skip("PyYAML was built without LibYAML")
    return str(request.param)
