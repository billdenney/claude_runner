"""Tests for the dispatcher's in-loop heartbeat persistence.

The dispatcher's ``_dispatch_loop`` reads stream-json events from the
subprocess and updates a local ``last_heartbeat`` on each event. To
let the supervisor's per-tick silent-orphan reaper see fresh liveness
on healthy long-running tasks, the loop also calls
``heartbeat_persist_fn`` — at most once per
``heartbeat_persist_interval_s`` seconds — so the YAML reflects
current heartbeat freshness rather than the (stale) value from the
prior finalize.

These tests exercise the persistence callback directly via
``_dispatch_loop`` with hand-crafted event streams and a
:class:`FakeClock`, so the rate-limit logic is verified without a
real subprocess.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.schema import TaskCapsSettings
from claude_task_runner.queue.schema import Task
from claude_task_runner.runner.dispatcher import (
    _HEARTBEAT_PERSIST_FAIL_ESCALATE_AFTER,
    _dispatch_loop,
)


class _FakeStdout:
    """Iterable wrapper that exposes a ``.readline``-style sequence of
    JSON stream-json events as plain strings, one per line. The
    dispatcher's ``parse_lines`` consumes the iterable directly."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    def __iter__(self) -> Any:
        return iter(self._lines)


class _FakeProcess:
    """Minimum subprocess.Popen shape the dispatch loop touches.

    The loop reads ``process.stdout`` (iterated by ``parse_lines``)
    and may call ``process.send_signal`` / ``process.wait`` on a cap
    breach. These tests stay inside the alert window so no signaling
    happens; only ``stdout`` is exercised."""

    def __init__(self, lines: list[str]) -> None:
        self.stdout = _FakeStdout(lines)

    def send_signal(self, _sig: int) -> None:  # pragma: no cover (defensive)
        raise AssertionError("test should not signal")

    def wait(self, timeout: float | None = None) -> int:  # pragma: no cover
        return 0

    def kill(self) -> None:  # pragma: no cover
        pass


def _caps(*, persist_s: float = 30.0, alert: float = 600.0) -> TaskCapsSettings:
    return TaskCapsSettings(
        max_tokens_per_task=0,
        max_duration_s_per_task=0,
        heartbeat_silence_alert_s=alert,
        heartbeat_silence_kill_s=0,
        heartbeat_persist_interval_s=persist_s,
    )


def _task() -> Task:
    return Task(id="t-hb", title="hb", prompt="p")


def _system_init_line() -> str:
    """Minimum-valid system_init event for the stream parser."""
    return '{"type":"system","subtype":"init","session_id":"sess-1","tools":[],"mcp_servers":[]}'


def _assistant_message_line() -> str:
    """Minimum-valid assistant message event."""
    return (
        '{"type":"assistant","message":{"id":"m1","content":[],'
        '"usage":{"input_tokens":1,"output_tokens":1}}}'
    )


def _result_line() -> str:
    """Final result event so the loop exits cleanly."""
    return (
        '{"type":"result","subtype":"success","duration_ms":1,'
        '"duration_api_ms":1,"is_error":false,"num_turns":1,'
        '"session_id":"sess-1","total_cost_usd":0.0,'
        '"usage":{"input_tokens":1,"output_tokens":1}}'
    )


def test_persist_called_on_first_event() -> None:
    """The very first event triggers a persist — until then there is no
    prior persist timestamp to rate-limit against."""
    process = _FakeProcess([_system_init_line(), _result_line()])
    clock = FakeClock(datetime(2026, 6, 12, 12, 0, tzinfo=UTC))
    started = clock.now()

    persists: list[datetime] = []

    def persist(when: datetime) -> None:
        persists.append(when)

    _dispatch_loop(
        lines=process.stdout,
        terminate=lambda: None,
        settings_caps=_caps(persist_s=30.0, alert=600.0),
        clock=clock,
        task=_task(),
        started_at=started,
        heartbeat_persist_fn=persist,
    )

    # Both events triggered the rate-limited persist; the first is
    # always-on, the second falls within the 30s window so it does NOT
    # double-fire (clock didn't advance between events in this test).
    assert len(persists) == 1
    assert persists[0] == started


def test_persist_rate_limited_by_interval() -> None:
    """Two events fired in quick succession produce exactly one persist
    because the second falls inside the rate-limit window."""
    lines = [_system_init_line(), _assistant_message_line(), _result_line()]
    process = _FakeProcess(lines)
    clock = FakeClock(datetime(2026, 6, 12, 12, 0, tzinfo=UTC))
    started = clock.now()

    persists: list[datetime] = []

    def persist(when: datetime) -> None:
        persists.append(when)
        # Don't advance the clock — simulate events arriving rapid-fire.

    _dispatch_loop(
        lines=process.stdout,
        terminate=lambda: None,
        settings_caps=_caps(persist_s=30.0),
        clock=clock,
        task=_task(),
        started_at=started,
        heartbeat_persist_fn=persist,
    )

    # Only the first event persisted; the subsequent events landed at
    # the same instant and were rate-limited away.
    assert len(persists) == 1


def test_persist_fires_again_after_interval_elapses() -> None:
    """When wall-clock advances past the rate-limit interval, a
    subsequent event re-persists. Verified with a clock that
    advances by 31s between events (1s past the 30s interval)."""
    lines = [_system_init_line(), _assistant_message_line(), _result_line()]
    process = _FakeProcess(lines)
    clock = FakeClock(datetime(2026, 6, 12, 12, 0, tzinfo=UTC))
    started = clock.now()

    persists: list[datetime] = []
    event_counter = {"n": 0}

    def persist(when: datetime) -> None:
        persists.append(when)

    # Wrap parse_lines so the clock advances between events.
    # We do this by monkey-patching clock.now via the FakeClock's
    # advance() — but we need to hook into the iteration. Simpler:
    # advance the clock inside the persist callback's iteration
    # boundary by tracking event count. Each event picks up the
    # advanced clock when _dispatch_loop calls clock.now() for the
    # next event's last_heartbeat.
    original_now = clock.now

    def advancing_now() -> datetime:
        n = event_counter["n"]
        event_counter["n"] = n + 1
        # Each event-step costs 31 seconds of wall-clock relative to start.
        return started + timedelta(seconds=31 * n)

    clock.now = advancing_now  # type: ignore[method-assign]

    try:
        _dispatch_loop(
            lines=process.stdout,
            terminate=lambda: None,
            settings_caps=_caps(persist_s=30.0),
            clock=clock,
            task=_task(),
            started_at=started,
            heartbeat_persist_fn=persist,
        )
    finally:
        clock.now = original_now  # type: ignore[method-assign]

    # With three events and a 30s interval, every event qualifies (each
    # is +31s past the prior persist). All three events trigger a persist.
    assert len(persists) == 3


def test_persist_fn_omitted_does_not_break() -> None:
    """When the caller does NOT supply a persist callback (e.g. unit
    tests that don't care about YAML I/O), the dispatch loop runs
    normally without persistence side effects."""
    lines = [_system_init_line(), _result_line()]
    process = _FakeProcess(lines)
    clock = FakeClock(datetime(2026, 6, 12, 12, 0, tzinfo=UTC))
    started = clock.now()

    # No exception, no persists requested.
    summary, cap_violation = _dispatch_loop(
        lines=process.stdout,
        terminate=lambda: None,
        settings_caps=_caps(),
        clock=clock,
        task=_task(),
        started_at=started,
        heartbeat_persist_fn=None,
    )
    assert cap_violation is None
    assert summary.session_id == "sess-1"


def test_persist_callback_failure_is_swallowed() -> None:
    """A persist callback that raises must not abort the dispatch loop
    — the YAML write is a best-effort observability nicety, not a
    correctness gate. The loop logs and continues."""
    lines = [_system_init_line(), _result_line()]
    process = _FakeProcess(lines)
    clock = FakeClock(datetime(2026, 6, 12, 12, 0, tzinfo=UTC))
    started = clock.now()

    def boom(_when: datetime) -> None:
        raise OSError("disk full")

    summary, _ = _dispatch_loop(
        lines=process.stdout,
        terminate=lambda: None,
        settings_caps=_caps(),
        clock=clock,
        task=_task(),
        started_at=started,
        heartbeat_persist_fn=boom,
    )

    # Loop completed; we got the result event.
    assert summary.session_id == "sess-1"


def test_repeated_persist_failures_escalate_to_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Audit code-smell (dispatcher.py:360): a sustained run of
    heartbeat-persist failures within one dispatch loop must escalate
    from WARNING to ERROR after ``_HEARTBEAT_PERSIST_FAIL_ESCALATE_AFTER``
    so a real disk problem (full / read-only) isn't lost in a stream of
    warnings. A single failure stays at WARNING.

    The stream carries enough events (and the clock advances past the
    persist interval each event) that every event triggers a persist,
    every persist raises, and the failure counter crosses the
    escalation threshold."""
    n_events = _HEARTBEAT_PERSIST_FAIL_ESCALATE_AFTER + 2
    # system_init + (n_events - 2) assistant messages + result = n_events
    # parsed events, each forcing a persist attempt.
    lines = [
        _system_init_line(),
        *[_assistant_message_line() for _ in range(n_events - 2)],
        _result_line(),
    ]
    process = _FakeProcess(lines)
    clock = FakeClock(datetime(2026, 6, 12, 12, 0, tzinfo=UTC))
    started = clock.now()

    # Advance the clock 31s per event so each event clears the 30s
    # rate-limit window and forces a fresh persist attempt.
    event_counter = {"n": 0}

    def advancing_now() -> datetime:
        n = event_counter["n"]
        event_counter["n"] = n + 1
        return started + timedelta(seconds=31 * n)

    original_now = clock.now
    clock.now = advancing_now  # type: ignore[method-assign]

    fail_count = {"n": 0}

    def boom(_when: datetime) -> None:
        fail_count["n"] += 1
        raise OSError("disk full")

    try:
        with caplog.at_level(logging.WARNING, logger="claude_task_runner.runner.dispatcher"):
            _dispatch_loop(
                lines=process.stdout,
                terminate=lambda: None,
                settings_caps=_caps(persist_s=30.0, alert=10_000.0),
                clock=clock,
                task=_task(),
                started_at=started,
                heartbeat_persist_fn=boom,
            )
    finally:
        clock.now = original_now  # type: ignore[method-assign]

    # Every event forced a persist attempt, and all of them raised.
    assert fail_count["n"] == n_events

    dispatcher_records = [
        r for r in caplog.records if r.name == "claude_task_runner.runner.dispatcher"
    ]
    warnings = [r for r in dispatcher_records if r.levelno == logging.WARNING]
    errors = [r for r in dispatcher_records if r.levelno == logging.ERROR]

    # First (threshold - 1) failures are WARNING; the rest are ERROR.
    assert len(warnings) == _HEARTBEAT_PERSIST_FAIL_ESCALATE_AFTER - 1
    assert len(errors) == n_events - (_HEARTBEAT_PERSIST_FAIL_ESCALATE_AFTER - 1)
    # The escalated records carry the per-dispatch failure count marker.
    assert all("failure" in r.getMessage() for r in errors)
