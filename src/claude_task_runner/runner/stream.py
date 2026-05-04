"""Parse Claude Code's ``--output-format=stream-json`` NDJSON stream.

The claude binary emits one JSON object per line when invoked with
``claude --print --output-format=stream-json --verbose``. The runner
consumes those lines as they're produced to:

1. Capture the ``session_id`` from the first ``system/init`` event so
   that subsequent attempts can ``--resume`` mid-task across rate-limit
   windows (ADR-0005).
2. Update :class:`TokenUsage` aggregates for cap and EMA tracking.
3. Emit timestamps so :mod:`runner.heartbeat` can flag silence.
4. Surface the final ``result`` event's ``stop_reason`` and accumulated
   ``cost_usd`` for the :class:`RunRecord`.

This module is the **pure parser**. The dispatcher reads bytes from the
subprocess and feeds them to :func:`parse_lines`. We DON'T spawn
subprocesses here.

Robust parsing: malformed JSON lines are skipped with a
:class:`StreamWarning` (not raised) so a single corrupt line doesn't
abort the whole run. If the entire stream produces zero events, the
caller should treat that as a process error, not a parse error.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from claude_task_runner.queue.schema import TokenUsage

logger = logging.getLogger(__name__)


class StreamWarning(UserWarning):
    """Issued for skipped (malformed/unrecognized) NDJSON lines."""


@dataclass(frozen=True)
class SystemInitEvent:
    """The first line — carries the new session id."""

    session_id: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class AssistantMessageEvent:
    """A model-emitted message; usage deltas live inside the message body."""

    text_excerpt: str
    """First ~200 chars of the assistant's text content, for log readability."""
    usage_delta: TokenUsage
    """Per-message usage delta. Cumulative is the sum across events."""
    raw: dict[str, Any]


@dataclass(frozen=True)
class UserMessageEvent:
    """A user-side message (tool result, follow-up prompt)."""

    raw: dict[str, Any]


@dataclass(frozen=True)
class ResultEvent:
    """The final line, summarizing the run.

    All fields except ``raw`` are derived from common stream-json shapes
    but we tolerate missing keys so a slightly-different upstream
    version doesn't crash the runner.
    """

    subtype: str  # "success", "error", etc.
    stop_reason: str
    is_error: bool
    cost_usd: float
    duration_ms: int
    final_usage: TokenUsage
    raw: dict[str, Any]


@dataclass
class StreamSummary:
    """Running totals updated as :func:`parse_lines` yields events."""

    session_id: str | None = None
    cumulative_usage: TokenUsage = field(default_factory=TokenUsage)
    final_result: ResultEvent | None = None
    event_count: int = 0
    skipped_lines: int = 0


def _coerce_token_usage(d: Any | None) -> TokenUsage:
    """Best-effort conversion of an arbitrary 'usage' dict to :class:`TokenUsage`.

    Tolerant of partial fields. Unknown fields are ignored. Cost is
    tracked separately on :class:`ResultEvent`.
    """
    if not isinstance(d, dict):
        return TokenUsage()
    return TokenUsage(
        input_tokens=int(d.get("input_tokens") or 0),
        output_tokens=int(d.get("output_tokens") or 0),
        cache_read_tokens=int(d.get("cache_read_input_tokens") or d.get("cache_read_tokens") or 0),
        cache_creation_tokens=int(
            d.get("cache_creation_input_tokens") or d.get("cache_creation_tokens") or 0
        ),
    )


def _add_usage(a: TokenUsage, b: TokenUsage) -> TokenUsage:
    return TokenUsage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cache_read_tokens=a.cache_read_tokens + b.cache_read_tokens,
        cache_creation_tokens=a.cache_creation_tokens + b.cache_creation_tokens,
    )


def _extract_assistant_text(message: dict[str, Any]) -> str:
    """Return the first text block's content, truncated for log lines."""
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if isinstance(text, str):
                    return text[:200]
    if isinstance(content, str):
        return content[:200]
    return ""


def parse_line(line: str | bytes) -> dict[str, Any] | None:
    """Parse a single NDJSON line into a dict, or ``None`` on malformed JSON."""
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            return None
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def parse_lines(
    lines: Iterable[str | bytes],
    *,
    summary: StreamSummary | None = None,
) -> Iterator[SystemInitEvent | AssistantMessageEvent | UserMessageEvent | ResultEvent]:
    """Yield typed events from an iterable of NDJSON lines.

    The optional ``summary`` is mutated in-place so callers can inspect
    cumulative state after the iterator finishes — handy for the
    dispatcher's "what was the final usage?" path.

    Empty / whitespace-only lines are skipped silently (not counted as
    drift); only non-empty lines that fail to parse increment
    ``skipped_lines``.
    """
    if summary is None:
        summary = StreamSummary()

    for raw_line in lines:
        candidate = (
            raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
        )
        if not candidate.strip():
            continue

        obj = parse_line(raw_line)
        if obj is None:
            summary.skipped_lines += 1
            continue

        evt_type = obj.get("type")
        summary.event_count += 1

        if evt_type == "system":
            sub = obj.get("subtype")
            if sub == "init":
                session_id = obj.get("session_id")
                if isinstance(session_id, str):
                    summary.session_id = session_id
                    yield SystemInitEvent(session_id=session_id, raw=obj)
                    continue
            # Unknown system subtype — count it but don't yield typed event.
            continue

        if evt_type == "assistant":
            message = obj.get("message")
            if isinstance(message, dict):
                delta = _coerce_token_usage(message.get("usage"))
                summary.cumulative_usage = _add_usage(summary.cumulative_usage, delta)
                yield AssistantMessageEvent(
                    text_excerpt=_extract_assistant_text(message),
                    usage_delta=delta,
                    raw=obj,
                )
            continue

        if evt_type == "user":
            yield UserMessageEvent(raw=obj)
            continue

        if evt_type == "result":
            final_usage = _coerce_token_usage(obj.get("usage"))
            cost = obj.get("total_cost_usd") or obj.get("cost_usd") or 0.0
            duration_ms = int(obj.get("duration_ms") or 0)
            stop_reason = str(obj.get("stop_reason") or obj.get("subtype") or "unknown")
            is_error = bool(obj.get("is_error", obj.get("subtype") == "error"))
            event = ResultEvent(
                subtype=str(obj.get("subtype") or "result"),
                stop_reason=stop_reason,
                is_error=is_error,
                cost_usd=float(cost),
                duration_ms=duration_ms,
                final_usage=final_usage,
                raw=obj,
            )
            summary.final_result = event
            yield event
            continue

        # Unrecognized event type — count and continue.
        summary.skipped_lines += 1
