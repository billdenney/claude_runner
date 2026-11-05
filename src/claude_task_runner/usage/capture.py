"""Capture ``claude /usage`` output via pexpect.

Two phases:

1. **Input box ready** — feed the stream into a virtual terminal the size
   of the PTY until the screen shows the input box's footer with no
   dialog over it. The checks read the rendered screen, not the byte
   stream: the TUI paints the words of a line with cursor-column jumps
   between them, so a phrase is rarely contiguous in the bytes. On the
   way, first-run prompts are answered (trust: "Yes, I trust this folder";
   theme picker: the highlighted theme) and the fullscreen-renderer offer,
   or any other confirm dialog, is closed with Esc, which never opts in.
   A login-method screen, or a banner that names API billing once the
   input box is ready, ends the capture with
   :class:`UsageCaptureLoginRequired`, because ``/usage`` then has no plan
   limits to show (ADR-0039).
2. **Submit /usage and wait for both Resets** — the TUI panel first shows
   "Loading usage data…" while it queries the OAuth API, then renders the
   actual numbers. We wait for two ``Resets`` matches.

Cleanup: send Esc, then ``/exit``, then expect EOF.

The full PTY stream (including ANSI) is persisted to a forensics .cap
file in ``<runtime_dir>/usage_captures/<ts>.cap`` for post-mortem
debugging, whether the capture succeeds or fails once ``claude`` has
started. See ADR-0008.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pexpect

from claude_task_runner.clock import Clock
from claude_task_runner.config.paths import ConfigDirError, existing_config_dir
from claude_task_runner.config.schema import UsageSettings
from claude_task_runner.usage.drift import (
    UsageCaptureLoginRequired,
    UsageCaptureSpawnError,
    UsageCaptureTimeout,
)
from claude_task_runner.usage.render import LiveScreen

logger = logging.getLogger(__name__)

_PTY_ROWS = 24
_PTY_COLUMNS = 80
"""The pseudo-terminal ``claude`` draws into: pexpect's default size, at
which the parser fixtures were recorded. Phase 1 renders at the same size,
so it reads the screen ``claude`` drew."""

_POLL_S = 0.25
"""Longest wait for output before phase 1 looks at the screen again."""

_READ_CHUNK = 65536

_RESEND_S = 2.0
"""How long a prompt may stay unchanged after its keys before phase 1 sends
them again: the TUI drops keys that arrive while it is still starting."""

_MAX_ANSWERS = 8
"""Keys a capture sends to prompts, over both runs of phase 1, before it
gives up: a bound on a prompt that ignores them, or a TUI that keeps
opening new ones."""

_ENTER = b"\r"
_ESC = b"\x1b"
_DOWN = b"\x1b[B"

_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"

_FOCUSED_OPTION_RE = re.compile(r"^\s*\u276f\s*(?:\d+\.\s*)?(\S.*?)\s*$", re.MULTILINE)
"""The option a selection list has the focus on, after its marker U+276F
(heavy right-pointing angle quotation mark)."""


@dataclass(frozen=True)
class _Prompt:
    """Something drawn over the input box, and how to answer it."""

    name: str
    pattern: re.Pattern[str]
    keys: bytes = b""
    choose: re.Pattern[str] | None = None
    """The option to pick, for a prompt answered by choice instead of ``keys``."""

    def keys_for(self, screen: str) -> bytes | None:
        """The keys to send to this prompt as ``screen`` shows it.

        For a prompt with ``choose``: Enter when the focus is on that
        option, Down while it is not, ``None`` while no focus is drawn.
        """
        if self.choose is None:
            return self.keys
        focused = _FOCUSED_OPTION_RE.search(screen)
        if focused is None:
            return None
        return _ENTER if self.choose.search(focused.group(1)) else _DOWN


_PROMPTS = (
    # 2.1.318 focuses "No, exit", which Enter or Esc would choose; older
    # versions focused "1. Yes, proceed". Trust prompts come before the Esc
    # rules for that reason.
    _Prompt(
        "the folder trust prompt",
        re.compile(r"Yes,\s*I\s*trust\s*this\s*folder|Quick\s*safety\s*check"),
        choose=re.compile(r"^Yes\b"),
    ),
    # The first-run theme picker: Enter keeps the highlighted theme.
    _Prompt("the theme picker", re.compile(r"colorblind-friendly"), _ENTER),
    # "Yes, try it" has the focus, so Enter would opt in; Esc is "Not now".
    _Prompt("the fullscreen-renderer offer", re.compile(r"fullscreen\s*renderer\?"), _ESC),
    # Any other dialog with this footer: Esc cancels without choosing.
    _Prompt(
        "a confirm dialog",
        re.compile(r"Enter\s*to\s*confirm\s*·\s*Esc\s*to\s*cancel"),
        _ESC,
    ),
)

_LOGIN_METHOD_RE = re.compile(r"Select\s*login\s*method")
"""The first-run screen of a config dir with no login, after the theme."""

_INPUT_BOX_FOOTER_RE = re.compile(r"\?\s*for\s*shortcuts|\son\s*\([^()\n]*\bto\s*cycle\)")
"""The footer under the input box, in any permission mode: ``? for
shortcuts`` (manual or default mode), or ``<mode> on (shift+tab to cycle)``
(auto, accept edits, plan, bypass permissions)."""

_BANNER_ROWS = 6
"""The top rows of the screen, which hold the welcome banner at start-up."""

_API_BILLING_RE = re.compile(
    r"^[^\n]*Claude Code v\d[^\n]*\n"  # the banner's title row
    r"[^\S\n]*[^\s·][^·\n]*·[^\S\n]*"  # the next row: logo and model, then " · "
    r"(Claude API|API Usage Billing)[^\S\n]*$",  # the billing label, ending the row
    re.MULTILINE,
)
"""The banner's billing label when ``claude`` has no plan login: ``Claude
API`` for a credential without a subscription, such as a ``setup-token``
token, and ``API Usage Billing`` when it is not logged in to claude.ai.

Only the 2.1.318 banner row counts, ``<logo> <model> · <billing>`` right
under ``Claude Code v<version>``, so a tip or notice that ends in either
phrase does not. The boxed banner of older versions does not match, and
the capture then goes on to ``/usage`` as it always did."""

_NOT_LOGGED_IN_RE = re.compile(r"Not\s*logged\s*in")


def _format_filename(when: datetime) -> str:
    return when.strftime("%Y%m%dT%H%M%SZ.cap")


def _build_spawn_env(claude_config_dir: str) -> dict[str, str] | None:
    """Build the env dict passed to ``pexpect.spawn`` for a usage capture.

    The capture authenticates with the account's claude.ai login in
    ``.credentials.json``. It never passes ``CLAUDE_CODE_OAUTH_TOKEN``, and
    drops one it would inherit: the CLI prefers that variable to the login,
    and a ``setup-token`` token in it has no ``user:profile`` scope, so
    ``/usage`` can show no plan limits (ADR-0039). The account's
    ``<config_dir>/oauth-token`` stays with the dispatcher and the API
    source.

    Returns ``None`` when ``claude_config_dir`` is empty and the
    environment holds no ``CLAUDE_CODE_OAUTH_TOKEN``: the pexpect spawn
    then inherits the supervisor's process environment unchanged. When
    ``claude_config_dir`` is set, ``CLAUDE_CONFIG_DIR`` is exported so the
    spawned ``claude`` reads that account's files.

    Raises :class:`UsageCaptureSpawnError` when ``claude_config_dir``
    points at a path that does not exist on disk, cannot be checked (a
    parent that denies access) or cannot be expanded (caller
    pre-condition, surfaced here so the spawn site has one consistent
    error type, which the supervisor's poll catches).
    """
    if not claude_config_dir:
        if _TOKEN_ENV not in os.environ:
            return None
        return {key: value for key, value in os.environ.items() if key != _TOKEN_ENV}
    try:
        config_path = existing_config_dir(claude_config_dir)
    except ConfigDirError as exc:
        raise UsageCaptureSpawnError(str(exc)) from exc
    env = {key: value for key, value in os.environ.items() if key != _TOKEN_ENV}
    env["CLAUDE_CONFIG_DIR"] = str(config_path)
    return env


def _showing_prompt(screen: str) -> _Prompt | None:
    """The first of :data:`_PROMPTS` drawn on ``screen``, if any."""
    for prompt in _PROMPTS:
        if prompt.pattern.search(screen):
            return prompt
    return None


def _await_input_box(
    child: pexpect.spawn[bytes],
    screen: LiveScreen,
    clock: Clock,
    timeout_s: float,
    claude_config_dir: str,
    keys_sent: int = 0,
) -> tuple[str, int]:
    """Read ``child`` into ``screen`` until the input box is ready for a command.

    Ready means the footer under the input box is drawn and no prompt from
    :data:`_PROMPTS` is. A prompt gets its keys once ``claude`` has drawn
    nothing new for a poll, so the keys answer the whole frame; the same
    keys go again only after :data:`_RESEND_S` without a change that calls
    for other keys, so a key the TUI dropped is repeated and one it is
    still acting on is not. (2.1.318 can redraw the trust prompt with its
    focus reset in its first seconds, dropping a key sent before.)

    ``keys_sent`` is how many keys this capture has sent to prompts so
    far; :data:`_MAX_ANSWERS` bounds the total over the capture.

    Returns the rendered screen and the new total of keys sent. Raises
    :class:`UsageCaptureLoginRequired` when ``claude`` asks for a login
    method, and :class:`UsageCaptureTimeout` when it exits, when the total
    would pass :data:`_MAX_ANSWERS`, or when it is not ready within
    ``timeout_s`` of ``clock``; the timeout names the prompt still drawn.
    """
    deadline = clock.monotonic() + timeout_s
    last_sent: tuple[_Prompt, bytes] | None = None
    sent_at = 0.0
    answers = keys_sent
    while True:
        try:
            screen.feed(child.read_nonblocking(_READ_CHUNK, timeout=_POLL_S))
            quiet = False
        except pexpect.TIMEOUT:
            quiet = True
        except pexpect.EOF as exc:
            raise UsageCaptureTimeout("claude exited before its input box appeared") from exc
        text = screen.text()
        if _LOGIN_METHOD_RE.search(text):
            raise _login_required("asks which login method to use", claude_config_dir)
        prompt = _showing_prompt(text)
        if prompt is None:
            if _INPUT_BOX_FOOTER_RE.search(text):
                return text, answers
            last_sent = None
        elif quiet:
            keys = prompt.keys_for(text)
            now = clock.monotonic()
            if keys and ((prompt, keys) != last_sent or now - sent_at >= _RESEND_S):
                if answers == _MAX_ANSWERS:
                    raise UsageCaptureTimeout(
                        f"claude still shows {prompt.name} after {answers} answers"
                    )
                child.send(keys)
                last_sent = (prompt, keys)
                sent_at = now
                answers += 1
        if clock.monotonic() >= deadline:
            showing = f"; it still shows {prompt.name}" if prompt is not None else ""
            raise UsageCaptureTimeout(f"TUI did not become ready within {timeout_s}s{showing}")


def _api_billing_label(screen: str) -> str | None:
    """The API-billing label in the welcome banner of ``screen``, if any."""
    banner = "\n".join(screen.splitlines()[:_BANNER_ROWS])
    match = _API_BILLING_RE.search(banner)
    return match.group(1) if match is not None else None


def _login_state(screen: str) -> str | None:
    """How a ready ``screen`` shows that ``claude`` has no plan login, if it does."""
    label = _api_billing_label(screen)
    if label is None:
        return None
    if _NOT_LOGGED_IN_RE.search(screen):
        return f"bills as {label!r} and says it is not logged in"
    return f"bills as {label!r}"


def _login_required(state: str, claude_config_dir: str) -> UsageCaptureLoginRequired:
    """The error for a ``claude`` that has no plan login; ``state`` says how it shows."""
    where = claude_config_dir or "~/.claude"
    command = f"CLAUDE_CONFIG_DIR={claude_config_dir} claude" if claude_config_dir else "claude"
    return UsageCaptureLoginRequired(
        f"claude for config dir {where} {state}, so /usage shows no plan limits: "
        f"{where}/.credentials.json holds no current claude.ai login. Run `{command}` in a "
        f"shell without {_TOKEN_ENV} set and log in with /login (docs/runbook.md, "
        "'A usage capture says claude has no claude.ai login')."
    )


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
        Time source for naming the capture file and for phase 1's waits.
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
    UsageCaptureLoginRequired
        If the banner shows API billing, or ``claude`` asks for a login
        method: it has no claude.ai plan login in that config dir. A
        ``UsageCaptureSpawnError`` subclass.
    UsageCaptureTimeout
        If the input box or both ``Resets`` lines do not appear within
        their configured timeouts.
    """
    if shutil.which(claude_executable) is None:
        raise UsageCaptureSpawnError(f"binary not found in PATH: {claude_executable}")

    captures_dir.mkdir(parents=True, exist_ok=True)
    capture_path = captures_dir / _format_filename(clock.now())

    spawn_env = _build_spawn_env(claude_config_dir)

    # Pre-trust the spawn CWD, mark onboarding complete and close the
    # fullscreen-renderer offer in the target .claude.json. Idempotent — a
    # no-op once the keys are set. Done for
    # the default CLAUDE_CONFIG_DIR too (config_path defaults to ~/.claude
    # inside the helper) so fresh accounts don't hit the trust dialog.
    from claude_task_runner.claude_init import ensure_initialized as _ensure_claude_init

    _ensure_claude_init(claude_config_dir or None, Path.cwd())

    log_buf = io.BytesIO()
    screen = LiveScreen(columns=_PTY_COLUMNS, rows=_PTY_ROWS)
    child: pexpect.spawn[bytes] | None = None
    try:
        # encoding=None -> raw bytes mode. We strip ANSI in the parser.
        child = pexpect.spawn(
            claude_executable,
            timeout=settings.capture_trust_timeout_s,
            encoding=None,
            env=spawn_env,
            dimensions=(_PTY_ROWS, _PTY_COLUMNS),
        )
        child.logfile_read = log_buf

        # Phase 1: wait for the input box, answering prompts on the way.
        # The footer appears before the input field accepts commands, and a
        # dialog can open in the pad after it, so the wait runs again: no
        # key may reach a dialog. The banner is read from the later screen,
        # by which time a "Not logged in" notice has been drawn too.
        _, keys_sent = _await_input_box(
            child, screen, clock, settings.capture_trust_timeout_s, claude_config_dir
        )
        _sleep_ms(settings.capture_post_ready_pad_ms)
        ready_screen, _ = _await_input_box(
            child, screen, clock, settings.capture_trust_timeout_s, claude_config_dir, keys_sent
        )
        state = _login_state(ready_screen)
        if state is not None:
            raise _login_required(state, claude_config_dir)

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

    except BaseException:
        # Keep what claude drew for the post-mortem; a failed write must
        # not replace the capture's own error.
        if child is not None:
            with contextlib.suppress(OSError):
                capture_path.write_bytes(log_buf.getvalue())
                _rotate_captures(captures_dir, settings.capture_rotation_count)
        raise
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
    for label, candidate in candidates:
        try:
            blocks = _parser._extract_blocks(_render.render(candidate))
        except Exception:
            # A candidate that can't be rendered/parsed is skipped in
            # favour of the others; log which one (and why) so a total
            # parse failure leaves a forensic trail in the .cap file's
            # vicinity instead of vanishing silently.
            logger.debug(
                "usage capture: snapshot %r (%d bytes, %s) failed to parse; skipping",
                label,
                len(candidate),
                capture_path.name,
                exc_info=True,
            )
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
