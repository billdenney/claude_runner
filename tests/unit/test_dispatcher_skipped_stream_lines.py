"""A run's skipped stream-json lines are recorded and logged, not dropped.

``runner.stream.parse_lines`` skips malformed lines and events of a type it
does not recognize, so one bad line cannot abort a run. ``_build_run_record``
records how many on the :class:`RunRecord` and logs one warning naming the
unknown types, since a non-zero count can mean the stream format drifted.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_task_runner.queue.schema import RunRecord
from claude_task_runner.runner.dispatcher import _build_run_record, _reparse_stdout_file
from claude_task_runner.runner.session import ResumeStrategy, SpawnPlan

DISPATCHER_LOGGER = "claude_task_runner.runner.dispatcher"
RESULT = (
    '{"type": "result", "subtype": "success", "stop_reason": "end_turn", '
    '"is_error": false, "total_cost_usd": 0.5, "duration_ms": 1000}'
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


def test_skipped_lines_are_recorded_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    log = tmp_path / "attempt-2.stream.jsonl"
    lines = [
        '{"type": "system", "subtype": "init", "session_id": "s1"}',
        "{not json",
        '{"type": "rate_limit_event"}',
        '{"type": "rate_limit_event"}',
        '{"type": "mystery"}',
        RESULT,
    ]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=DISPATCHER_LOGGER):
        record = _record_from_log(log)
    assert record.skipped_stream_lines == 4
    assert record.stop_reason == "end_turn"
    assert [r.getMessage() for r in caplog.records] == [
        "task t1 attempt 2: skipped 4 stream-json line(s): 1 malformed, "
        "unknown event types {'mystery': 1, 'rate_limit_event': 2}"
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
