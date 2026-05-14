"""Capture ``claude /usage`` output via pexpect.

Two phases:

1. **Trust prompt vs TUI ready** — race the "Yes, I trust this folder"
   prompt against the "shortcuts" marker. If trust prompt appears first,
   send Enter and continue waiting. Otherwise the welcome screen is
   already drawn.
2. **Submit /usage and wait for both Resets** — the TUI panel first shows
   "Loading usage data…" while it queries the OAuth API, then renders the
   actual numbers. We wait for two ``Resets`` matches.

Cleanup: send Esc, then ``/exit``, then expect EOF.

The full PTY stream (including ANSI) is always persisted to a forensics
.cap file in ``<runtime_dir>/usage_captures/<ts>.cap`` for post-mortem
debugging. See ADR-0008.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
from datetime import datetime
from pathlib import Path

import pexpect

from claude_task_runner.clock import Clock
from claude_task_runner.config.schema import UsageSettings
from claude_task_runner.usage.drift import UsageCaptureSpawnError, UsageCaptureTimeout


def _format_filename(when: datetime) -> str:
    return when.strftime("%Y%m%dT%H%M%SZ.cap")


def _rotate_captures(captures_dir: Path, keep: int) -> None:
    """Drop the oldest .cap files so at most ``keep`` remain."""
    if keep <= 0:
        return
    caps = sorted(captures_dir.glob("*.cap"))
    excess = len(caps) - keep
    for old in caps[:excess]:
        # Don't let rotation failure break a capture.
        with contextlib.suppress(OSError):
            old.unlink()


def capture(
    settings: UsageSettings,
    clock: Clock,
    *,
    captures_dir: Path,
    claude_executable: str = "claude",
    claude_config_dir: str = "",
) -> tuple[bytes, Path]:
    """Spawn ``claude``, send ``/usage``, and return the raw PTY bytes.

    The raw bytes are also persisted to ``captures_dir/<ISO>.cap`` for
    forensics. Returns ``(raw_bytes, capture_path)``.

    Parameters
    ----------
    settings
        Pre-validated usage section of the config.
    clock
        Time source for naming the capture file.
    captures_dir
        Where to write the forensics .cap. Created if it does not exist.
    claude_executable
        Override for tests / non-PATH binaries.
    claude_config_dir
        If non-empty, exported as ``CLAUDE_CONFIG_DIR`` so claude reads
        credentials from the given directory instead of ``~/.claude``.
        Used to target non-default accounts (work vs personal).

    Raises
    ------
    UsageCaptureSpawnError
        If the ``claude`` binary cannot be located or launched.
    UsageCaptureTimeout
        If the trust prompt or both ``Resets`` lines do not appear within
        their configured timeouts.
    """
    if shutil.which(claude_executable) is None:
        raise UsageCaptureSpawnError(f"binary not found in PATH: {claude_executable}")

    captures_dir.mkdir(parents=True, exist_ok=True)
    capture_path = captures_dir / _format_filename(clock.now())

    spawn_env: dict[str, str] | None = None
    if claude_config_dir:
        config_path = Path(claude_config_dir).expanduser()
        if not config_path.exists():
            raise UsageCaptureSpawnError(f"CLAUDE_CONFIG_DIR does not exist: {config_path}")
        spawn_env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config_path)}

    log_buf = io.BytesIO()
    child: pexpect.spawn[bytes] | None = None
    try:
        # encoding=None -> raw bytes mode. We strip ANSI in the parser.
        child = pexpect.spawn(
            claude_executable,
            timeout=settings.capture_trust_timeout_s,
            encoding=None,
            env=spawn_env,
        )
        child.logfile_read = log_buf

        # Phase 1: race trust prompt vs TUI-ready marker.
        #
        # Two trust-prompt shapes are known:
        #   - Claude <= 2.1.131:   "Yes, I trust this folder"  (Enter accepts)
        #   - Claude >= 2.1.141:   "Quick safety check: Is this a project..."
        #                          (Enter on the highlighted default accepts;
        #                          if that ever stops working, the operator
        #                          can pre-trust the directory by flipping
        #                          `hasTrustDialogAccepted=true` in
        #                          <config_dir>/.claude.json, which makes the
        #                          prompt vanish entirely.)
        # Either prompt -> send Enter and then wait for the "shortcuts"
        # TUI-ready marker.
        try:
            idx = child.expect(
                [
                    b"Yes, I trust this folder",
                    b"Quick safety check",
                    b"shortcuts",
                    pexpect.TIMEOUT,
                ],
                timeout=settings.capture_trust_timeout_s,
            )
        except pexpect.EOF as exc:
            raise UsageCaptureTimeout("claude exited before any TUI marker appeared") from exc

        if idx in (0, 1):
            child.sendline("")  # confirm trust (Enter accepts the default)
            try:
                child.expect(
                    [b"shortcuts", pexpect.TIMEOUT],
                    timeout=settings.capture_trust_timeout_s,
                )
            except pexpect.EOF as exc:
                raise UsageCaptureTimeout(
                    "claude exited after trust confirmation, before TUI ready"
                ) from exc
        elif idx == 3:
            raise UsageCaptureTimeout(
                f"TUI did not become ready within {settings.capture_trust_timeout_s}s"
            )

        # Pad before sending /usage — "shortcuts" appears before the input
        # field is actually accepting commands.
        _sleep_ms(settings.capture_post_ready_pad_ms)

        # Phase 2: send /usage and wait for both Resets lines.
        # The TUI shows placeholder values immediately ("Refreshing…"),
        # then overwrites with the real values from the OAuth API. If
        # we exit at the first 2 Resets we'd capture the placeholder.
        # Strategy: wait for both Resets to appear, THEN drain output for
        # `capture_post_data_pad_ms` so the API response has time to land
        # and overwrite. The parser takes the *last* 2 blocks, so the
        # placeholder is naturally discarded.
        child.send(b"/usage\r")
        resets_seen = 0
        deadline_s = settings.capture_usage_timeout_s
        while resets_seen < 2:
            try:
                idx = child.expect(
                    [b"Resets", pexpect.TIMEOUT],
                    timeout=deadline_s,
                )
            except pexpect.EOF as exc:
                raise UsageCaptureTimeout("claude exited mid-/usage rendering") from exc
            if idx == 1:
                raise UsageCaptureTimeout(
                    f"only saw {resets_seen} of 2 Resets lines within "
                    f"{settings.capture_usage_timeout_s}s"
                )
            resets_seen += 1

        # Drain post-Resets output so any in-place redraws of the
        # placeholder by the real OAuth response are captured.
        if settings.capture_post_data_pad_ms > 0:
            # Process exit during the drain is fine; whatever we captured is
            # what we get.
            with contextlib.suppress(pexpect.EOF):
                child.expect(
                    pexpect.TIMEOUT,
                    timeout=settings.capture_post_data_pad_ms / 1000.0,
                )

        # Cleanup: Esc, then /exit, then EOF.
        child.send(b"\x1b")
        child.send(b"/exit\r")
        try:
            child.expect(pexpect.EOF, timeout=settings.capture_eof_timeout_s)
        except pexpect.TIMEOUT:
            # Best-effort: kill the child if it didn't exit cleanly.
            with contextlib.suppress(OSError):
                child.terminate(force=True)

    finally:
        if child is not None and child.isalive():
            with contextlib.suppress(OSError):
                child.terminate(force=True)

    raw = log_buf.getvalue()
    capture_path.write_bytes(raw)
    _rotate_captures(captures_dir, settings.capture_rotation_count)
    return raw, capture_path


def _sleep_ms(milliseconds: int) -> None:
    """Sleep helper isolated for monkey-patching in tests."""
    if milliseconds <= 0:
        return
    import time as _time  # local import keeps test patching tidy

    _time.sleep(milliseconds / 1000.0)
