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
2. **Submit /usage and wait for the complete panel** — the stream is fed
   into a second virtual terminal, the size the parser renders at. The
   panel opens with ``Loading usage data…`` or with older numbers under
   ``Refreshing…``, and draws the numbers of its own fetch, in as many
   sections as the plan has, once that fetch lands. It is complete when,
   on a screen ``claude`` drew nothing new on for a poll, its closing line
   reads ``Esc to cancel`` alone and nothing is in flight
   (:func:`parser.panel_pending`); a closing line that offers a retry
   means the fetch failed. The stream then drains for
   ``capture_post_data_pad_ms`` (ADR-0040).

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
from claude_task_runner.usage import parser as parser_mod
from claude_task_runner.usage import render as render_mod
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

_INPUT_ROW_RE = re.compile(r"─{20,}[^\n]*\n[^\S\n]*\u276f")
"""The input box itself: a row that starts with the prompt marker U+276F
right under a rule. The recorded dialogs (trust, theme, login method,
fullscreen offer) take its place, so a dialog this module does not know
stops phase 1 short of typing ``/usage`` into it, even with the footer
still drawn."""

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

    Ready means the input box and its footer are drawn and no prompt from
    :data:`_PROMPTS` is. A prompt gets its keys once ``claude`` has drawn
    nothing new for a poll, so the keys answer the whole frame; Esc or
    Down goes again only after :data:`_RESEND_S` without a change that
    calls for other keys, so a key the TUI dropped is repeated and one it
    is still acting on is not, and Enter never goes twice in a row.
    (2.1.318 can redraw the trust prompt with its focus reset in its first
    seconds, dropping a key sent before.)

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
            if _INPUT_BOX_FOOTER_RE.search(text) and _INPUT_ROW_RE.search(text):
                return text, answers
            last_sent = None
        elif quiet:
            keys = prompt.keys_for(text)
            now = clock.monotonic()
            # Enter is never repeated: on a TUI that acted on it but has not
            # redrawn yet, a second one would land on the next screen, where
            # it could accept the fullscreen offer.
            repeat = keys != _ENTER and now - sent_at >= _RESEND_S
            if keys and ((prompt, keys) != last_sent or repeat):
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


def _await_usage_panel(
    child: pexpect.spawn[bytes],
    screen: LiveScreen,
    clock: Clock,
    timeout_s: float,
) -> None:
    """Read ``child`` into ``screen`` until the ``/usage`` panel is complete.

    The panel is judged only on a screen ``claude`` drew nothing new on for
    a poll, never halfway through a frame, and is complete when
    :func:`parser.panel_pending` finds nothing left to draw. ``screen``
    renders at the parser's size, so the panel judged complete is the one
    the parser reads.

    Raises :class:`UsageCaptureTimeout` when ``claude`` exits, when the
    panel says its fetch failed (:func:`parser.panel_refresh_failure`),
    and when it is not complete within ``timeout_s`` of ``clock``; the
    timeout says what the panel still lacked.
    """
    deadline = clock.monotonic() + timeout_s
    while True:
        try:
            screen.feed(child.read_nonblocking(_READ_CHUNK, timeout=_POLL_S))
        except pexpect.TIMEOUT:
            text = screen.text()
            failure = parser_mod.panel_refresh_failure(text)
            if failure is not None:
                raise UsageCaptureTimeout(
                    f"the /usage panel could not refresh its numbers: {failure!r}"
                ) from None
            if parser_mod.panel_pending(text) is None:
                return
        except pexpect.EOF as exc:
            raise UsageCaptureTimeout("claude exited before the /usage panel was complete") from exc
        if clock.monotonic() >= deadline:
            pending = parser_mod.panel_pending(screen.text()) or "claude kept drawing"
            raise UsageCaptureTimeout(
                f"the /usage panel was not complete within {timeout_s}s: {pending}"
            )


def _parse_target(at_complete: bytes, after_pad: bytes) -> bytes:
    """The stream the parser reads: ``after_pad`` or ``at_complete``.

    ``at_complete`` is the stream up to the screen phase 2 judged complete,
    ``after_pad`` the stream after the pad that followed. The later one is
    read when its panel is still complete and still shows every section the
    earlier one did, so numbers redrawn during the pad win. Otherwise, as
    when the panel scrolled away, moved to another view or failed a fetch
    during the pad, the screen judged complete is read.
    """
    if after_pad == at_complete:
        return at_complete
    later = render_mod.render(after_pad)
    if (
        parser_mod.panel_refresh_failure(later) is not None
        or parser_mod.panel_pending(later) is not None
    ):
        return at_complete
    sections = {
        block.header for block in parser_mod._extract_blocks(render_mod.render(at_complete))
    }
    later_sections = {block.header for block in parser_mod._extract_blocks(later)}
    return after_pad if sections <= later_sections else at_complete


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
) -> tuple[bytes, Path | None]:
    """Spawn ``claude``, send ``/usage``, and return the raw PTY bytes.

    The bytes returned run from the spawn to the complete ``/usage`` panel,
    or to the end of the pad after it (:func:`_parse_target`); they stop
    before the cleanup closes the panel. The whole stream, cleanup
    included, is persisted to ``captures_dir/<ISO>.cap`` for forensics.
    Returns ``(raw_bytes, capture_path)``; ``capture_path`` is ``None``
    when the ``.cap`` could not be written, which is logged as a warning
    and does not cost the reading.

    Parameters
    ----------
    settings
        Pre-validated usage section of the config.
    clock
        Time source for naming the capture file and for the waits of
        phases 1 and 2.
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
        If the ``claude`` binary cannot be located or launched, or
        ``captures_dir`` cannot be created.
    UsageCaptureLoginRequired
        If the banner shows API billing, or ``claude`` asks for a login
        method: it has no claude.ai plan login in that config dir. A
        ``UsageApiAuthExpired`` subclass.
    UsageCaptureTimeout
        If the input box does not appear, or the ``/usage`` panel is not
        complete, within its configured timeout, or if the panel says its
        fetch failed.
    """
    if shutil.which(claude_executable) is None:
        raise UsageCaptureSpawnError(f"binary not found in PATH: {claude_executable}")

    try:
        captures_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UsageCaptureSpawnError(
            f"cannot create the usage captures directory {captures_dir}: {exc}"
        ) from exc
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
        # pexpect raises OSError when no pty can be opened and
        # ExceptionPexpect when the binary cannot be executed.
        try:
            child = pexpect.spawn(
                claude_executable,
                timeout=settings.capture_trust_timeout_s,
                encoding=None,
                env=spawn_env,
                dimensions=(_PTY_ROWS, _PTY_COLUMNS),
            )
        except (OSError, pexpect.ExceptionPexpect) as exc:
            raise UsageCaptureSpawnError(f"cannot start {claude_executable}: {exc}") from exc
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

        # Phase 2: send /usage and wait until the panel is complete. The
        # second screen renders the whole stream at the parser's size, so
        # the panel it judges is the one the parser reads.
        panel = LiveScreen(columns=render_mod.DEFAULT_COLUMNS, rows=render_mod.DEFAULT_ROWS)
        panel.feed(log_buf.getvalue())
        child.send(b"/usage\r")
        _await_usage_panel(child, panel, clock, settings.capture_usage_timeout_s)
        at_complete = log_buf.getvalue()

        # The pad: a redraw of the numbers that lands in it is what the
        # parser reads (_parse_target). An exit during it keeps what was drawn.
        if settings.capture_post_data_pad_ms > 0:
            with contextlib.suppress(pexpect.EOF):
                child.expect(
                    pexpect.TIMEOUT,
                    timeout=settings.capture_post_data_pad_ms / 1000.0,
                )
        after_pad = log_buf.getvalue()

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
    written_path: Path | None = capture_path
    try:
        capture_path.write_bytes(raw_full)
    except OSError as exc:
        written_path = None
        logger.warning("usage capture: cannot write the forensics file %s: %s", capture_path, exc)
    _rotate_captures(captures_dir, settings.capture_rotation_count)

    # Esc closes the panel, so the stream after cleanup is never the one parsed.
    return _parse_target(at_complete, after_pad), written_path


def _sleep_ms(milliseconds: int) -> None:
    """Sleep helper isolated for monkey-patching in tests."""
    if milliseconds <= 0:
        return
    import time as _time  # local import keeps test patching tidy

    _time.sleep(milliseconds / 1000.0)
