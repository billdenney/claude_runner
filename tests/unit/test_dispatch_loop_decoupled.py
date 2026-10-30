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
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.schema import TaskCapsSettings
from claude_task_runner.queue.schema import Task
from claude_task_runner.runner.caps import CapViolation
from claude_task_runner.runner.dispatcher import TerminateFailed, _CapKill, _dispatch_loop


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


def _kill_fails() -> None:
    raise TerminateFailed("task pid 4242 survived SIGKILL")


def test_a_cap_kill_that_raises_leaves_its_cap_with_the_caller() -> None:
    """The kill raises out of the loop, and ``cap_kill`` still holds the cap
    it was for, so the caller can record the run as killed by it."""
    clock = FakeClock(datetime(2026, 6, 13, 12, 0, tzinfo=UTC))
    cap_kill = _CapKill()

    with pytest.raises(TerminateFailed):
        _dispatch_loop(
            lines=[_init(), _assistant(1_000_000, 0), _assistant(1_000_000, 0), _result()],
            terminate=_kill_fails,
            settings_caps=_caps(max_tokens=1_500_000),
            clock=clock,
            task=_task(),
            started_at=clock.now(),
            cap_kill=cap_kill,
        )

    assert cap_kill.violation == CapViolation(which="tokens", observed=2_000_000, cap=1_500_000)


_START = datetime(2026, 6, 13, 12, 0, tzinfo=UTC)


def _then(clock: FakeClock, *steps: str | float | None) -> Iterator[str | None]:
    """Each step: a line to yield, ``None`` for a tick, or seconds the clock
    moves on before the next step."""
    for step in steps:
        if isinstance(step, float):
            clock.advance(step)
        else:
            yield step


def _tool_progress() -> str:
    """A ``tool_progress`` heartbeat, which claude writes every 30 s while a
    tool runs. parse_lines yields no event for it."""
    return json.dumps(
        {
            "type": "tool_progress",
            "tool_name": "Bash",
            "elapsed_time_seconds": 30,
            "heartbeat": True,
        }
    )


def _run(
    steps: tuple[str | float | None, ...], caps: TaskCapsSettings
) -> tuple[list[str], CapViolation | None]:
    """Run the loop over ``steps`` from ``_START``: the kills it made and the
    violation it returned."""
    clock = FakeClock(_START)
    terminated: list[str] = []
    _summary, violation = _dispatch_loop(
        lines=_then(clock, *steps),
        terminate=lambda: terminated.append("kill"),
        settings_caps=caps,
        clock=clock,
        task=_task(),
        started_at=_START,
    )
    return terminated, violation


def test_a_tick_after_silence_past_the_kill_stops_the_worker() -> None:
    """With no line for longer than heartbeat_silence_kill_s, the next tick
    kills the worker and records the silence as the duration cap it hit;
    the loop reads no further."""
    terminated, violation = _run((_init(), 901.0, None, _assistant(), _result()), _caps(kill=900.0))

    assert terminated == ["kill"]
    assert violation == CapViolation(which="duration", observed=901.0, cap=900.0)


def test_a_tick_past_the_duration_cap_stops_a_worker_with_no_events() -> None:
    """A worker that never writes a line still meets the duration cap."""
    terminated, violation = _run((14401.0, None), _caps(max_duration=14400.0))

    assert terminated == ["kill"]
    assert violation == CapViolation(which="duration", observed=14401.0, cap=14400.0)


def test_a_tick_within_both_limits_lets_the_loop_go_on() -> None:
    terminated, violation = _run(
        (_init(), 600.0, None, _result()), _caps(max_duration=14400.0, kill=900.0)
    )

    assert (terminated, violation) == ([], None)


def test_a_tool_progress_line_is_no_silence() -> None:
    """claude writes tool_progress while a long tool call runs. It is no
    event, but it is a line, so the silence the kill measures starts again
    from it."""
    terminated, violation = _run(
        (_init(), 600.0, _tool_progress(), 600.0, None, _result()), _caps(kill=900.0)
    )

    assert (terminated, violation) == ([], None)


class _SettableClock:
    """A wall clock a test sets, backwards too, as a system clock can be."""

    def __init__(self, at: datetime) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at

    def monotonic(self) -> float:
        return 0.0


def test_a_clock_stepped_back_trips_nothing() -> None:
    """A wall clock set back before the start, at an event or at a tick, is
    no silence and no duration: the loop neither raises nor kills."""
    clock = _SettableClock(_START)

    def _steps() -> Iterator[str | None]:
        yield _init()
        clock.at = _START - timedelta(seconds=10)
        yield _assistant()
        yield None
        yield _result()

    terminated: list[str] = []
    _summary, violation = _dispatch_loop(
        lines=_steps(),
        terminate=lambda: terminated.append("kill"),
        settings_caps=_caps(max_duration=1000.0, kill=900.0),
        clock=clock,
        task=_task(),
        started_at=_START,
    )

    assert (terminated, violation) == ([], None)


def test_a_silence_kill_that_raises_leaves_its_cap_with_the_caller() -> None:
    """The silence kill records its duration cap before the kill too."""
    clock = FakeClock(_START)
    cap_kill = _CapKill()

    with pytest.raises(TerminateFailed):
        _dispatch_loop(
            lines=_then(clock, _init(), 1000.0, None),
            terminate=_kill_fails,
            settings_caps=_caps(kill=900.0),
            clock=clock,
            task=_task(),
            started_at=_START,
            cap_kill=cap_kill,
        )

    assert cap_kill.violation == CapViolation(which="duration", observed=1000.0, cap=900.0)
