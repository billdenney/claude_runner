"""A run's skipped stream-json lines and unusable values are recorded and logged.

``runner.stream.parse_lines`` skips malformed lines, events of a known type
that lack what the runner needs, and events of a type it does not recognize,
so one bad line cannot abort a run. ``_build_run_record`` records how many on
the :class:`RunRecord` and logs one warning naming the types, since a non-zero
count can mean the stream format drifted. A value the parser cannot use, such
as a ``NaN`` cost, gets a warning of its own, and the run still records.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_task_runner.queue.schema import RunRecord, TokenUsage
from claude_task_runner.runner.dispatcher import _build_run_record, _reparse_stdout_file
from claude_task_runner.runner.session import ResumeStrategy, SpawnPlan

DISPATCHER_LOGGER = "claude_task_runner.runner.dispatcher"
RESULT = (
    '{"type": "result", "subtype": "success", "stop_reason": "end_turn", '
    '"is_error": false, "total_cost_usd": 0.5, "duration_ms": 1000}'
)
# Shapes claude 2.1.281 emits for the two event types the parser skips quietly.
TOOL_PROGRESS = (
    '{"type": "tool_progress", "tool_use_id": "toolu_1", "tool_name": "Bash", '
    '"parent_tool_use_id": null, "elapsed_time_seconds": 30, "heartbeat": true, '
    '"session_id": "s1", "uuid": "u1"}'
)
RATE_LIMIT_EVENT = (
    '{"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", '
    '"rateLimitType": "five_hour", "isUsingOverage": false}, "uuid": "u2", "session_id": "s1"}'
)
UNUSABLE_VALUES_TAIL = (
    "(token counts taken as 0, a cost recorded as unknown, is_error taken from subtype)"
)


def _record_from_log(log: Path) -> RunRecord:
    return _build_run_record(
        task_id="t1",
        attempt=2,
        started_at=datetime(2026, 9, 26, 12, 0, tzinfo=UTC),
        finished_at=datetime(2026, 9, 26, 12, 5, tzinfo=UTC),
        plan=SpawnPlan(strategy=ResumeStrategy.FRESH, session_id=None, prompt="p", extra_args=[]),
        summary=_reparse_stdout_file(log),
        cap_violation=None,
        process_exit_code=0,
        stderr_tail="",
    )


def _write_log(tmp_path: Path, lines: list[str]) -> Path:
    log = tmp_path / "attempt-2.stream.jsonl"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log


def _result_with_cost(literal: str) -> str:
    return (
        '{"type": "result", "subtype": "success", "stop_reason": "end_turn", '
        f'"is_error": false, "total_cost_usd": {literal}}}'
    )


def test_skipped_lines_are_recorded_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    log = _write_log(
        tmp_path,
        [
            '{"type": "system", "subtype": "init", "session_id": "s1"}',
            "{not json",
            '{"type": "brand_new_event"}',
            '{"type": "brand_new_event"}',
            '{"type": "mystery"}',
            RESULT,
        ],
    )
    with caplog.at_level(logging.WARNING, logger=DISPATCHER_LOGGER):
        record = _record_from_log(log)
    assert record.skipped_stream_lines == 4
    assert record.stop_reason == "end_turn"
    assert [r.getMessage() for r in caplog.records] == [
        "task t1 attempt 2: skipped 4 stream-json line(s): 1 malformed, "
        "unknown event types {'brand_new_event': 2, 'mystery': 1}"
    ]


def test_only_malformed_lines_say_no_unknown_types(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    log = tmp_path / "attempt-2.stream.jsonl"
    log.write_text("{half a line\n" + RESULT + "\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=DISPATCHER_LOGGER):
        record = _record_from_log(log)
    assert record.skipped_stream_lines == 1
    assert [r.getMessage() for r in caplog.records] == [
        "task t1 attempt 2: skipped 1 stream-json line(s): 1 malformed, unknown event types none"
    ]


def test_clean_stream_records_zero_and_logs_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    log = tmp_path / "attempt-2.stream.jsonl"
    log.write_text(RESULT + "\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=DISPATCHER_LOGGER):
        record = _record_from_log(log)
    assert record.skipped_stream_lines == 0
    assert caplog.records == []


def test_quietly_skipped_types_are_not_drift(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Every real run carries these; counting them hid real drift."""
    log = _write_log(
        tmp_path,
        [
            '{"type": "system", "subtype": "init", "session_id": "s1"}',
            RATE_LIMIT_EVENT,
            TOOL_PROGRESS,
            TOOL_PROGRESS,
            RESULT,
        ],
    )
    with caplog.at_level(logging.WARNING, logger=DISPATCHER_LOGGER):
        record = _record_from_log(log)
    assert record.skipped_stream_lines == 0
    assert record.stop_reason == "end_turn"
    assert caplog.records == []


def test_unusable_known_events_are_named_by_type(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    log = _write_log(
        tmp_path,
        [
            '{"type": "system", "subtype": "init"}',
            '{"type": "assistant", "message": "not an object"}',
            "{not json",
            '{"type": "mystery"}',
            RESULT,
        ],
    )
    with caplog.at_level(logging.WARNING, logger=DISPATCHER_LOGGER):
        record = _record_from_log(log)
    assert record.skipped_stream_lines == 4
    assert [r.getMessage() for r in caplog.records] == [
        "task t1 attempt 2: skipped 4 stream-json line(s): 1 malformed, "
        "unusable known event types {'assistant': 1, 'system/init': 1}, "
        "unknown event types {'mystery': 1}"
    ]


@pytest.mark.parametrize(
    "literal",
    [
        pytest.param("NaN", id="nan"),
        pytest.param("Infinity", id="inf"),
        pytest.param("-Infinity", id="-inf"),
        pytest.param("1e999", id="float-literal-too-large"),
        pytest.param("1" + "0" * 400, id="int-literal-too-large"),
        pytest.param("-0.5", id="negative"),
        pytest.param('"0.5"', id="string"),
    ],
)
def test_unusable_cost_is_recorded_as_unknown(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, literal: str
) -> None:
    """Before, NaN failed RunRecord validation, so the finished run never
    recorded; Infinity was saved as inf; a negative became 0.0 silently."""
    log = _write_log(tmp_path, [_result_with_cost(literal)])
    with caplog.at_level(logging.WARNING, logger=DISPATCHER_LOGGER):
        record = _record_from_log(log)
    assert record.cost_usd is None
    assert record.stop_reason == "end_turn"
    assert record.error is None
    assert record.skipped_stream_lines == 0
    assert [r.getMessage() for r in caplog.records] == [
        "task t1 attempt 2: unusable stream-json values {'result.total_cost_usd': 1} "
        + UNUSABLE_VALUES_TAIL
    ]


def test_a_usable_cost_is_recorded_as_given(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    log = _write_log(tmp_path, [_result_with_cost("1.25")])
    with caplog.at_level(logging.WARNING, logger=DISPATCHER_LOGGER):
        record = _record_from_log(log)
    assert record.cost_usd == 1.25
    assert caplog.records == []


def test_unusable_token_count_is_taken_as_zero(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Before, a NaN token count raised out of the parser mid-stream, so the
    run never recorded. The result line here has no usage, so the record
    falls back to the running total."""
    log = _write_log(
        tmp_path,
        [
            '{"type": "assistant", "message": {"usage": {"input_tokens": NaN, "output_tokens": 3}}}',
            RESULT,
        ],
    )
    with caplog.at_level(logging.WARNING, logger=DISPATCHER_LOGGER):
        record = _record_from_log(log)
    assert record.usage == TokenUsage(input_tokens=0, output_tokens=3)
    assert record.cost_usd == 0.5
    assert [r.getMessage() for r in caplog.records] == [
        "task t1 attempt 2: unusable stream-json values {'assistant.usage.input_tokens': 1} "
        + UNUSABLE_VALUES_TAIL
    ]
