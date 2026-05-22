"""Logging setup: structlog frontend, stdlib-compatible bridge, journald sink.

Before this module existed, the supervisor used ``logging.getLogger(__name__)``
everywhere but never configured a root handler — so every ``logger.info``
call was silently dropped at the WARNING-level default. After
``systemctl --user start claude-task-runner.service``, ``journalctl
--user -u claude-task-runner`` only ever showed the systemd unit's own
``Started ...`` line; nothing from the Python process reached the
journal. Operators had no visibility into dispatch decisions, throttle
transitions, or capture failures.

This module fixes that with three guarantees:

1. **Structured output.** :mod:`structlog` processors render every log
   line as a timestamped, level-tagged key=value sequence (or JSON
   when requested). Both the supervisor's own ``logger.info(...)``
   calls and any third-party library logs flow through the same
   processor chain via :class:`structlog.stdlib.ProcessorFormatter`.
2. **journald capture.** stderr is the destination handler. systemd
   user services capture stderr into the journal automatically; no
   ``systemd.journal.JournalHandler`` dependency is needed. ``journalctl
   --user -u claude-task-runner`` shows the lines as they're written.
3. **Idempotent.** Calling :func:`configure_logging` more than once
   does nothing on the second call (it sets a module-level sentinel).
   Tests can call it freely; CLI commands can call it from every
   sub-entry-point.

Output format is controlled by ``[logging]`` settings (see
:mod:`config.schema`) with ``text`` (key=value, the default) for
operator-readable journalctl output, and ``json`` for machine
parsing (Loki, Vector, etc.).
"""

from __future__ import annotations

import logging
import sys
from typing import Literal

import structlog

LogFormat = Literal["text", "json"]
"""Output format selector.

* ``"text"`` (default) — key=value lines, human-readable. Recommended
  for ``journalctl`` consumption and operator-driven debugging.
* ``"json"`` — single-line JSON per record. Pipe-friendly for log
  shippers (Loki, Vector, Fluent Bit).
"""

_last_applied: tuple[str, str] | None = None
"""Module-level cache of the (level, fmt) most recently applied.

The CLI calls :func:`configure_logging` twice: once early from
``cli/__init__.py`` (with env-var-or-default settings, before Typer
parses arguments) and once after a settings-loading command resolves
``[logging]`` from the queue TOML. Tracking the last-applied tuple
lets the second call re-apply only when the values changed,
preserving the historical "tests can call freely" guarantee while
still respecting per-queue overrides."""


def configure_logging(
    *,
    level: str = "INFO",
    fmt: LogFormat = "text",
) -> None:
    """Wire structlog + stdlib logging at process start.

    Re-entrant: calling with the same ``(level, fmt)`` after a prior
    call is a no-op (the existing handler stays in place). Calling
    with DIFFERENT args replaces the configured handler so per-queue
    ``[logging]`` settings override the env-var-driven early defaults
    from :func:`cli._early_configure_logging`.

    Parameters
    ----------
    level
        Standard logging level name (``"DEBUG"``, ``"INFO"``,
        ``"WARNING"``, ``"ERROR"``). Anything below is dropped at the
        root handler.
    fmt
        ``"text"`` (key=value, default) or ``"json"`` (one JSON
        object per line). The ``[logging].format`` settings field
        threads through to here.
    """
    global _last_applied
    if _last_applied == (level, fmt):
        return

    numeric_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    # Shared processor chain that runs for BOTH structlog-native loggers
    # (acquired via ``structlog.get_logger()``) and stdlib loggers (via
    # the ProcessorFormatter bridge below). Keep these processors pure —
    # they're called in hot paths.
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        timestamper,
    ]

    # Pick the final renderer. ``ConsoleRenderer`` is journalctl-
    # friendly; ``JSONRenderer`` is shipper-friendly. The renderer must
    # run AFTER the shared processors so it sees the full event dict.
    final_renderer: structlog.typing.Processor
    if fmt == "json":
        final_renderer = structlog.processors.JSONRenderer()
    else:
        final_renderer = structlog.dev.ConsoleRenderer(colors=False)

    # Configure structlog (native loggers).
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Configure stdlib root logger: one StreamHandler to stderr,
    # formatted via structlog's ProcessorFormatter so stdlib log
    # records render through the same processor chain.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            final_renderer,
        ],
    )
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(formatter)
    handler.setLevel(numeric_level)

    root = logging.getLogger()
    # Clear any pre-existing handlers so we don't double-emit if a
    # caller had already called basicConfig (rare but possible).
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(numeric_level)

    # Quiet noisy third-party loggers that hit DEBUG/INFO by default.
    # The supervisor doesn't care about pexpect's PTY data or httpx's
    # per-request lines unless we're explicitly debugging.
    for noisy in ("pexpect", "httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(max(numeric_level, logging.WARNING))

    _last_applied = (level, fmt)


def _reset_for_tests() -> None:
    """Test helper: drop the cache so the next call re-applies.

    NOT exported in :data:`__all__`. Tests that exercise the logging
    setup itself use this to drive multiple ``configure_logging`` calls
    deterministically; production code should never need it.
    """
    global _last_applied
    _last_applied = None
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)


__all__ = ["LogFormat", "configure_logging"]
