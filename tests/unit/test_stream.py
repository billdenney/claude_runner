"""Tests for runner.stream — claude stream-json parser."""

from __future__ import annotations

import pytest

from claude_task_runner.queue.schema import TokenUsage
from claude_task_runner.runner.stream import (
    _MAX_TOKEN_COUNT,
    _QUIETLY_SKIPPED_EVENT_TYPES,
    _TOKEN_FIELDS,
    AssistantMessageEvent,
    ResultEvent,
    StreamSummary,
    SystemInitEvent,
    UserMessageEvent,
    parse_line,
    parse_lines,
)

SAMPLE_INIT = '{"type": "system", "subtype": "init", "session_id": "abc-123"}'
SAMPLE_ASSISTANT = (
    '{"type": "assistant", "message": {"content": '
    '[{"type": "text", "text": "Reading the file"}], '
    '"usage": {"input_tokens": 100, "output_tokens": 200, '
    '"cache_read_input_tokens": 5000}}}'
)
SAMPLE_USER = '{"type": "user", "message": {"content": [{"type": "tool_result"}]}}'
SAMPLE_RESULT = (
    '{"type": "result", "subtype": "success", "stop_reason": "end_turn", '
    '"is_error": false, "total_cost_usd": 1.234, "duration_ms": 5000, '
    '"usage": {"input_tokens": 500, "output_tokens": 1500, '
    '"cache_read_input_tokens": 10000, "cache_creation_input_tokens": 2000}}'
)


class TestParseLine:
    def test_valid_object(self) -> None:
        out = parse_line('{"a": 1}')
        assert out == {"a": 1}

    def test_blank_returns_none(self) -> None:
        assert parse_line("") is None
        assert parse_line("   \n") is None

    def test_malformed_returns_none(self) -> None:
        assert parse_line("{not json") is None

    def test_top_level_array_returns_none(self) -> None:
        assert parse_line("[1, 2, 3]") is None

    def test_bytes_input(self) -> None:
        assert parse_line(b'{"a": 1}') == {"a": 1}

    def test_invalid_utf8_returns_none(self) -> None:
        assert parse_line(b"\xff\xfe\xfd") is None


class TestParseLines:
    def test_full_session(self) -> None:
        lines = [SAMPLE_INIT, SAMPLE_ASSISTANT, SAMPLE_RESULT]
        summary = StreamSummary()
        events = list(parse_lines(lines, summary=summary))
        assert len(events) == 3
        assert isinstance(events[0], SystemInitEvent)
        assert isinstance(events[1], AssistantMessageEvent)
        assert isinstance(events[2], ResultEvent)
        assert summary.session_id == "abc-123"
        assert summary.cumulative_usage.input_tokens == 100
        assert summary.cumulative_usage.output_tokens == 200
        assert summary.cumulative_usage.cache_read_tokens == 5000
        assert summary.final_result is not None
        assert summary.final_result.stop_reason == "end_turn"
        assert summary.final_result.cost_usd == 1.234

    def test_user_message_yielded(self) -> None:
        events = list(parse_lines([SAMPLE_USER]))
        assert len(events) == 1
        assert isinstance(events[0], UserMessageEvent)

    def test_cumulative_usage_sums(self) -> None:
        line1 = (
            '{"type": "assistant", "message": {"usage": {"input_tokens": 10, "output_tokens": 20}}}'
        )
        line2 = (
            '{"type": "assistant", "message": {"usage": {"input_tokens": 5, "output_tokens": 15}}}'
        )
        summary = StreamSummary()
        list(parse_lines([line1, line2], summary=summary))
        assert summary.cumulative_usage.input_tokens == 15
        assert summary.cumulative_usage.output_tokens == 35

    def test_malformed_lines_skipped(self) -> None:
        lines = [SAMPLE_INIT, "{not json", SAMPLE_RESULT]
        summary = StreamSummary()
        events = list(parse_lines(lines, summary=summary))
        assert len(events) == 2
        assert summary.skipped_lines == 1

    def test_empty_lines_skipped_silently(self) -> None:
        lines = ["", SAMPLE_INIT, "  \n", SAMPLE_RESULT]
        summary = StreamSummary()
        events = list(parse_lines(lines, summary=summary))
        assert len(events) == 2
        # Empty lines are skipped pre-classification, not counted as malformed
        assert summary.skipped_lines == 0

    def test_result_with_error(self) -> None:
        line = (
            '{"type": "result", "subtype": "error", "is_error": true, '
            '"stop_reason": "rate_limit", "total_cost_usd": 0, "duration_ms": 100}'
        )
        events = list(parse_lines([line]))
        assert isinstance(events[0], ResultEvent)
        assert events[0].is_error is True
        assert events[0].stop_reason == "rate_limit"

    def test_unknown_event_type_skipped(self) -> None:
        line = '{"type": "totally_new_event"}'
        summary = StreamSummary()
        events = list(parse_lines([line], summary=summary))
        assert events == []
        assert summary.skipped_lines == 1
        assert summary.unknown_event_types == {"totally_new_event": 1}

    def test_skipped_lines_split_into_malformed_and_unknown_types(self) -> None:
        lines = [
            '{"type": "a"}',
            "{broken",
            '{"type": "b"}',
            '{"type": "a"}',
            '{"no_type": 1}',
            '{"type": 7}',
            SAMPLE_RESULT,
        ]
        summary = StreamSummary()
        events = list(parse_lines(lines, summary=summary))
        assert [type(e) for e in events] == [ResultEvent]
        assert summary.skipped_lines == 6
        assert summary.unknown_event_types == {"a": 2, "b": 1, "<NoneType>": 1, "<int>": 1}

    def test_system_with_unknown_subtype(self) -> None:
        line = '{"type": "system", "subtype": "unknown_thing"}'
        summary = StreamSummary()
        events = list(parse_lines([line], summary=summary))
        assert events == []
        # We don't yield it, but "system" is a known type, so it is not drift.
        assert summary.skipped_lines == 0
        assert summary.unknown_event_types == {}


# Shapes claude 2.1.281 emits for the event types the parser skips quietly.
QUIET_SAMPLES = {
    "tool_progress": (
        '{"type": "tool_progress", "tool_use_id": "toolu_1", "tool_name": "Bash", '
        '"parent_tool_use_id": null, "elapsed_time_seconds": 30, "heartbeat": true, '
        '"session_id": "s1", "uuid": "u1"}'
    ),
    "rate_limit_event": (
        '{"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", '
        '"rateLimitType": "five_hour", "isUsingOverage": false}, "uuid": "u2", '
        '"session_id": "s1"}'
    ),
}


def _parse_one(line: str) -> tuple[list[object], StreamSummary]:
    summary = StreamSummary()
    events: list[object] = list(parse_lines([line], summary=summary))
    return events, summary


def _parse_result(fields: str) -> tuple[ResultEvent, StreamSummary]:
    """Parse a result line with ``fields`` added; it must yield one ResultEvent."""
    events, summary = _parse_one('{"type": "result"' + (", " + fields if fields else "") + "}")
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, ResultEvent)
    return event, summary


def _parse_usage(usage: str) -> StreamSummary:
    """Parse an assistant line whose usage is ``usage``; it must yield one event."""
    events, summary = _parse_one(f'{{"type": "assistant", "message": {{"usage": {usage}}}}}')
    assert len(events) == 1
    return summary


class TestQuietlySkippedEventTypes:
    """Informational types every real run carries: not drift, never yielded."""

    def test_the_set_is_exactly_these(self) -> None:
        assert set(QUIET_SAMPLES) == _QUIETLY_SKIPPED_EVENT_TYPES

    @pytest.mark.parametrize("event_type", sorted(_QUIETLY_SKIPPED_EVENT_TYPES))
    def test_skipped_without_counting(self, event_type: str) -> None:
        events, summary = _parse_one(QUIET_SAMPLES[event_type])
        assert events == []
        assert summary.skipped_lines == 0
        assert summary.unknown_event_types == {}
        assert summary.unusable_event_types == {}
        assert summary.unusable_values == {}

    def test_unhashable_type_is_counted_not_raised(self) -> None:
        """The set lookup must not see a list or object ``type``."""
        summary = StreamSummary()
        lines = ['{"type": [1]}', '{"type": {"a": 1}}']
        assert list(parse_lines(lines, summary=summary)) == []
        assert summary.skipped_lines == 2
        assert summary.unknown_event_types == {"<list>": 1, "<dict>": 1}


class TestUnusableKnownEvents:
    """A known type missing what the runner needs is skipped and counted by type."""

    @pytest.mark.parametrize(
        "line",
        [
            '{"type": "system", "subtype": "init"}',
            '{"type": "system", "subtype": "init", "session_id": null}',
            '{"type": "system", "subtype": "init", "session_id": 7}',
        ],
    )
    def test_init_without_a_string_session_id(self, line: str) -> None:
        events, summary = _parse_one(line)
        assert events == []
        assert summary.session_id is None
        assert summary.skipped_lines == 1
        assert summary.unusable_event_types == {"system/init": 1}
        assert summary.unknown_event_types == {}

    @pytest.mark.parametrize(
        "line",
        [
            '{"type": "assistant"}',
            '{"type": "assistant", "message": "hi"}',
            '{"type": "assistant", "message": [1]}',
        ],
    )
    def test_assistant_without_an_object_message(self, line: str) -> None:
        events, summary = _parse_one(line)
        assert events == []
        assert summary.cumulative_usage == TokenUsage()
        assert summary.skipped_lines == 1
        assert summary.unusable_event_types == {"assistant": 1}
        assert summary.unknown_event_types == {}


class TestResultFallbacks:
    @pytest.mark.parametrize(
        ("fields", "is_error", "unusable"),
        [
            pytest.param('"subtype": "error_max_turns"', True, {}, id="error_max_turns"),
            pytest.param('"subtype": "error_during_execution"', True, {}, id="during_execution"),
            pytest.param('"subtype": "error_max_budget_usd"', True, {}, id="max_budget"),
            pytest.param('"subtype": "error"', True, {}, id="error"),
            pytest.param('"subtype": "success"', False, {}, id="success"),
            pytest.param("", False, {}, id="no-subtype"),
            pytest.param('"subtype": "error_max_turns", "is_error": null', True, {}, id="null"),
            pytest.param(
                '"subtype": "success", "is_error": "false"',
                False,
                {"result.is_error": 1},
                id="string-success",
            ),
            pytest.param(
                '"subtype": "error_max_turns", "is_error": 0',
                True,
                {"result.is_error": 1},
                id="number-error",
            ),
            pytest.param('"subtype": "success", "is_error": true', True, {}, id="bool-wins-true"),
            pytest.param(
                '"subtype": "error_max_turns", "is_error": false', False, {}, id="bool-wins-false"
            ),
        ],
    )
    def test_is_error(self, fields: str, is_error: bool, unusable: dict[str, int]) -> None:
        """Only a JSON boolean is used as is; anything else falls back to
        whether ``subtype`` starts with ``error``. Before, the fallback was
        ``subtype == "error"``, and a string ``"false"`` read as an error."""
        event, summary = _parse_result(fields)
        assert event.is_error is is_error
        assert summary.unusable_values == unusable

    def test_stop_reason_falls_back_to_subtype(self) -> None:
        """claude 2.1.281's own error result: ``stop_reason`` is null."""
        event, summary = _parse_result(
            '"subtype": "error_during_execution", "duration_ms": 0, "duration_api_ms": 0, '
            '"is_error": true, "num_turns": 0, "stop_reason": null, "session_id": "s1", '
            '"total_cost_usd": 0, "usage": {"input_tokens": 0, "output_tokens": 0}'
        )
        assert event == ResultEvent(
            stop_reason="error_during_execution",
            is_error=True,
            cost_usd=0.0,
            final_usage=TokenUsage(),
        )
        assert summary.unusable_values == {}

    def test_stop_reason_without_subtype_is_unknown(self) -> None:
        event, _ = _parse_result("")
        assert event.stop_reason == "unknown"


class TestCost:
    @pytest.mark.parametrize(
        "literal",
        [
            pytest.param("NaN", id="nan"),
            pytest.param("Infinity", id="inf"),
            pytest.param("-Infinity", id="-inf"),
            pytest.param("1e999", id="float-literal-too-large"),
            pytest.param("1" + "0" * 400, id="int-literal-too-large"),
            pytest.param("-0.5", id="negative"),
            pytest.param('"1.5"', id="string"),
            pytest.param("true", id="bool"),
            pytest.param("[1]", id="list"),
            pytest.param('{"usd": 1}', id="object"),
        ],
    )
    @pytest.mark.parametrize("key", ["total_cost_usd", "cost_usd"])
    def test_unusable_cost_is_unknown_and_counted(self, key: str, literal: str) -> None:
        event, summary = _parse_result(f'"{key}": {literal}')
        assert event.cost_usd is None
        assert summary.unusable_values == {f"result.{key}": 1}

    @pytest.mark.parametrize(
        ("fields", "cost"),
        [
            pytest.param('"total_cost_usd": 1.234', 1.234, id="float"),
            pytest.param('"total_cost_usd": 5', 5.0, id="int"),
            pytest.param('"total_cost_usd": 0', 0.0, id="zero"),
            pytest.param("", 0.0, id="missing"),
            pytest.param('"total_cost_usd": null', 0.0, id="null"),
            pytest.param('"cost_usd": 0.7', 0.7, id="older-key"),
            pytest.param('"total_cost_usd": null, "cost_usd": 0.7', 0.7, id="null-then-older"),
            pytest.param('"total_cost_usd": 0, "cost_usd": 5', 0.0, id="present-zero-wins"),
        ],
    )
    def test_usable_cost(self, fields: str, cost: float) -> None:
        event, summary = _parse_result(fields)
        assert event.cost_usd == cost
        assert summary.unusable_values == {}


UNUSABLE_COUNTS = [
    pytest.param("NaN", id="nan"),
    pytest.param("Infinity", id="inf"),
    pytest.param("-Infinity", id="-inf"),
    pytest.param("1e999", id="float-literal-too-large"),
    pytest.param(str(_MAX_TOKEN_COUNT + 1), id="above-2**53"),
    pytest.param("-1", id="negative"),
    pytest.param("2.5", id="fraction"),
    pytest.param('"12"', id="string"),
    pytest.param("true", id="bool"),
    pytest.param("[1]", id="list"),
]
EVERY_USAGE_KEY = [pytest.param(name, key, id=key) for name, keys in _TOKEN_FIELDS for key in keys]


class TestTokenCounts:
    def test_every_token_usage_field_is_read(self) -> None:
        """A field added to TokenUsage fails here until the parser reads it."""
        assert [name for name, _ in _TOKEN_FIELDS] == list(TokenUsage.model_fields)

    @pytest.mark.parametrize("literal", UNUSABLE_COUNTS)
    @pytest.mark.parametrize(("name", "key"), EVERY_USAGE_KEY)
    def test_unusable_count_is_zero_and_counted(self, name: str, key: str, literal: str) -> None:
        """Before, each of these raised out of the parser mid-stream. A
        second, usable count in the same usage is still read."""
        other = "input_tokens" if name == "output_tokens" else "output_tokens"
        summary = _parse_usage(f'{{"{key}": {literal}, "{other}": 9}}')
        assert summary.cumulative_usage == TokenUsage(**{other: 9})
        assert summary.unusable_values == {f"assistant.usage.{key}": 1}

    def test_result_usage_is_named_by_its_event(self) -> None:
        event, summary = _parse_result('"usage": {"input_tokens": NaN, "output_tokens": 4}')
        assert event.final_usage == TokenUsage(output_tokens=4)
        assert summary.unusable_values == {"result.usage.input_tokens": 1}

    @pytest.mark.parametrize(
        ("literal", "count"),
        [
            pytest.param("0", 0, id="zero"),
            pytest.param("7", 7, id="int"),
            pytest.param("7.0", 7, id="whole-float"),
            pytest.param(str(_MAX_TOKEN_COUNT), _MAX_TOKEN_COUNT, id="2**53"),
        ],
    )
    def test_usable_count(self, literal: str, count: int) -> None:
        summary = _parse_usage(f'{{"input_tokens": {literal}}}')
        assert summary.cumulative_usage == TokenUsage(input_tokens=count)
        assert summary.unusable_values == {}

    @pytest.mark.parametrize(
        ("usage", "expected"),
        [
            pytest.param('{"cache_read_input_tokens": 3, "cache_read_tokens": 5}', 3, id="first"),
            pytest.param('{"cache_read_input_tokens": 0, "cache_read_tokens": 5}', 0, id="zero"),
            pytest.param('{"cache_read_input_tokens": null, "cache_read_tokens": 5}', 5, id="null"),
            pytest.param('{"cache_read_tokens": 5}', 5, id="older-key-only"),
        ],
    )
    def test_first_present_key_wins(self, usage: str, expected: int) -> None:
        summary = _parse_usage(usage)
        assert summary.cumulative_usage == TokenUsage(cache_read_tokens=expected)
        assert summary.unusable_values == {}

    @pytest.mark.parametrize("usage", ["5", "[1]", '"x"'])
    def test_usage_that_is_not_an_object(self, usage: str) -> None:
        summary = _parse_usage(usage)
        assert summary.cumulative_usage == TokenUsage()
        assert summary.unusable_values == {"assistant.usage": 1}

    def test_null_usage_is_not_counted(self) -> None:
        summary = _parse_usage("null")
        assert summary.cumulative_usage == TokenUsage()
        assert summary.unusable_values == {}
