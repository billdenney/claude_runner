"""Tests for SIGHUP-triggered live reload in supervisor/daemon.py.

The reload itself is broken into two pieces:

* :func:`_apply_sighup_reload` — pure function: given current settings,
  config path, and a pending-count callable, return new settings + new
  count. All error paths land here (bad TOML, missing file, OSError),
  which is what we cover in unit tests.
* The signal handler + flag in :func:`start_daemon` — exercised by an
  integration-style test that runs the daemon with ``max_ticks`` and
  sends ``SIGHUP`` to the test process.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_task_runner.config.loader import load_settings
from claude_task_runner.queue.store import queue_runtime_dir
from claude_task_runner.supervisor.daemon import (
    _apply_sighup_reload,
    _diff_settings,
    start_daemon,
)
from claude_task_runner.usage.models import UsageReading, WindowReading
from claude_task_runner.usage.source import FakeUsageSource

# ---------------------------------------------------------------------------
# _diff_settings — coarse change-count used by the reload summary log.
# ---------------------------------------------------------------------------


def test_diff_settings_zero_when_identical() -> None:
    s = load_settings(None)
    assert _diff_settings(s, s) == 0


def test_diff_settings_nonzero_when_different() -> None:
    s = load_settings(None)
    # Tweak one top-level group.
    new_concurrency = s.concurrency.model_copy(update={"max_concurrency": 99})
    s2 = s.model_copy(update={"concurrency": new_concurrency})
    assert _diff_settings(s, s2) == 1


# ---------------------------------------------------------------------------
# _apply_sighup_reload
# ---------------------------------------------------------------------------


def _write_toml(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_apply_sighup_reload_success_returns_new_settings_and_pending(
    tmp_path: Path,
) -> None:
    config = tmp_path / "claude_runner.toml"
    _write_toml(config, "[concurrency]\nmax_concurrency = 7\n")

    current = load_settings(None)
    pending_call_count = {"n": 0}

    def pending_fn() -> int:
        pending_call_count["n"] += 1
        return 42

    new_settings, new_pending = _apply_sighup_reload(
        current=current,
        config_path=config,
        prior_pending=10,
        pending_count_fn=pending_fn,
    )
    assert new_settings.concurrency.max_concurrency == 7
    assert new_pending == 42
    assert pending_call_count["n"] == 1


def test_apply_sighup_reload_keeps_old_on_malformed_toml(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = tmp_path / "claude_runner.toml"
    _write_toml(config, "this is not [valid toml")
    current = load_settings(None)

    with caplog.at_level("WARNING", logger="claude_task_runner.supervisor.daemon"):
        new_settings, new_pending = _apply_sighup_reload(
            current=current,
            config_path=config,
            prior_pending=10,
            pending_count_fn=lambda: 999,  # MUST NOT be consulted
        )
    assert new_settings is current
    assert new_pending == 10  # prior_pending preserved
    assert any("reload failed" in r.message for r in caplog.records)


def test_apply_sighup_reload_keeps_old_on_missing_file(tmp_path: Path) -> None:
    """A config path that doesn't exist routes through ConfigError, same as bad TOML."""
    current = load_settings(None)
    missing = tmp_path / "never_existed.toml"
    new_settings, new_pending = _apply_sighup_reload(
        current=current,
        config_path=missing,
        prior_pending=5,
        pending_count_fn=lambda: 999,
    )
    assert new_settings is current
    assert new_pending == 5


def test_apply_sighup_reload_emits_summary_log_with_counts(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = tmp_path / "claude_runner.toml"
    _write_toml(config, "[concurrency]\nmax_concurrency = 8\n")
    current = load_settings(None)

    with caplog.at_level("INFO", logger="claude_task_runner.supervisor.daemon"):
        _apply_sighup_reload(
            current=current,
            config_path=config,
            prior_pending=5,
            pending_count_fn=lambda: 7,
        )
    messages = [r.message for r in caplog.records]
    # Documented log line shape from the supervisor --help text.
    assert any(
        "SIGHUP received: reloaded config" in m and "rescanned queue" in m and "2 new tasks" in m
        for m in messages
    ), messages


def test_apply_sighup_reload_idempotent_no_changes_log_zero(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A SIGHUP with no actual config change still logs successfully but
    with ``0 changes`` — the operator can verify the handler ran."""
    config = tmp_path / "claude_runner.toml"
    _write_toml(config, "")  # empty = same as defaults
    current = load_settings(None)

    with caplog.at_level("INFO", logger="claude_task_runner.supervisor.daemon"):
        new_settings, _new_pending = _apply_sighup_reload(
            current=current,
            config_path=config,
            prior_pending=5,
            pending_count_fn=lambda: 5,
        )
    assert _diff_settings(current, new_settings) == 0
    assert any("0 changes" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Integration: run start_daemon, send SIGHUP, check reload took effect.
# ---------------------------------------------------------------------------


def _reading(pct: int) -> UsageReading:
    return UsageReading(
        captured_at=datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC),
        five_hour=WindowReading(
            utilization_pct=pct,
            resets_at_raw="x",
            resets_at=datetime(2026, 5, 16, 17, 0, 0, tzinfo=UTC),
        ),
        seven_day=WindowReading(
            utilization_pct=pct,
            resets_at_raw="x",
            resets_at=datetime(2026, 5, 20, 11, 0, 0, tzinfo=UTC),
        ),
    )


def test_start_daemon_sighup_triggers_reload_on_next_tick(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: starting the daemon, sending SIGHUP, and seeing the log
    line on the next tick. Uses ``max_ticks`` to bound the test runtime.

    Pid-file machinery is exercised here — pid-file write / clear — but
    the host-wide global lock is redirected to ``tmp_path`` so the test
    doesn't fight a real supervisor that may be running on the host.
    """
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)

    # Redirect the host-wide lock to this tmp dir so the test isn't
    # blocked by a real supervisor.
    test_lock = tmp_path / "test_global.lock"
    monkeypatch.setattr(
        "claude_task_runner.supervisor.pidfile.global_lock_path",
        lambda: test_lock,
    )

    config = tmp_path / "claude_runner.toml"
    # poll_interval_s lives in the toml so the SIGHUP reload preserves it;
    # otherwise the reload re-reads defaults (60 s) and the test hangs for
    # 8 minutes waiting for ticks to elapse.
    _write_toml(config, "[usage]\npoll_interval_s = 0.05\n")
    settings = load_settings(config)

    source = FakeUsageSource([_reading(20)])
    sent_event = threading.Event()

    # Send SIGHUP from a background thread after the daemon has started its
    # first tick. The test process's signal disposition is set by
    # ``start_daemon(install_signal_handlers=True)``.
    def fire_sighup() -> None:
        time.sleep(0.2)
        _write_toml(
            config,
            "[usage]\npoll_interval_s = 0.05\n[concurrency]\nmax_concurrency = 11\n",
        )
        os.kill(os.getpid(), signal.SIGHUP)
        sent_event.set()

    fire_thread = threading.Thread(target=fire_sighup, daemon=True)
    fire_thread.start()

    with caplog.at_level("INFO", logger="claude_task_runner.supervisor.daemon"):
        start_daemon(
            queue_dir=qd,
            settings=settings,
            source=source,
            pending_count_fn=lambda: 0,
            in_flight_count_fn=lambda: 0,
            install_signal_handlers=True,
            max_ticks=8,
            config_path=config,
        )

    fire_thread.join(timeout=2)
    assert sent_event.is_set(), "SIGHUP fire thread did not run"
    # Reload log line must have been emitted on the post-SIGHUP tick.
    assert any("SIGHUP received: reloaded config" in r.message for r in caplog.records), [
        r.message for r in caplog.records
    ]
