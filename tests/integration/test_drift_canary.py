"""Drift canary: standalone parser regression check, runnable without a real `claude` binary.

Two parsers protect the runner from upstream format changes:

1. ``claude_task_runner.runner.stream`` — consumes ``claude --print
   --output-format=stream-json`` NDJSON output during task dispatch.
2. ``claude_task_runner.usage.parser`` — consumes ``claude /usage`` TUI
   output during 5h / weekly budget polling.

If Anthropic changes either format, the runner silently drifts: the
dispatcher might miss the session_id (no resume), or the supervisor's
usage poll might raise :class:`UsageFormatDrift` and pause the queue.
This canary catches both regressions WITHOUT requiring a real ``claude``
binary, so it's nightly-runnable in CI and PR runs.

Test coverage:

* **stream-json drift** — invoke the bundled fake ``claude`` shim
  (``tests/fixtures/claude_shim/claude``) and assert ``parse_lines``
  yields the expected event sequence and cumulative usage. The CI step
  pre-pends the shim's directory to ``PATH`` so the test can also be run
  by typing ``claude …`` directly; here we invoke the shim by absolute
  path so the test is independent of ``PATH`` setup.

* **/usage drift** — replay every ``tests/fixtures/usage/*.cap`` PTY
  capture through :func:`usage.parser.parse` and assert percentages and
  raw-reset strings match the paired ``.expected.json``. This duplicates
  ``tests/unit/test_parser.py::test_fixture_replay`` intentionally: if
  the unit-level parser test is moved or refactored away, the canary
  still guarantees the format contract is exercised end-to-end.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.runner.stream import (
    AssistantMessageEvent,
    ResultEvent,
    StreamSummary,
    SystemInitEvent,
    parse_lines,
)
from claude_task_runner.usage.parser import parse as parse_usage

REPO_ROOT = Path(__file__).parent.parent.parent
SHIM_PATH = REPO_ROOT / "tests" / "fixtures" / "claude_shim" / "claude"
USAGE_FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "usage"


def _run_shim(env: dict[str, str]) -> list[str]:
    """Invoke the fake claude shim with ``env`` and return its stdout lines.

    ``check=False``: the shim exits non-zero on the error path (mirroring
    real claude). The runner cares about the parsed result event, not the
    exit code.
    """
    shim_env = {**os.environ, **env}
    result = subprocess.run(
        [sys.executable, str(SHIM_PATH)],
        env=shim_env,
        capture_output=True,
        check=False,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def test_stream_json_canary_default_invocation() -> None:
    """Default-env shim emits the canonical event sequence the parser expects.

    Defaults (see ``tests/fixtures/claude_shim/claude``):
    ``SHIM_SESSION_ID=test-session``, ``SHIM_INPUT_TOKENS=10``,
    ``SHIM_OUTPUT_TOKENS=5``, ``SHIM_NUM_ASSISTANT_MSGS=2``,
    ``SHIM_STOP_REASON=end_turn``, ``SHIM_IS_ERROR=false``.

    A regression that changed the stream-json shape (e.g. renamed
    ``session_id`` → ``id``, dropped ``usage`` from ``assistant`` events,
    or moved ``stop_reason`` to a nested object) would cause one of the
    asserts below to fail.
    """
    lines = _run_shim(env={})

    summary = StreamSummary()
    events = list(parse_lines(lines, summary=summary))

    init_events = [e for e in events if isinstance(e, SystemInitEvent)]
    assistant_events = [e for e in events if isinstance(e, AssistantMessageEvent)]
    result_events = [e for e in events if isinstance(e, ResultEvent)]

    assert len(init_events) == 1
    assert init_events[0].session_id == "test-session"

    assert len(assistant_events) == 2
    for evt in assistant_events:
        assert evt.usage_delta.input_tokens > 0
        assert evt.usage_delta.output_tokens > 0

    assert len(result_events) == 1
    final = result_events[0]
    assert final.subtype == "success"
    assert final.stop_reason == "end_turn"
    assert final.is_error is False
    assert final.final_usage.input_tokens == 10
    assert final.final_usage.output_tokens == 5

    assert summary.session_id == "test-session"
    assert summary.final_result is not None
    assert summary.skipped_lines == 0


def test_stream_json_canary_error_path() -> None:
    """Error-mode shim emits a result event the parser correctly classifies.

    Protects against drift that would let an actual claude error read as
    success (e.g. if ``is_error`` moved to a different key).
    """
    lines = _run_shim(
        env={
            "SHIM_SESSION_ID": "err-session",
            "SHIM_IS_ERROR": "true",
            "SHIM_STOP_REASON": "max_tokens",
        }
    )

    summary = StreamSummary()
    events = list(parse_lines(lines, summary=summary))
    result_events = [e for e in events if isinstance(e, ResultEvent)]

    assert len(result_events) == 1
    final = result_events[0]
    assert final.subtype == "error"
    assert final.is_error is True
    assert final.stop_reason == "max_tokens"
    assert summary.session_id == "err-session"


@pytest.mark.parametrize(
    "cap_path",
    sorted(USAGE_FIXTURES_DIR.glob("*.cap")),
    ids=lambda p: p.stem,
)
def test_usage_parser_canary_fixture_replay(cap_path: Path) -> None:
    """Every bundled ``.cap`` PTY capture parses to its expected JSON shape.

    Mirrors ``tests/unit/test_parser.py::test_fixture_replay`` so the
    canary independently asserts the /usage format contract is still met,
    even if the unit suite is refactored. Failure here means either a
    parser regression or upstream Anthropic format change.
    """
    expected_path = cap_path.with_suffix(".expected.json")
    assert expected_path.exists(), f"missing expected JSON beside {cap_path.name}"

    expected = json.loads(expected_path.read_text())
    captured_at = datetime(2026, 5, 3, 18, 0, 0, tzinfo=UTC)
    clock = FakeClock(captured_at)

    reading = parse_usage(cap_path.read_bytes(), captured_at, clock)

    assert reading.five_hour.utilization_pct == expected["five_hour"]["utilization"]
    assert reading.five_hour.resets_at_raw == expected["five_hour"]["resets_at_raw"]
    assert reading.seven_day.utilization_pct == expected["seven_day"]["utilization"]
    assert reading.seven_day.resets_at_raw == expected["seven_day"]["resets_at_raw"]
