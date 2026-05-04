"""Tests for runner.stream — claude stream-json parser."""

from __future__ import annotations

from claude_task_runner.runner.stream import (
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

    def test_assistant_text_excerpt(self) -> None:
        events = list(parse_lines([SAMPLE_ASSISTANT]))
        assert isinstance(events[0], AssistantMessageEvent)
        assert events[0].text_excerpt == "Reading the file"

    def test_assistant_text_truncated(self) -> None:
        long_text = "x" * 500
        line = (
            f'{{"type": "assistant", "message": {{"content": '
            f'[{{"type": "text", "text": "{long_text}"}}], "usage": {{}}}}}}'
        )
        events = list(parse_lines([line]))
        assert isinstance(events[0], AssistantMessageEvent)
        assert len(events[0].text_excerpt) == 200

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

    def test_system_with_unknown_subtype(self) -> None:
        line = '{"type": "system", "subtype": "unknown_thing"}'
        summary = StreamSummary()
        events = list(parse_lines([line], summary=summary))
        assert events == []
        # We don't yield, but we count it as an event we saw (not skipped malformed).
        assert summary.event_count == 1
