"""Parse Claude Code's ``--output-format=stream-json`` NDJSON stream.

The claude binary emits one JSON object per line when invoked with
``claude --print --output-format=stream-json --verbose``. The runner
consumes those lines as they're produced to:

1. Capture the ``session_id`` from the first ``system/init`` event so
   that subsequent attempts can ``--resume`` mid-task across rate-limit
   windows (ADR-0005).
2. Update :class:`TokenUsage` aggregates for the per-task token cap.
3. Emit timestamps so :mod:`runner.heartbeat` can flag silence.
4. Surface the final ``result`` event's ``stop_reason`` and accumulated
   ``cost_usd`` for the :class:`RunRecord`.

This module is the **pure parser**. The dispatcher reads bytes from the
subprocess and feeds them to :func:`parse_lines`. We DON'T spawn
subprocesses here.

Robust parsing: a malformed line, an event of a known type that lacks
what the runner needs, or an event of a type this parser does not
recognize, is skipped and counted in :attr:`StreamSummary.skipped_lines`
rather than aborting the run; the last two are also counted by type. A
value the runner cannot use, such as a ``NaN`` token count, is replaced
and counted in :attr:`StreamSummary.unusable_values`, so no value in the
stream can keep a finished run from being recorded. The dispatcher records
the skipped count on the run's :class:`RunRecord` and logs a warning for
each kind of count, since a non-zero count can mean the stream format has
drifted. The informational event types in ``_QUIETLY_SKIPPED_EVENT_TYPES``
are skipped without being counted. If the entire stream produces zero
events, the caller should treat that as a process error, not a parse error.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from claude_task_runner.queue.schema import TokenUsage

logger = logging.getLogger(__name__)

_QUIETLY_SKIPPED_EVENT_TYPES = frozenset({"rate_limit_event", "tool_progress"})
"""Informational event types claude emits that the runner has no use for.

They are skipped without being counted as drift, and never yielded. Not
yielding matters for ``tool_progress``: claude sends it while a tool runs,
including periodic heartbeats during a long Bash call. The dispatcher ticks
its heartbeat on every yielded event, and the stuck-sleep-loop reaper acts
only while that heartbeat is stale, so yielding these would make a worker
stuck in a Bash poll loop look alive. ``rate_limit_event`` carries the
account's rate-limit status. Both shapes were checked against claude 2.1.281.
"""

_TOKEN_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("input_tokens", ("input_tokens",)),
    ("output_tokens", ("output_tokens",)),
    ("cache_read_tokens", ("cache_read_input_tokens", "cache_read_tokens")),
    ("cache_creation_tokens", ("cache_creation_input_tokens", "cache_creation_tokens")),
)
"""Each :class:`TokenUsage` field, with the ``usage`` keys it is read from,
most preferred first."""

_MAX_TOKEN_COUNT = 2**53
"""The largest token count accepted: the largest integer a float holds
exactly. The token cap reports totals as floats, and no real count is near."""


@dataclass(frozen=True)
class SystemInitEvent:
    """The first line — carries the new session id."""

    session_id: str


@dataclass(frozen=True)
class AssistantMessageEvent:
    """A model-emitted message. Its usage is already in
    :attr:`StreamSummary.cumulative_usage` when it is yielded."""


@dataclass(frozen=True)
class UserMessageEvent:
    """A user-side message (tool result, follow-up prompt)."""


@dataclass(frozen=True)
class ResultEvent:
    """The final line, summarizing the run.

    All fields are derived from common stream-json shapes but we
    tolerate missing keys so a slightly-different upstream version
    doesn't crash the runner.
    """

    stop_reason: str
    is_error: bool
    cost_usd: float | None
    """``None`` when the line carried a cost the runner cannot use; see
    :attr:`StreamSummary.unusable_values`. 0.0 when it carried none."""
    final_usage: TokenUsage


@dataclass
class StreamSummary:
    """Running totals updated as :func:`parse_lines` yields events."""

    session_id: str | None = None
    cumulative_usage: TokenUsage = field(default_factory=TokenUsage)
    final_result: ResultEvent | None = None
    skipped_lines: int = 0
    unknown_event_types: dict[str, int] = field(default_factory=dict)
    """How many of :attr:`skipped_lines` were events of each unrecognized
    ``type``."""
    unusable_event_types: dict[str, int] = field(default_factory=dict)
    """How many of :attr:`skipped_lines` were events of a known type that
    lack what the runner needs: ``system/init`` without a string
    ``session_id``, or ``assistant`` whose ``message`` is not an object. The
    rest of :attr:`skipped_lines`, beyond these and
    :attr:`unknown_event_types`, were malformed lines."""
    unusable_values: dict[str, int] = field(default_factory=dict)
    """How many values in parsed events the runner could not use, keyed by
    where they were (``result.total_cost_usd``,
    ``assistant.usage.input_tokens``, ...). A missing or ``null`` value is
    not counted. Each counted value was replaced: a token count by 0, a
    ``usage`` that is not an object by zero usage, a cost by ``None``
    (unknown), and ``is_error`` by the ``subtype`` fallback."""


def _count_unusable(summary: StreamSummary, where: str) -> None:
    summary.unusable_values[where] = summary.unusable_values.get(where, 0) + 1


def _skip_unusable_event(summary: StreamSummary, kind: str) -> None:
    summary.skipped_lines += 1
    summary.unusable_event_types[kind] = summary.unusable_event_types.get(kind, 0) + 1


def _token_count(value: Any) -> int | None:
    """``value`` as a token count, or ``None`` if it is not a whole JSON
    number from 0 to ``_MAX_TOKEN_COUNT``.

    A ``bool`` is not a count, though Python treats ``True`` as 1. ``NaN``
    and infinities fail :meth:`float.is_integer`.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= _MAX_TOKEN_COUNT else None
    if isinstance(value, float) and value.is_integer() and 0 <= value <= _MAX_TOKEN_COUNT:
        return int(value)
    return None


def _cost_usd(value: Any) -> float | None:
    """``value`` as a cost, or ``None`` if it is not a finite, non-negative
    JSON number.

    Python's json reads ``NaN``, ``Infinity`` and a float literal too large
    for a float (``1e999``) as non-finite floats, and an integer literal of
    any size as an int.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        cost = float(value)
    except OverflowError:  # an integer literal too large for a float
        return None
    return cost if math.isfinite(cost) and cost >= 0 else None


def _coerce_token_usage(usage: Any, summary: StreamSummary, where: str) -> TokenUsage:
    """Convert an event's ``usage`` object to :class:`TokenUsage`.

    A missing or ``null`` count is 0. A present count that is not usable
    (see :func:`_token_count`) is also 0, and is counted in
    :attr:`StreamSummary.unusable_values` under ``<where>.usage.<key>``;
    a ``usage`` that is not an object is counted under ``<where>.usage``.
    Unknown keys are ignored. Cost is tracked separately on
    :class:`ResultEvent`.
    """
    if usage is None:
        return TokenUsage()
    if not isinstance(usage, dict):
        _count_unusable(summary, f"{where}.usage")
        return TokenUsage()
    counts: dict[str, int] = {}
    for name, keys in _TOKEN_FIELDS:
        key = next((k for k in keys if usage.get(k) is not None), None)
        if key is None:
            continue
        count = _token_count(usage[key])
        if count is None:
            _count_unusable(summary, f"{where}.usage.{key}")
            continue
        counts[name] = count
    return TokenUsage(**counts)


def _add_usage(a: TokenUsage, b: TokenUsage) -> TokenUsage:
    return TokenUsage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cache_read_tokens=a.cache_read_tokens + b.cache_read_tokens,
        cache_creation_tokens=a.cache_creation_tokens + b.cache_creation_tokens,
    )


def _result_event(obj: dict[str, Any], summary: StreamSummary) -> ResultEvent:
    """Build the :class:`ResultEvent` for a ``result`` line.

    ``stop_reason`` falls back to ``subtype``, which claude relies on: it
    sends ``stop_reason: null`` on an ``error_during_execution`` result.
    ``is_error`` falls back to whether ``subtype`` starts with ``error``
    (``error_during_execution``, ``error_max_turns``,
    ``error_max_budget_usd``, ...). The cost is ``total_cost_usd``, or the
    older ``cost_usd`` when that is missing.
    """
    subtype = obj.get("subtype")
    stop_reason = str(obj.get("stop_reason") or subtype or "unknown")

    raw_is_error = obj.get("is_error")
    if isinstance(raw_is_error, bool):
        is_error = raw_is_error
    else:
        if raw_is_error is not None:
            _count_unusable(summary, "result.is_error")
        is_error = isinstance(subtype, str) and subtype.startswith("error")

    cost_key = "total_cost_usd" if obj.get("total_cost_usd") is not None else "cost_usd"
    raw_cost = obj.get(cost_key)
    cost: float | None = 0.0
    if raw_cost is not None:
        cost = _cost_usd(raw_cost)
        if cost is None:
            _count_unusable(summary, f"result.{cost_key}")

    return ResultEvent(
        stop_reason=stop_reason,
        is_error=is_error,
        cost_usd=cost,
        final_usage=_coerce_token_usage(obj.get("usage"), summary, "result"),
    )


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

    The optional ``summary`` is mutated in-place before each event is
    yielded, so callers can read the running totals mid-stream (the
    dispatcher checks the token cap after every event) or after the
    iterator finishes (its "what was the final usage?" path).

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

        if evt_type == "system":
            if obj.get("subtype") != "init":
                # Another system subtype: a known type, so not drift; nothing to yield.
                continue
            session_id = obj.get("session_id")
            if not isinstance(session_id, str):
                _skip_unusable_event(summary, "system/init")
                continue
            summary.session_id = session_id
            yield SystemInitEvent(session_id=session_id)
            continue

        if evt_type == "assistant":
            message = obj.get("message")
            if not isinstance(message, dict):
                _skip_unusable_event(summary, "assistant")
                continue
            delta = _coerce_token_usage(message.get("usage"), summary, "assistant")
            summary.cumulative_usage = _add_usage(summary.cumulative_usage, delta)
            yield AssistantMessageEvent()
            continue

        if evt_type == "user":
            yield UserMessageEvent()
            continue

        if evt_type == "result":
            event = _result_event(obj, summary)
            summary.final_result = event
            yield event
            continue

        if isinstance(evt_type, str) and evt_type in _QUIETLY_SKIPPED_EVENT_TYPES:
            continue

        # Unrecognized event type — count it by name and continue. A
        # missing or non-string ``type`` is named by its Python type.
        summary.skipped_lines += 1
        name = evt_type if isinstance(evt_type, str) else f"<{type(evt_type).__name__}>"
        summary.unknown_event_types[name] = summary.unknown_event_types.get(name, 0) + 1
