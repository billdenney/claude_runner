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

    # Pre-trust the spawn CWD and mark onboarding complete in the target
    # .claude.json. Idempotent — a no-op once the flags are set. Done for
    # the default CLAUDE_CONFIG_DIR too (config_path defaults to ~/.claude
    # inside the helper) so fresh accounts don't hit the trust dialog.
    from claude_task_runner.claude_init import ensure_initialized as _ensure_claude_init

    _ensure_claude_init(claude_config_dir or None, Path.cwd())

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

        # Phase 1: race onboarding prompts against the TUI-ready marker.
        #
        # Different .claude config dirs land at different first frames; we
        # loop, dismissing whichever prompt appears, until the "shortcuts"
        # footer marks the TUI as ready (or we hit a timeout slice without
        # any match).
        #
        # Known dismissable prompts:
        #   - "Yes, I trust this folder"  (Claude <= 2.1.131 trust prompt)
        #   - "Quick safety check"        (Claude >= 2.1.141 trust prompt)
        #   - "colorblind-friendly"       (first-run theme picker — appears
        #                                  when .claude.json lacks
        #                                  `hasCompletedOnboarding=true`;
        #                                  the longer phrases like
        #                                  "Choose the text style" are
        #                                  split by ANSI cursor escapes,
        #                                  so we anchor on a contiguous
        #                                  hyphenated word from the option
        #                                  list.)
        # Each is dismissed by sending Enter (trust prompts accept the
        # highlighted default) or "1" + Enter (theme picker selects the
        # first option, "Auto"). Operator escape hatches: pre-set
        # `hasTrustDialogAccepted=true` and `hasCompletedOnboarding=true`
        # in <config_dir>/.claude.json to skip both prompts entirely.
        ready = False
        for _attempt in range(5):  # bound dismissals — guards a stuck loop
            try:
                idx = child.expect(
                    [
                        b"Yes, I trust this folder",  # 0: legacy trust
                        b"Quick safety check",  # 1: newer trust
                        b"colorblind-friendly",  # 2: theme picker
                        b"shortcuts",  # 3: TUI ready
                        pexpect.TIMEOUT,  # 4
                    ],
                    timeout=settings.capture_trust_timeout_s,
                )
            except pexpect.EOF as exc:
                raise UsageCaptureTimeout("claude exited before any TUI marker appeared") from exc
            if idx in (0, 1):
                child.sendline("")  # Enter accepts the default
            elif idx == 2:
                child.sendline("1")  # "1" = Auto theme
            elif idx == 3:
                ready = True
                break
            else:  # TIMEOUT — nothing matched this slice; give up
                break

        if not ready:
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

        # EARLIEST SNAPSHOT: right after both Resets markers appeared.
        # The TUI has finished rendering the 5-hour and weekly blocks at
        # this point (we wouldn't have seen 2 Resets otherwise). Some
        # TUI versions then auto-navigate or scroll the panel away
        # during the post-data-pad drain, so capturing here is the
        # safest moment to preserve the data the parser needs.
        snapshot_post_resets = log_buf.getvalue()

        # Drain post-Resets output so any in-place redraws of the
        # placeholder by the real OAuth response are captured. Older
        # TUI versions used in-place redraw to replace "Refreshing…"
        # placeholders; the parser handles this by taking the LAST two
        # blocks. Newer TUI versions auto-navigate to a daily view
        # during this window; the snapshot above is the fallback for
        # that case.
        if settings.capture_post_data_pad_ms > 0:
            # Process exit during the drain is fine; whatever we captured is
            # what we get.
            with contextlib.suppress(pexpect.EOF):
                child.expect(
                    pexpect.TIMEOUT,
                    timeout=settings.capture_post_data_pad_ms / 1000.0,
                )

        # LATER SNAPSHOT: after the drain but before cleanup. Used as
        # the primary parse target when it differs from the
        # post-Resets snapshot AND contains usage blocks (we'll only
        # know after attempting to parse — see the caller).
        raw_for_parse = log_buf.getvalue()

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

    raw_full = log_buf.getvalue()
    capture_path.write_bytes(raw_full)
    _rotate_captures(captures_dir, settings.capture_rotation_count)

    # Pick the best snapshot for the parser. We have up to three
    # candidates, EARLIEST first:
    #   * `snapshot_post_resets`: log_buf RIGHT after both Resets
    #     markers appeared.
    #   * `raw_for_parse`: log_buf after the post-data-pad drain.
    #   * `raw_full`: log_buf including cleanup.
    #
    # Decision rule: render each candidate via pyte and count the
    # blocks the parser's block-builder would extract. Return the
    # FIRST candidate (earliest, freshest) that yields >= 2 blocks.
    # If none reach 2 blocks, return the candidate with the most
    # blocks; tiebreak to the latest (consistent with the original
    # "drain handles placeholder overwrite" semantics for legacy TUIs).
    #
    # This rendering-aware choice handles both:
    #   - LEGACY TUI: placeholder "Refreshing…" overwritten in-place
    #     by API response during the drain. Late snapshots are
    #     correct; early ones may show the placeholder.
    #   - NEW TUI (>= 2.1.141): the /usage panel auto-navigates to a
    #     daily-Sonnet view during the drain, removing the main
    #     blocks from the visible screen state. Early snapshot wins.
    #
    # Local import to avoid module-load cycles; the parser/render
    # modules are siblings under `usage/`.
    from claude_task_runner.usage import parser as _parser
    from claude_task_runner.usage import render as _render

    try:
        candidates: list[tuple[str, bytes]] = [
            ("post_resets", snapshot_post_resets),
            ("post_drain", raw_for_parse),
            ("full", raw_full),
        ]
    except UnboundLocalError:
        # Phase 2 didn't reach the snapshot point; return the full stream.
        return raw_full, capture_path

    best = (-1, candidates[-1][1])  # (n_blocks, raw) — start with full
    for _, candidate in candidates:
        try:
            blocks = _parser._extract_blocks(_render.render(candidate))
        except Exception:
            continue
        n = len(blocks)
        if n >= 2:
            # Earliest snapshot with at least 2 blocks wins — return now.
            return candidate, capture_path
        if n > best[0]:
            best = (n, candidate)
    return best[1], capture_path


def _sleep_ms(milliseconds: int) -> None:
    """Sleep helper isolated for monkey-patching in tests."""
    if milliseconds <= 0:
        return
    import time as _time  # local import keeps test patching tidy

    _time.sleep(milliseconds / 1000.0)
