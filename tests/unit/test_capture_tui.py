"""Tests for the TTY usage capture's dealings with the ``claude`` TUI.

``capture()`` and its phase 1 (``_await_input_box``) drive a stand-in for
the pexpect child that replays recorded start-up screens
(``tests/fixtures/usage_startup/``) and records every key sent. The pinned
behaviour:

* the input box is found in every permission mode, from the rendered
  screen, not from a byte marker the TUI paints in fragments;
* the fullscreen-renderer offer is closed with Esc, never Enter, which
  would opt in; the trust prompt gets "Yes, I trust this folder", never
  the focused "No, exit";
* a dropped key is sent again after the resend interval, never sooner;
* a banner that names API billing, or a login-method screen, stops the
  capture before ``/usage``, at once, with ``UsageCaptureLoginRequired``.
"""

from __future__ import annotations

import io
import sys
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pexpect
import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.config.schema import UsageSettings
from claude_task_runner.usage import capture as capture_mod
from claude_task_runner.usage.capture import (
    _api_billing_label,
    _await_input_box,
    _login_required,
    _login_state,
)
from claude_task_runner.usage.drift import (
    UsageApiAuthExpired,
    UsageCaptureLoginRequired,
    UsageCaptureTimeout,
    usage_error_kind,
)
from claude_task_runner.usage.parser import parse
from claude_task_runner.usage.render import LiveScreen, render

FIXTURES = Path(__file__).parent.parent / "fixtures" / "usage_startup"
USAGE_FIXTURE = Path(__file__).parent.parent / "fixtures" / "usage"
ESC = b"\x1b"
ENTER = b"\r"
DOWN = b"\x1b[B"
CLEAR = b"\x1b[2J\x1b[H"


def _fixture(label: str) -> bytes:
    return (FIXTURES / f"real_2026-11-05_{label}.cap").read_bytes()


def _subscribed() -> bytes:
    """The auto-mode start-up with a plan's banner: ``Claude API`` and
    ``Claude Max`` have the same length, so every cursor move still fits."""
    raw = _fixture("auto_mode_claude_api")
    assert raw.count(b"Claude API") == 1
    return raw.replace(b"Claude API", b"Claude Max")


class FakeChild:
    """A pexpect child that replays scripted output and records input.

    ``output`` is what ``claude`` draws at start; ``script`` pairs each key
    the capture is expected to send, in order, with the chunks ``claude``
    draws in reply. A key off the script gets no reply. Each read returns
    one chunk. An empty read advances ``clock`` by the read's timeout, as
    a real wait would.
    """

    def __init__(
        self,
        clock: FakeClock,
        output: list[bytes],
        script: list[tuple[bytes, list[bytes]]] | None = None,
        *,
        exits: bool = False,
    ) -> None:
        self.clock = clock
        self._chunks: deque[bytes] = deque(output)
        self._script = deque(script or [])
        self._alive = not exits
        self.sent: list[bytes] = []
        self.sent_at: list[float] = []
        self.logfile_read: io.BytesIO | None = None

    def _deliver(self, data: bytes) -> bytes:
        if self.logfile_read is not None:
            self.logfile_read.write(data)
        return data

    def read_nonblocking(self, size: int, timeout: float) -> bytes:
        if self._chunks:
            chunk = self._chunks.popleft()
            if len(chunk) > size:
                self._chunks.appendleft(chunk[size:])
                chunk = chunk[:size]
            return self._deliver(chunk)
        if not self._alive:
            raise pexpect.EOF("claude exited")
        self.clock.advance(timeout)
        raise pexpect.TIMEOUT("no output")

    def draw(self, data: bytes) -> None:
        """Have ``claude`` draw ``data`` next, as it would during a wait."""
        self._chunks.append(data)

    def add_replies(self, script: list[tuple[bytes, list[bytes]]]) -> None:
        """Extend the script of keys and replies."""
        self._script.extend(script)

    def send(self, data: bytes) -> int:
        self.sent.append(data)
        self.sent_at.append(self.clock.monotonic())
        if self._script and self._script[0][0] == data:
            self._chunks.extend(self._script.popleft()[1])
        if data == b"/exit\r":
            self._alive = False
        return len(data)

    def expect(self, pattern: Any, timeout: float = -1) -> int:
        patterns = pattern if isinstance(pattern, list) else [pattern]
        pending = b"".join(self._chunks)
        for index, wanted in enumerate(patterns):
            if isinstance(wanted, bytes) and wanted in pending:
                end = pending.index(wanted) + len(wanted)
                self._chunks = deque([pending[end:]] if pending[end:] else [])
                self._deliver(pending[:end])
                return index
        self._chunks.clear()
        self._deliver(pending)
        if not self._alive and pexpect.EOF in patterns:
            return patterns.index(pexpect.EOF)
        if pexpect.TIMEOUT in patterns:
            self.clock.advance(max(timeout, 0))
            return patterns.index(pexpect.TIMEOUT)
        if not self._alive:
            raise pexpect.EOF("claude exited")
        raise pexpect.TIMEOUT("no match")

    def isalive(self) -> bool:
        return self._alive

    def terminate(self, force: bool = False) -> bool:
        self._alive = False
        return True


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 11, 5, 17, 0, 0, tzinfo=UTC))


def _screen() -> LiveScreen:
    return LiveScreen(columns=capture_mod._PTY_COLUMNS, rows=capture_mod._PTY_ROWS)


def _await(child: FakeChild, clock: FakeClock, timeout_s: float = 10.0) -> str:
    screen, _ = _await_input_box(child, _screen(), clock, timeout_s, "")  # type: ignore[arg-type]
    return screen


# ---------------------------------------------------------------------------
# Phase 1: the input box, in every permission mode.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "footer"),
    [
        ("manual_mode", "⏸ manual mode on · ? for shortcuts · ← for agents"),
        ("accept_edits_mode", "⏵⏵ accept edits on (shift+tab to cycle)"),
        ("plan_mode", "⏸ plan mode on (shift+tab to cycle)"),
        ("auto_mode_claude_api", "⏵⏵ auto mode on (shift+tab to cycle) · ← for agents"),
        ("auto_mode_not_logged_in", "⏵⏵ auto mode on (shift+tab to cycle) · ← for agents"),
    ],
)
def test_input_box_is_found_in_every_permission_mode(
    clock: FakeClock, label: str, footer: str
) -> None:
    child = FakeChild(clock, [_fixture(label)])

    screen = _await(child, clock)

    assert footer in screen
    assert child.sent == []
    # Found on the first read: no poll waited.
    assert clock.monotonic() == 0.0


@pytest.mark.parametrize("label", ["accept_edits_mode", "plan_mode", "auto_mode_claude_api"])
def test_non_manual_modes_show_no_shortcuts_footer(label: str) -> None:
    """The ``shortcuts`` marker the capture used to wait for is not on the
    screen outside manual mode."""
    assert "for shortcuts" not in render(_fixture(label), columns=80, rows=24)


def test_auto_mode_bytes_never_hold_the_old_marker() -> None:
    """Auto mode, the 2.1.318 default, never sends ``shortcuts`` at all."""
    assert b"shortcuts" not in _fixture("auto_mode_claude_api")


def test_screen_without_a_footer_times_out_under_the_fake_clock(clock: FakeClock) -> None:
    child = FakeChild(clock, [b"Loading\r\n"])

    with pytest.raises(UsageCaptureTimeout, match=r"did not become ready within 10\.0s$"):
        _await(child, clock)

    assert child.sent == []
    assert clock.monotonic() == pytest.approx(10.0)


def test_exit_before_the_input_box_is_a_capture_timeout(clock: FakeClock) -> None:
    child = FakeChild(clock, [b"starting\r\n"], exits=True)

    with pytest.raises(UsageCaptureTimeout, match="exited before its input box appeared"):
        _await(child, clock)


def test_a_split_utf8_character_renders_once_both_halves_arrive(clock: FakeClock) -> None:
    raw = _fixture("manual_mode")
    cut = raw.index("⏸".encode()) + 1
    child = FakeChild(clock, [raw[:cut], raw[cut:]])

    screen = _await(child, clock)

    assert "⏸ manual mode on" in screen


# ---------------------------------------------------------------------------
# Phase 1: prompts and dialogs.
# ---------------------------------------------------------------------------


def test_fullscreen_offer_focuses_yes_so_enter_would_opt_in() -> None:
    screen = render(_fixture("fullscreen_upsell"), columns=80, rows=24)
    assert "Try the new fullscreen renderer?" in screen
    assert "\u276f 1. Yes, try it" in screen  # the focus marker
    assert "Enter to confirm · Esc to cancel" in screen


def test_fullscreen_offer_is_closed_with_esc_and_never_accepted(clock: FakeClock) -> None:
    child = FakeChild(
        clock,
        [_fixture("fullscreen_upsell")],
        [(ESC, [_fixture("fullscreen_upsell_after_esc")])],
    )

    screen = _await(child, clock)

    assert child.sent == [ESC]
    assert "fullscreen renderer" not in screen
    assert "⏵⏵ auto mode on (shift+tab to cycle)" in screen


def test_offer_that_ignores_esc_gets_it_every_resend_interval(clock: FakeClock) -> None:
    child = FakeChild(clock, [_fixture("fullscreen_upsell")])

    with pytest.raises(
        UsageCaptureTimeout,
        match=r"within 10\.0s; it still shows the fullscreen-renderer offer$",
    ):
        _await(child, clock)

    # First after one quiet poll, then once per resend interval.
    assert child.sent == [ESC] * 5
    assert child.sent_at == [0.25, 2.25, 4.25, 6.25, 8.25]


def test_offer_that_keeps_coming_back_stops_at_the_bound(clock: FakeClock) -> None:
    upsell = _fixture("fullscreen_upsell")
    child = FakeChild(clock, [upsell], [(ESC, [CLEAR, upsell])] * 9)

    with pytest.raises(
        UsageCaptureTimeout,
        match="still shows the fullscreen-renderer offer after 8 answers",
    ):
        _await(child, clock)

    # Each return is answered after one quiet poll, not a resend interval:
    # the screen showed no prompt in between.
    assert child.sent == [ESC] * 8
    assert child.sent_at == [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]


def test_keys_sent_count_toward_one_bound_per_capture(clock: FakeClock) -> None:
    """The total carries over from the first run of phase 1 to the second."""
    upsell = _fixture("fullscreen_upsell")
    child = FakeChild(clock, [upsell], [(ESC, [CLEAR, upsell])] * 3)

    with pytest.raises(
        UsageCaptureTimeout,
        match="still shows the fullscreen-renderer offer after 8 answers",
    ):
        _await_input_box(child, _screen(), clock, 10.0, "", 6)  # type: ignore[arg-type]

    assert child.sent == [ESC] * 2


def test_the_total_of_keys_sent_is_returned(clock: FakeClock) -> None:
    child = FakeChild(
        clock,
        [_fixture("fullscreen_upsell")],
        [(ESC, [_fixture("fullscreen_upsell_after_esc")])],
    )

    _, keys_sent = _await_input_box(child, _screen(), clock, 10.0, "", 3)  # type: ignore[arg-type]

    assert keys_sent == 4


def test_capture_bounds_keys_over_both_runs_of_phase_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: FakeClock
) -> None:
    """Prompts before the pad and after it share one bound of 8 keys."""
    upsell = _fixture("fullscreen_upsell")
    after = _fixture("fullscreen_upsell_after_esc")
    # Before the pad: the offer comes back 4 times, then the input box.
    child = FakeChild(clock, [upsell], [(ESC, [CLEAR, upsell])] * 4 + [(ESC, [after])])

    def pad(milliseconds: int) -> None:
        # In the pad: the offer again, and it keeps coming back.
        child.draw(CLEAR + upsell)
        child.add_replies([(ESC, [CLEAR, upsell])] * 8)

    monkeypatch.setattr(capture_mod, "_sleep_ms", pad)

    with pytest.raises(UsageCaptureTimeout, match="after 8 answers"):
        _run_capture(monkeypatch, tmp_path, clock, child, {})

    assert child.sent == [ESC] * 8


def test_trust_prompt_is_answered_yes_never_the_focused_no(clock: FakeClock) -> None:
    """2.1.318 focuses "No, exit"; Enter or Esc there would end claude."""
    assert "\u276f No, exit" in render(_fixture("trust_prompt"), columns=80, rows=24)
    child = FakeChild(
        clock,
        [_fixture("trust_prompt")],
        [
            (DOWN, [_fixture("trust_prompt_after_down")]),
            (ENTER, [_fixture("trust_prompt_after_enter")]),
        ],
    )

    screen = _await(child, clock)

    assert child.sent == [DOWN, ENTER]
    assert "⏵⏵ auto mode on (shift+tab to cycle)" in screen


def test_a_dropped_key_is_sent_again_after_the_resend_interval(clock: FakeClock) -> None:
    child = FakeChild(
        clock,
        [_fixture("trust_prompt")],
        [
            (DOWN, []),
            (DOWN, [_fixture("trust_prompt_after_down")]),
            (ENTER, [_fixture("trust_prompt_after_enter")]),
        ],
    )

    _await(child, clock)

    assert child.sent == [DOWN, DOWN, ENTER]
    # The repeat waited out the interval; the next, different key only for
    # the quiet poll after the redraw.
    assert child.sent_at == [0.25, 2.25, 2.5]


def test_theme_picker_keeps_the_highlighted_theme(clock: FakeClock) -> None:
    """Enter only: the picker has no numbers, and a stray key would reach
    the next first-run screen."""
    child = FakeChild(
        clock,
        [_fixture("theme_picker")],
        [(ENTER, [_fixture("theme_picker_after_enter")])],
    )

    with pytest.raises(UsageCaptureLoginRequired, match="asks which login method to use, so"):
        _await(child, clock)

    assert child.sent == [ENTER]


def test_any_other_confirm_dialog_is_cancelled_with_esc(clock: FakeClock) -> None:
    dialog = b"Teach auto mode about your environment?\r\n\xe2\x9d\xaf 1. Yes\r\nEnter to confirm \xc2\xb7 Esc to cancel\r\n"
    child = FakeChild(clock, [dialog], [(ESC, [CLEAR, _fixture("plan_mode")])])

    _await(child, clock)

    assert child.sent == [ESC]


def test_dialog_over_a_drawn_footer_is_answered_before_ready(clock: FakeClock) -> None:
    """The footer alone is not ready: keys typed then would reach the dialog."""
    raw = _fixture("manual_mode") + b"\x1b[10;1HEnter to confirm \xc2\xb7 Esc to cancel"
    child = FakeChild(clock, [raw], [(ESC, [b"\x1b[10;1H\x1b[2K"])])

    screen = _await(child, clock)

    assert child.sent == [ESC]
    assert "? for shortcuts" in screen


# ---------------------------------------------------------------------------
# The banner's billing label.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "billing"),
    [
        ("auto_mode_claude_api", "Claude API"),
        ("auto_mode_not_logged_in", "API Usage Billing"),
        # The label's words arrive in fragments here; the screen has them whole.
        ("manual_mode", "API Usage Billing"),
    ],
)
def test_api_billing_label_is_read_from_the_banner(label: str, billing: str) -> None:
    assert _api_billing_label(render(_fixture(label), columns=80, rows=24)) == billing


@pytest.mark.parametrize(
    ("label", "state"),
    [
        ("auto_mode_claude_api", "bills as 'Claude API'"),
        ("auto_mode_not_logged_in", "bills as 'API Usage Billing' and says it is not logged in"),
    ],
)
def test_login_state_of_a_ready_screen(label: str, state: str) -> None:
    assert _login_state(render(_fixture(label), columns=80, rows=24)) == state


def test_a_plan_banner_has_no_api_billing_label() -> None:
    screen = render(_subscribed(), columns=80, rows=24)
    assert "Sonnet 5.5 · Claude Max" in screen
    assert _api_billing_label(screen) is None
    assert _login_state(screen) is None


def test_label_below_the_banner_rows_is_ignored() -> None:
    screen = "\n".join(["Claude Code v2.1.318", "Opus 5.5 · Claude Max"] + [""] * 4)
    screen += "\nOpus 5.5 · Claude API"
    assert _api_billing_label(screen) is None


@pytest.mark.parametrize(
    "tip",
    [
        "  Tip: tools you build can call the Claude API",
        "  Docs · Claude API",
        "  Billing for this org: API Usage Billing",
    ],
)
def test_a_line_ending_in_a_billing_phrase_is_not_the_banner(tip: str) -> None:
    """Only the row under ``Claude Code v...`` is the banner's billing row."""
    raw = _subscribed() + f"\x1b[4;1H\x1b[2K{tip}".encode()
    screen = render(raw, columns=80, rows=24)
    assert screen.splitlines()[3].rstrip() == tip
    assert _api_billing_label(screen) is None
    assert _login_state(screen) is None


def test_plan_capture_with_a_tip_ending_in_claude_api_reads_usage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: FakeClock
) -> None:
    """A plan account must not be told to log in again because of a tip."""
    usage = (USAGE_FIXTURE / "real_2026-05-03_three_section.cap").read_bytes()
    tip = b"\x1b[4;1H\x1b[2K  Tip: tools you build can call the Claude API"
    child = FakeChild(clock, [_subscribed() + tip], [(b"/usage\r", [usage])])

    raw, _cap_path = _run_capture(monkeypatch, tmp_path, clock, child, {})

    assert child.sent == [b"/usage\r", ESC, b"/exit\r"]
    assert parse(raw, clock.now(), clock).seven_day.utilization_pct == 100


def test_login_required_message_for_the_default_config_dir() -> None:
    exc = _login_required("bills as 'Claude API'", "")
    assert str(exc) == (
        "claude for config dir ~/.claude bills as 'Claude API', so /usage shows no plan "
        "limits: ~/.claude/.credentials.json holds no current claude.ai login. Run `claude` "
        "in a shell without CLAUDE_CODE_OAUTH_TOKEN set and log in with /login "
        "(docs/runbook.md, 'A usage capture says claude has no claude.ai login')."
    )


# ---------------------------------------------------------------------------
# capture() end to end, with the fake child in place of pexpect.spawn.
# ---------------------------------------------------------------------------


def _settings() -> UsageSettings:
    return load_settings(None).usage.model_copy(
        update={
            "capture_trust_timeout_s": 10.0,
            "capture_usage_timeout_s": 15.0,
            "capture_post_ready_pad_ms": 0,
            "capture_post_data_pad_ms": 1,
        }
    )


def _run_capture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    clock: FakeClock,
    child: FakeChild,
    spawned: dict[str, Any],
    *,
    settings: UsageSettings | None = None,
) -> tuple[bytes, Path]:
    """Run ``capture`` against ``child`` for an account that has an
    ``oauth-token`` file, in an environment that holds a token too.

    ``spawned`` receives the keyword arguments ``pexpect.spawn`` got.
    """
    config_dir = tmp_path / "account"
    config_dir.mkdir()
    (config_dir / "oauth-token").write_text("sk-ant-oat01-LONG\n", encoding="utf-8")

    def fake_spawn(command: str, **kwargs: Any) -> FakeChild:
        spawned.update(kwargs)
        return child

    monkeypatch.setattr(capture_mod.pexpect, "spawn", fake_spawn)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "inherited-value")
    return capture_mod.capture(
        settings or _settings(),
        clock,
        captures_dir=tmp_path / "caps",
        claude_executable=sys.executable,
        claude_config_dir=str(config_dir),
    )


def test_capture_stops_at_a_claude_api_banner_before_usage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: FakeClock
) -> None:
    raw = _fixture("auto_mode_claude_api")
    child = FakeChild(clock, [raw])
    spawned: dict[str, Any] = {}

    with pytest.raises(UsageCaptureLoginRequired) as info:
        _run_capture(monkeypatch, tmp_path, clock, child, spawned)

    config_dir = tmp_path / "account"
    message = str(info.value)
    assert message == (
        f"claude for config dir {config_dir} bills as 'Claude API', so /usage shows no plan "
        f"limits: {config_dir}/.credentials.json holds no current claude.ai login. Run "
        f"`CLAUDE_CONFIG_DIR={config_dir} claude` in a shell without CLAUDE_CODE_OAUTH_TOKEN "
        "set and log in with /login (docs/runbook.md, 'A usage capture says claude has no "
        "claude.ai login')."
    )
    # Routed as a login failure: ErrorDrift with an auth message.
    assert isinstance(info.value, UsageApiAuthExpired)
    assert usage_error_kind(message) == "auth"
    # At once: one empty poll after the pad, and nothing was typed.
    assert clock.monotonic() == 0.25
    assert child.sent == []
    # The spawn used the PTY size phase 1 renders at, and no token.
    assert spawned["dimensions"] == (24, 80)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in spawned["env"]
    # What claude drew is kept for the post-mortem.
    caps = sorted((tmp_path / "caps").glob("*.cap"))
    assert [p.read_bytes() for p in caps] == [raw]


def test_capture_names_a_logged_out_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: FakeClock
) -> None:
    child = FakeChild(clock, [_fixture("auto_mode_not_logged_in")])

    with pytest.raises(
        UsageCaptureLoginRequired,
        match=r"bills as 'API Usage Billing' and says it is not logged in, so",
    ):
        _run_capture(monkeypatch, tmp_path, clock, child, {})

    assert child.sent == []


def test_capture_reads_the_banner_after_the_pad(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: FakeClock
) -> None:
    """The notice ``Not logged in`` is drawn after the footer; the login
    check runs on the screen after the post-ready pad, which has it."""
    raw = _fixture("auto_mode_not_logged_in")
    first, later = raw[:1115], raw[1115:]
    first_screen = render(first, columns=80, rows=24)
    assert "auto mode on (shift+tab to cycle)" in first_screen
    assert "Not logged in" not in first_screen
    child = FakeChild(clock, [first])
    pads: list[int] = []

    def pad(milliseconds: int) -> None:
        pads.append(milliseconds)
        child.draw(later)

    monkeypatch.setattr(capture_mod, "_sleep_ms", pad)
    settings = _settings().model_copy(update={"capture_post_ready_pad_ms": 1500})

    with pytest.raises(UsageCaptureLoginRequired, match="and says it is not logged in, so"):
        _run_capture(monkeypatch, tmp_path, clock, child, {}, settings=settings)

    assert pads == [1500]
    assert child.sent == []


def test_capture_closes_the_fullscreen_offer_then_checks_the_banner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: FakeClock
) -> None:
    child = FakeChild(
        clock,
        [_fixture("fullscreen_upsell")],
        [(ESC, [_fixture("fullscreen_upsell_after_esc")])],
    )

    with pytest.raises(UsageCaptureLoginRequired, match="'API Usage Billing'"):
        _run_capture(monkeypatch, tmp_path, clock, child, {})

    assert child.sent == [ESC]


def test_capture_with_a_plan_banner_reads_usage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clock: FakeClock
) -> None:
    """Known answer: a plan's banner, then the 2026-05-03 /usage panel."""
    usage = (USAGE_FIXTURE / "real_2026-05-03_three_section.cap").read_bytes()
    child = FakeChild(clock, [_subscribed()], [(b"/usage\r", [usage])])

    spawned: dict[str, Any] = {}
    raw, cap_path = _run_capture(monkeypatch, tmp_path, clock, child, spawned)

    assert child.sent == [b"/usage\r", ESC, b"/exit\r"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in spawned["env"]
    assert spawned["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / "account")
    assert cap_path.read_bytes() == _subscribed() + usage
    reading = parse(raw, clock.now(), clock)
    assert reading.five_hour.utilization_pct == 3
    assert reading.five_hour.resets_at_raw == "10:59pm (UTC)"
    assert reading.seven_day.utilization_pct == 100
    assert reading.seven_day.resets_at_raw == "3am (UTC)"
