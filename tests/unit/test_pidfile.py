"""Tests for supervisor.pidfile — global lock + per-queue PID writeout."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from claude_task_runner.supervisor.pidfile import (
    GlobalLockProbe,
    SupervisorAlreadyRunning,
    acquire_global_lock,
    clear_pid_file,
    global_lock_path,
    is_pid_alive,
    probe_global_lock,
    read_existing_pid,
    write_pid_file,
)


class TestReadExistingPid:
    def test_missing_file(self, tmp_path: Path) -> None:
        assert read_existing_pid(tmp_path / "nope.lock") is None

    def test_valid_pid(self, tmp_path: Path) -> None:
        p = tmp_path / "lock"
        p.write_text("1234\n")
        assert read_existing_pid(p) == 1234

    def test_garbage_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "lock"
        p.write_text("not a number")
        assert read_existing_pid(p) is None

    def test_empty_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "lock"
        p.write_text("")
        assert read_existing_pid(p) is None


class TestIsPidAlive:
    def test_self(self) -> None:
        assert is_pid_alive(os.getpid()) is True

    def test_init_pid(self) -> None:
        # PID 1 always exists (init / systemd).
        assert is_pid_alive(1) is True

    def test_invalid_pid(self) -> None:
        assert is_pid_alive(0) is False
        assert is_pid_alive(-1) is False

    def test_implausibly_large_pid(self) -> None:
        # pid_max on Linux is typically 4194304; 9999999 is comfortably beyond.
        assert is_pid_alive(9_999_999) is False


class TestAcquireGlobalLock:
    def test_basic_acquire_release(self, tmp_path: Path) -> None:
        path = tmp_path / "global.lock"
        with acquire_global_lock(lock_path=path):
            assert path.exists()
            assert read_existing_pid(path) == os.getpid()
        # After exit, file persists but unlocked. Verify a re-acquire works.
        with acquire_global_lock(lock_path=path):
            pass

    def test_second_acquire_blocks(self, tmp_path: Path) -> None:
        path = tmp_path / "global.lock"
        # Spawn a child holding the lock.
        helper = textwrap.dedent(f"""
            import sys, time, fcntl
            from pathlib import Path
            sys.path.insert(0, {str(Path(__file__).parent.parent.parent / "src")!r})
            from claude_task_runner.supervisor.pidfile import acquire_global_lock
            with acquire_global_lock(lock_path=Path({str(path)!r})):
                Path({str(path)!r} + ".ready").write_text("ok")
                time.sleep(5)
        """)
        proc = subprocess.Popen(
            [sys.executable, "-c", helper],
        )
        try:
            ready = Path(str(path) + ".ready")
            for _ in range(30):
                if ready.exists():
                    break
                time.sleep(0.1)
            else:
                pytest.fail("child never acquired lock")

            with (
                pytest.raises(SupervisorAlreadyRunning) as exc_info,
                acquire_global_lock(lock_path=path),
            ):
                pass
            assert exc_info.value.existing_pid == proc.pid
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_release_lets_next_acquire_proceed(self, tmp_path: Path) -> None:
        path = tmp_path / "global.lock"
        with acquire_global_lock(lock_path=path):
            pass
        # Lock should be released; the next acquire succeeds.
        with acquire_global_lock(lock_path=path):
            assert read_existing_pid(path) == os.getpid()


class TestPerQueuePidFile:
    def test_write_and_clear(self, tmp_path: Path) -> None:
        path = tmp_path / "supervisor.pid"
        write_pid_file(path)
        assert read_existing_pid(path) == os.getpid()
        clear_pid_file(path)
        assert not path.exists()

    def test_clear_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "nope.pid"
        clear_pid_file(path)  # No-op on missing file.
        clear_pid_file(path)


FREE = GlobalLockProbe(held=False, pid=None)


class TestProbeGlobalLock:
    """``probe_global_lock`` asks the lock, not the PID left in the file."""

    def test_missing_file_is_free_and_not_created(self, tmp_path: Path) -> None:
        path = tmp_path / "global.lock"
        assert probe_global_lock(lock_path=path) == FREE
        assert not path.exists()

    def test_file_left_by_an_exited_supervisor_is_free(self, tmp_path: Path) -> None:
        """The file keeps the last holder's PID; here it is even a live one."""
        path = tmp_path / "global.lock"
        with acquire_global_lock(lock_path=path):
            pass
        assert read_existing_pid(path) == os.getpid()
        assert probe_global_lock(lock_path=path) == FREE

    def test_lock_held_in_this_process(self, tmp_path: Path) -> None:
        """flock locks belong to an open file, so a second open conflicts even here."""
        path = tmp_path / "global.lock"
        with acquire_global_lock(lock_path=path):
            assert probe_global_lock(lock_path=path) == GlobalLockProbe(held=True, pid=os.getpid())

    def test_lock_held_by_another_process(self, tmp_path: Path) -> None:
        path = tmp_path / "global.lock"
        ready = tmp_path / "ready"
        helper = textwrap.dedent(f"""
            import sys, time
            from pathlib import Path
            sys.path.insert(0, {str(Path(__file__).parent.parent.parent / "src")!r})
            from claude_task_runner.supervisor.pidfile import acquire_global_lock
            with acquire_global_lock(lock_path=Path({str(path)!r})):
                Path({str(ready)!r}).write_text("ok")
                time.sleep(30)
        """)
        proc = subprocess.Popen([sys.executable, "-c", helper])
        try:
            for _ in range(100):
                if ready.exists():
                    break
                time.sleep(0.1)
            else:
                pytest.fail("child never acquired the lock")
            assert probe_global_lock(lock_path=path) == GlobalLockProbe(held=True, pid=proc.pid)
        finally:
            proc.kill()
            proc.wait(timeout=5)
        # The OS released the killed child's lock.
        assert probe_global_lock(lock_path=path) == FREE

    def test_held_lock_whose_pid_is_not_written_yet(self, tmp_path: Path) -> None:
        """acquire_global_lock writes its PID just after it locks."""
        path = tmp_path / "global.lock"
        path.write_text("", encoding="utf-8")
        with path.open("a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            assert probe_global_lock(lock_path=path) == GlobalLockProbe(held=True, pid=None)

    def test_probe_releases_the_lock(self, tmp_path: Path) -> None:
        path = tmp_path / "global.lock"
        path.write_text("1\n", encoding="utf-8")
        assert probe_global_lock(lock_path=path) == FREE
        with acquire_global_lock(lock_path=path):
            assert read_existing_pid(path) == os.getpid()

    @pytest.mark.skipif(os.geteuid() == 0, reason="root is not denied by file permissions")
    def test_unreadable_file_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "global.lock"
        path.write_text("", encoding="utf-8")
        path.chmod(0o000)
        try:
            with pytest.raises(PermissionError):
                probe_global_lock(lock_path=path)
        finally:
            path.chmod(0o600)

    def test_default_path_is_the_per_user_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        assert global_lock_path() == tmp_path / ".claude_task_runner" / "global.lock"
        assert probe_global_lock() == FREE
        with acquire_global_lock():
            assert probe_global_lock() == GlobalLockProbe(held=True, pid=os.getpid())
