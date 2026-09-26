"""The cron watchdog manages one queue, and never races the per-user lock.

Only one supervisor runs per user, because each one takes
``~/.claude_task_runner/global.lock``. A tick used to manage every queue in
``queues.json``: while one queue's supervisor held the lock, it spawned the
other queue's every minute, each spawn exited 2, and each counted toward
the crash-loop threshold that every queue shared. The tick now manages the
last queue registered, which ``install`` and ``watchdog register`` replace,
and records no restart while another process holds the lock.

The supervisors here hold the real lock, through a separate open file in
this process, so the tick's flock probe sees what it would see in use.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
from collections.abc import Iterator
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from claude_task_runner.cli import watchdog_cmd
from claude_task_runner.cli.watchdog_cmd import app
from claude_task_runner.clock import FakeClock
from claude_task_runner.cron.backoff import (
    WatchdogState,
    load_state,
    watchdog_state_path,
    write_state_atomic,
)
from claude_task_runner.cron.registry import queues_registry_path, register_queue
from claude_task_runner.supervisor.pidfile import (
    SupervisorAlreadyRunning,
    acquire_global_lock,
    global_lock_path,
)

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
CRON_INTERVAL_S = 60.0
DEAD_PID = 9_999_999
"""Above Linux's largest pid_max (2**22), so no process has it."""

VERDICT_RE = re.compile(r"watchdog queue=(\S+) alive=\S+ pid=\S+ verdict=(\w+)")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``Path.home()``, so the registry, the state and the lock are the test's."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _pid_path(queue: Path) -> Path:
    return queue / ".claude_task_runner" / "supervisor.pid"


class Supervisors:
    """Stands in for the supervisors that ``_spawn_supervisor`` starts.

    A started supervisor takes the real per-user lock and writes this
    process's PID, a live one, to its queue's pid file. A spawn while the
    lock is held exits at once without writing a pid file, as
    ``supervisor start`` does."""

    def __init__(self) -> None:
        self._running: dict[Path, ExitStack] = {}
        self.spawns: list[Path] = []

    def start(self, queue: Path) -> None:
        stack = ExitStack()
        stack.enter_context(acquire_global_lock())
        self._running[queue] = stack
        _pid_path(queue).write_text(f"{os.getpid()}\n", encoding="utf-8")

    def crash(self, queue: Path) -> None:
        """Release the lock and leave a stale pid file, as a SIGKILL or OOM kill does."""
        self._running.pop(queue).close()
        _pid_path(queue).write_text(f"{DEAD_PID}\n", encoding="utf-8")

    def running(self) -> list[Path]:
        return list(self._running)

    def spawn(self, queue_dir: Path, config: Path | None = None) -> int:
        self.spawns.append(queue_dir)
        # Refused: `supervisor start` exits 2 before it writes a pid file.
        with contextlib.suppress(SupervisorAlreadyRunning):
            self.start(queue_dir)
        return 4242

    def close(self) -> None:
        for stack in self._running.values():
            stack.close()


@pytest.fixture
def supervisors(monkeypatch: pytest.MonkeyPatch) -> Iterator[Supervisors]:
    sups = Supervisors()
    monkeypatch.setattr(watchdog_cmd, "_spawn_supervisor", sups.spawn)
    yield sups
    sups.close()


def _make_queue(home: Path, name: str) -> Path:
    queue = home / name
    (queue / ".claude_task_runner").mkdir(parents=True)
    return queue.resolve()


def _write_older_registry(queues: list[Path]) -> None:
    """Write ``queues.json`` listing several queues, as an older version could."""
    path = queues_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"queues": [str(q) for q in queues]}), encoding="utf-8")


def _set_clock(monkeypatch: pytest.MonkeyPatch, at: datetime) -> None:
    clock = FakeClock(at)
    monkeypatch.setattr(watchdog_cmd, "RealClock", lambda: clock)


def _tick(monkeypatch: pytest.MonkeyPatch, k: int, *args: str, offset_s: float = 1.0) -> str:
    """Run cron tick ``k`` (60 s apart, ``offset_s`` past the minute); return its output."""
    _set_clock(monkeypatch, T0 + timedelta(seconds=k * CRON_INTERVAL_S + offset_s))
    result = CliRunner().invoke(app, ["tick", *args])
    assert result.exit_code == 0, result.output
    return result.stdout


def _verdicts(output: str) -> dict[str, str]:
    return {Path(q).name: v for q, v in VERDICT_RE.findall(output)}


def _locked_line(queue: Path, holder: str) -> str:
    return (
        f" watchdog queue={queue} alive=False pid=None verdict=locked "
        f"detail='{holder} holds global.lock; starting none until it exits'\n"
    )


class TestReplacedQueueStillRunning:
    """Regressions for the three ways two registered queues went wrong."""

    def test_register_replaces_and_ticks_wait_for_the_lock(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch, supervisors: Supervisors
    ) -> None:
        """A second install used to add B beside A. Every tick then spawned B,
        and every spawn exited 2 on the lock A's supervisor held."""
        a, b = _make_queue(isolated_home, "a"), _make_queue(isolated_home, "b")
        register_queue(a)
        supervisors.start(a)
        assert register_queue(b) == [a]

        outputs = [_tick(monkeypatch, k) for k in range(10)]

        assert [_verdicts(out) for out in outputs] == [{"b": "locked"}] * 10
        assert all(
            out.endswith(_locked_line(b, f"another supervisor (pid {os.getpid()})"))
            for out in outputs
        )
        assert supervisors.spawns == []
        state = load_state(watchdog_state_path())
        assert state.queue == b
        assert state.recent_restarts == []

    @pytest.mark.parametrize(
        ("toml", "offsets"),
        [
            pytest.param("", {10: 0.5}, id="defaults-with-cron-start-jitter"),
            pytest.param("[watchdog]\nrestart_cooldown_s = 60\n", {}, id="restart_cooldown_s-60"),
        ],
    )
    def test_new_queue_starts_on_the_first_tick_the_lock_is_free(
        self,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        supervisors: Supervisors,
        toml: str,
        offsets: dict[int, float],
    ) -> None:
        """The refused spawns used to fill the restart history every queue
        shared, which held the next real restart in BACKOFF. The timelines
        are the ones that showed it: a tick that started 0.5 s earlier than
        the one five ticks before, and a 10-minute crash-loop window."""
        config = isolated_home / "claude_runner.toml"
        config.write_text(toml, encoding="utf-8")
        a, b = _make_queue(isolated_home, "a"), _make_queue(isolated_home, "b")
        register_queue(a)
        supervisors.start(a)
        register_queue(b)

        for k in range(10):
            out = _tick(monkeypatch, k, "--config", str(config), offset_s=offsets.get(k, 1.0))
            assert _verdicts(out) == {"b": "locked"}
        supervisors.crash(a)
        out = _tick(monkeypatch, 10, "--config", str(config), offset_s=offsets.get(10, 1.0))

        assert _verdicts(out) == {"b": "restart"}
        assert supervisors.spawns == [b]
        assert supervisors.running() == [b]

    def test_older_list_restarts_the_queue_that_ran_not_the_first_listed(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch, supervisors: Supervisors
    ) -> None:
        """B was registered before A, and A ran. A crash of A used to hand the
        lock to B, because B came first and the shared cooldown then held A."""
        a, b = _make_queue(isolated_home, "a"), _make_queue(isolated_home, "b")
        _write_older_registry([b, a])
        supervisors.start(a)

        before = [_verdicts(_tick(monkeypatch, k)) for k in range(10)]
        supervisors.crash(a)
        after = _tick(monkeypatch, 10)

        assert before == [{"a": "skip"}] * 10
        assert _verdicts(after) == {"a": "restart"}
        assert supervisors.spawns == [a]
        assert supervisors.running() == [a]
        assert (
            " watchdog: WARNING queues.json lists 2 queues, but one supervisor runs per "
            f"user, so the watchdog manages only the last one, {a}, and ignores {b}. To "
            "register just one, run: claude-task-runner watchdog register --queue <queue>\n"
        ) in after


class TestTick:
    def test_stale_pid_file_and_a_leftover_lock_file_restart(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch, supervisors: Supervisors
    ) -> None:
        """A killed supervisor leaves both files. The lock file still names a
        PID, here even a live one, but nothing holds the lock."""
        queue = _make_queue(isolated_home, "q")
        register_queue(queue)
        with acquire_global_lock():
            pass
        _pid_path(queue).write_text(f"{DEAD_PID}\n", encoding="utf-8")

        out = _tick(monkeypatch, 0)

        assert (
            f" watchdog queue={queue} alive=False pid={DEAD_PID} verdict=restart "
            "detail='restart approved (recent count: 1 of threshold 5)'\n"
        ) in out
        assert supervisors.spawns == [queue]

    def test_lock_held_before_the_holder_wrote_its_pid(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch, supervisors: Supervisors
    ) -> None:
        queue = _make_queue(isolated_home, "q")
        register_queue(queue)
        lock = global_lock_path()
        lock.write_text("", encoding="utf-8")
        with lock.open("a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            out = _tick(monkeypatch, 0)
        assert out.endswith(_locked_line(queue, "another supervisor"))
        assert supervisors.spawns == []

    def test_live_supervisor_is_skipped_without_probing_the_lock(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch, supervisors: Supervisors
    ) -> None:
        """A healthy tick never takes the lock, not even for a probe."""
        queue = _make_queue(isolated_home, "q")
        register_queue(queue)
        supervisors.start(queue)

        def _no_probe() -> None:
            raise AssertionError("probed the lock while the supervisor was alive")

        monkeypatch.setattr(watchdog_cmd.pidfile_mod, "probe_global_lock", _no_probe)
        assert _verdicts(_tick(monkeypatch, 0)) == {"q": "skip"}

    @pytest.mark.skipif(os.geteuid() == 0, reason="root is not denied by file permissions")
    def test_probe_failure_is_logged_and_the_restart_goes_ahead(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch, supervisors: Supervisors
    ) -> None:
        """As before the probe existed; the supervisor then says why in its own log."""
        queue = _make_queue(isolated_home, "q")
        register_queue(queue)
        lock = global_lock_path()
        lock.write_text("", encoding="utf-8")
        lock.chmod(0o000)
        try:
            out = _tick(monkeypatch, 0)
        finally:
            lock.chmod(0o600)
        assert (
            " watchdog: ERROR could not check global.lock ([Errno 13] Permission denied: "
            f"'{lock}'); deciding as if no other supervisor held it\n"
        ) in out
        assert _verdicts(out) == {"q": "restart"}
        assert supervisors.spawns == [queue]

    def test_older_list_with_the_managed_queue_listed_twice(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch, supervisors: Supervisors
    ) -> None:
        a, b = _make_queue(isolated_home, "a"), _make_queue(isolated_home, "b")
        _write_older_registry([a, b, a])
        out = _tick(monkeypatch, 0)
        assert (
            " watchdog: WARNING queues.json lists 2 queues, but one supervisor runs per "
            f"user, so the watchdog manages only the last one, {a}, and ignores {b}. To "
            "register just one, run: claude-task-runner watchdog register --queue <queue>\n"
        ) in out
        assert supervisors.spawns == [a]


class TestRestartHistory:
    """The restart history in watchdog_state.json belongs to one queue."""

    def test_a_queue_registered_in_its_place_starts_with_none(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch, supervisors: Supervisors
    ) -> None:
        a, b = _make_queue(isolated_home, "a"), _make_queue(isolated_home, "b")
        now = T0 + timedelta(seconds=1)
        crash_loop = [now - timedelta(seconds=s) for s in (250, 200, 150, 100, 50)]
        write_state_atomic(
            WatchdogState(queue=a, recent_restarts=crash_loop), watchdog_state_path()
        )
        register_queue(b)

        out = _tick(monkeypatch, 0)

        assert f" watchdog: now managing {b}, not {a}; its restart history starts empty\n" in out
        assert _verdicts(out) == {"b": "restart"}
        state = load_state(watchdog_state_path())
        assert state.queue == b
        assert state.recent_restarts == [now]

    def test_the_same_queue_keeps_its_history(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch, supervisors: Supervisors
    ) -> None:
        queue = _make_queue(isolated_home, "q")
        register_queue(queue)
        now = T0 + timedelta(seconds=1)
        last = now - timedelta(seconds=10)
        write_state_atomic(
            WatchdogState(queue=queue, recent_restarts=[last]), watchdog_state_path()
        )

        out = _tick(monkeypatch, 0)

        assert "now managing" not in out
        assert _verdicts(out) == {"q": "cooldown"}
        assert load_state(watchdog_state_path()).recent_restarts == [last]

    def test_history_from_before_it_named_a_queue_starts_over_quietly(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch, supervisors: Supervisors
    ) -> None:
        """It may hold other queues' refused restarts, so it is not trusted."""
        queue = _make_queue(isolated_home, "q")
        register_queue(queue)
        now = T0 + timedelta(seconds=1)
        write_state_atomic(
            WatchdogState(recent_restarts=[now - timedelta(seconds=10)]), watchdog_state_path()
        )

        out = _tick(monkeypatch, 0)

        assert "now managing" not in out
        assert _verdicts(out) == {"q": "restart"}
        assert load_state(watchdog_state_path()) == WatchdogState(
            queue=queue, recent_restarts=[now]
        )


class TestDryRun:
    """A dry run used to save the restart it approved and never performed."""

    def test_dry_run_saves_no_state(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch, supervisors: Supervisors
    ) -> None:
        queue = _make_queue(isolated_home, "q")
        register_queue(queue)

        out = _tick(monkeypatch, 0, "--dry-run")

        assert _verdicts(out) == {"q": "restart"}
        assert supervisors.spawns == []
        assert not watchdog_state_path().exists()

    def test_dry_run_leaves_an_existing_state_as_it_is(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch, supervisors: Supervisors
    ) -> None:
        """Nor does it save the reset for a newly registered queue."""
        a, b = _make_queue(isolated_home, "a"), _make_queue(isolated_home, "b")
        write_state_atomic(
            WatchdogState(queue=a, recent_restarts=[T0 - timedelta(seconds=30)]),
            watchdog_state_path(),
        )
        before = watchdog_state_path().read_bytes()
        register_queue(b)

        out = _tick(monkeypatch, 0, "--dry-run")

        assert _verdicts(out) == {"b": "restart"}
        assert watchdog_state_path().read_bytes() == before

    def test_the_real_tick_after_a_dry_run_is_not_held_back(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch, supervisors: Supervisors
    ) -> None:
        """A second later a saved phantom restart would have meant COOLDOWN."""
        queue = _make_queue(isolated_home, "q")
        register_queue(queue)
        _tick(monkeypatch, 0, "--dry-run")
        out = _tick(monkeypatch, 0, offset_s=2.0)
        assert _verdicts(out) == {"q": "restart"}
        assert supervisors.spawns == [queue]
