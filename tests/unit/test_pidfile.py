"""Tests for supervisor.pidfile — global lock + per-queue PID writeout."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from claude_task_runner.supervisor.pidfile import (
    SupervisorAlreadyRunning,
    acquire_global_lock,
    clear_pid_file,
    is_pid_alive,
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
