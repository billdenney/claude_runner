"""Unit tests for the decoupled ``_dispatch_loop`` (ADR-0025).

``_dispatch_loop`` was refactored to consume an injected ``lines``
iterable and an injected ``terminate`` callback rather than reaching into
a ``Popen``. This lets the owned file-backed path (a file tailer + a
Popen-group terminate) and the adopted path (a file tailer + a
killpg-by-pid terminate) share the exact same per-event heartbeat / cap /
silence logic.

These tests drive the loop with a plain list of NDJSON lines and a fake
``terminate`` so the cap/silence enforcement is verified without any
subprocess, file, or thread — proving the loop's behaviour is identical
regardless of where the lines came from.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.schema import TaskCapsSettings
from claude_task_runner.queue.schema import Task
from claude_task_runner.runner.dispatcher import _dispatch_loop


def _caps(*, max_tokens: int = 0, max_duration: float = 0.0, kill: float = 0.0) -> TaskCapsSettings:
    return TaskCapsSettings(
        max_tokens_per_task=max_tokens,
        max_duration_s_per_task=max_duration,
        heartbeat_silence_alert_s=600.0,
        heartbeat_silence_kill_s=kill,
    )


def _task() -> Task:
    return Task(id="t-loop", title="t", prompt="p")


def _init() -> str:
    return json.dumps({"type": "system", "subtype": "init", "session_id": "sess-loop"})


def _assistant(inp: int = 10, out: int = 5) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "x"}],
                "usage": {"input_tokens": inp, "output_tokens": out},
            },
        }
    )


def _result(stop_reason: str = "end_turn") -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "stop_reason": stop_reason,
            "is_error": False,
            "total_cost_usd": 0.01,
            "duration_ms": 5,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    )


def test_clean_stream_summary_no_terminate() -> None:
    """A clean init→assistant→result stream produces the right summary and
    never calls ``terminate``."""
    lines = [_init(), _assistant(), _result("end_turn")]
    clock = FakeClock(datetime(2026, 6, 13, 12, 0, tzinfo=UTC))

    terminated = {"n": 0}

    summary, cap_violation = _dispatch_loop(
        lines=lines,
        terminate=lambda: terminated.__setitem__("n", terminated["n"] + 1),
        settings_caps=_caps(),
        clock=clock,
        task=_task(),
        started_at=clock.now(),
    )

    assert cap_violation is None
    assert terminated["n"] == 0
    assert summary.session_id == "sess-loop"
    assert summary.final_result is not None
    assert summary.final_result.stop_reason == "end_turn"
    assert summary.cumulative_usage.input_tokens == 10
    assert summary.cumulative_usage.output_tokens == 5


def test_token_cap_breach_calls_injected_terminate() -> None:
    """When cumulative tokens exceed the cap, the loop calls the injected
    ``terminate`` exactly once and reports a tokens cap violation — the
    same logic whether the lines came from a pipe or a file tailer."""
    # Each assistant message adds 1_000_000 tokens; cap is 1_500_000, so
    # the second message trips it.
    lines = [_init(), _assistant(1_000_000, 0), _assistant(1_000_000, 0), _result()]
    clock = FakeClock(datetime(2026, 6, 13, 12, 0, tzinfo=UTC))

    terminated = {"n": 0}

    _summary, cap_violation = _dispatch_loop(
        lines=lines,
        terminate=lambda: terminated.__setitem__("n", terminated["n"] + 1),
        settings_caps=_caps(max_tokens=1_500_000),
        clock=clock,
        task=_task(),
        started_at=clock.now(),
    )

    assert terminated["n"] == 1
    assert cap_violation is not None
    assert cap_violation.which == "tokens"
    assert cap_violation.observed == 2_000_000
    assert cap_violation.cap == 1_500_000


def test_duration_cap_breach_calls_injected_terminate() -> None:
    """A duration breach (now - started_at > cap) trips on the first event
    when started_at is far enough in the past."""
    lines = [_init(), _assistant(), _result()]
    started = datetime(2026, 6, 13, 12, 0, tzinfo=UTC)
    # Clock 900s past start; duration cap is 600s.
    clock = FakeClock(started + timedelta(seconds=900))

    terminated = {"n": 0}

    _summary, cap_violation = _dispatch_loop(
        lines=lines,
        terminate=lambda: terminated.__setitem__("n", terminated["n"] + 1),
        settings_caps=_caps(max_duration=600.0),
        clock=clock,
        task=_task(),
        started_at=started,
    )

    assert terminated["n"] == 1
    assert cap_violation is not None
    assert cap_violation.which == "duration"


def test_heartbeat_persist_fn_invoked() -> None:
    """The decoupled loop still drives the rate-limited heartbeat persist
    callback (unchanged behaviour) regardless of the line source."""
    lines = [_init(), _result()]
    clock = FakeClock(datetime(2026, 6, 13, 12, 0, tzinfo=UTC))

    persists: list[datetime] = []

    _dispatch_loop(
        lines=lines,
        terminate=lambda: None,
        settings_caps=_caps(),
        clock=clock,
        task=_task(),
        started_at=clock.now(),
        heartbeat_persist_fn=persists.append,
    )

    # First event always persists (no prior timestamp to rate-limit on).
    assert len(persists) == 1
    assert persists[0] == clock.now()
