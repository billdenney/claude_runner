"""Tests for the logging-setup helper.

The supervisor's logs were silently dropped before this module
existed (no handler attached to the root logger). These tests pin
the public guarantees:

* :func:`configure_logging` installs exactly one stderr handler.
* Repeated calls with the same ``(level, fmt)`` are no-ops; calling
  with different args replaces the handler.
* The handler emits structured output (timestamp + level + logger +
  the message + any structlog-bound key=values).
* JSON mode produces one JSON object per record.
* Noisy third-party loggers (pexpect, httpx) are capped at WARNING
  even when the operator picks DEBUG for the root.
"""

from __future__ import annotations

import io
import json
import logging

import structlog

from claude_task_runner.observability import (
    LogFormat,
    _reset_for_tests,
    configure_logging,
)


def _capture_stderr(monkeypatch) -> io.StringIO:
    """Redirect sys.stderr to a StringIO so we can read what was logged."""
    import sys

    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    return buf


def setup_function(_func) -> None:
    """Reset the module-level cache before each test."""
    _reset_for_tests()


def test_installs_exactly_one_stderr_handler() -> None:
    configure_logging(level="INFO", fmt="text")
    root = logging.getLogger()
    handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler)]
    assert len(handlers) == 1
    import sys

    assert handlers[0].stream is sys.stderr


def test_same_args_is_noop() -> None:
    """Two calls with identical args produce one handler total."""
    configure_logging(level="INFO", fmt="text")
    handlers_after_first = list(logging.getLogger().handlers)
    configure_logging(level="INFO", fmt="text")
    handlers_after_second = list(logging.getLogger().handlers)
    assert handlers_after_first == handlers_after_second


def test_different_args_replaces_handler() -> None:
    configure_logging(level="INFO", fmt="text")
    first = list(logging.getLogger().handlers)
    configure_logging(level="DEBUG", fmt="json")
    second = list(logging.getLogger().handlers)
    # Same count (1), but the handler object should differ.
    assert len(first) == len(second) == 1
    assert first[0] is not second[0]


def test_text_format_emits_iso_timestamp_and_level(monkeypatch) -> None:
    buf = _capture_stderr(monkeypatch)
    _reset_for_tests()
    configure_logging(level="DEBUG", fmt="text")
    logging.getLogger("test.logger").info("hello world", extra={"task_id": "t1"})
    out = buf.getvalue()
    # ISO 8601 marker.
    assert "T" in out and "Z" in out
    # Level + event present.
    assert "info" in out.lower()
    assert "hello world" in out


def test_json_format_emits_parseable_json_per_line(monkeypatch) -> None:
    buf = _capture_stderr(monkeypatch)
    _reset_for_tests()
    configure_logging(level="DEBUG", fmt="json")
    logging.getLogger("test.logger").info("hello json")
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    assert len(lines) >= 1
    parsed = json.loads(lines[-1])
    assert parsed["event"] == "hello json"
    assert parsed["level"] == "info"


def test_third_party_loggers_capped_at_warning(monkeypatch) -> None:
    """Even with root at DEBUG, pexpect / httpx don't flood the journal."""
    buf = _capture_stderr(monkeypatch)
    _reset_for_tests()
    configure_logging(level="DEBUG", fmt="text")
    logging.getLogger("pexpect").debug("should-be-suppressed")
    logging.getLogger("httpx").info("also-suppressed")
    logging.getLogger("httpcore").info("also-suppressed-3")
    out = buf.getvalue()
    assert "should-be-suppressed" not in out
    assert "also-suppressed" not in out
    assert "also-suppressed-3" not in out


def test_structlog_native_logger_uses_same_handler(monkeypatch) -> None:
    """structlog.get_logger().info(...) routes through the same stderr
    handler — the bridge in configure_logging hooks structlog's
    ProcessorFormatter into the stdlib root."""
    buf = _capture_stderr(monkeypatch)
    _reset_for_tests()
    configure_logging(level="DEBUG", fmt="json")
    structlog.get_logger("via-structlog").info("hi", account="personal")
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    parsed = json.loads(lines[-1])
    assert parsed["event"] == "hi"
    assert parsed.get("account") == "personal"


def test_default_args_install_info_level() -> None:
    _reset_for_tests()
    configure_logging()
    assert logging.getLogger().level == logging.INFO


def test_invalid_level_falls_back_to_info() -> None:
    """A typo in the operator's [logging].level shouldn't crash the
    supervisor; we default to INFO."""
    _reset_for_tests()
    configure_logging(level="BANANA")
    assert logging.getLogger().level == logging.INFO


def test_log_format_literal_includes_both_modes() -> None:
    """Schema-level: the LogFormat literal accepts text and json only."""
    # Smoke test: both values are accepted by configure_logging without
    # exceptions; an unknown value should fall through to ConsoleRenderer.
    for mode in ("text", "json"):
        _reset_for_tests()
        configure_logging(fmt=mode)  # type: ignore[arg-type]
    # Confirm LogFormat is what we think.
    assert LogFormat.__args__ == ("text", "json")  # type: ignore[attr-defined]
