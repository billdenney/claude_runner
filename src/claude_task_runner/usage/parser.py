"""Parse the output of ``claude /usage`` into a validated :class:`UsageReading`.

Pipeline:

1. :func:`render.render` — feed raw PTY bytes through a :mod:`pyte`
   virtual terminal so cursor-position overwrites resolve to the final
   on-screen text. The TUI shows a placeholder panel while it queries
   the OAuth API and overwrites it in-place once data lands; pyte gives
   us the post-overwrite state.
2. :func:`_extract_blocks` — walk the rendered text, pairing each
   ``"Current ..."`` section header (or implicit position) with its
   ``"NN% used"`` and ``"Resets …"`` lines.
3. Classification — match the configured five-hour and weekly section
   headers ("Current session", "Current week (all models)") to the
   blocks. Additional sections like ``"Current week (Sonnet only)"``
   are exposed via :attr:`UsageReading.extra_windows`, which the ``usage``
   command prints.
4. Validation — percentages in [0, 100], the weekly window present, and
   the 5-hour window present unless the panel shows that none applies
   (:func:`_no_five_hour_section_doubts`, ADR-0040).

The parser is pure: it raises :class:`UsageFormatDrift` on any
structural deviation (a missing weekly window, a missing 5-hour window
the panel does not account for, percentages out of range). The capture
layer waits until the panel is complete (:func:`panel_pending`) before
it hands the bytes over.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from claude_task_runner.clock import Clock
from claude_task_runner.usage import render as render_mod
from claude_task_runner.usage.drift import UsageFormatDrift
from claude_task_runner.usage.models import (
    ExtraWindow,
    UsageReading,
    WindowReading,
)
from claude_task_runner.usage.reset_times import parse_five_hour, parse_weekly

# "NN% used" — negative lookbehind to reject "-1% used".
_PCT_USED_RE = re.compile(r"(?<![\d.-])(\d{1,3})\s*%\s*used", re.IGNORECASE)
# "Resets <text>" — captures everything after "Resets" on the same line.
# `\s*` instead of `\s+` for the same pyte-collapse reason as the headers
# below: live captures from Claude >= 2.1.141 sometimes produce
# "Resets10:10pm(UTC)" with no whitespace.
_RESETS_RE = re.compile(r"^\s*resets\s*(.+?)\s*$", re.IGNORECASE)
# Section header patterns observed in the live TUI.
#
# IMPORTANT: pyte (the virtual terminal used by render.render) sometimes
# collapses adjacent ANSI-cursor-positioned tokens into one contiguous
# word — e.g. "Current session" renders as "Currentsession", and
# "Current week (all models)" renders as "Currentweek(allmodels)". Both
# shapes appear in the same dispatch run depending on terminal width
# and cursor moves. So we use `\s*` (not `\s+`) between every word; the
# `^\s*...\s*$` anchors still keep us from over-matching mid-line text.
_HEADER_5H_RE = re.compile(
    r"^\s*(?:current\s*session|5-?hour\s*session|5-?h\s*session)\s*$",
    re.IGNORECASE,
)
_HEADER_WEEKLY_ALL_RE = re.compile(
    r"^\s*(?:current\s*week\s*\(\s*all\s*models\s*\)|7-?day\s*weekly)\s*$",
    re.IGNORECASE,
)
_HEADER_WEEKLY_OTHER_RE = re.compile(
    r"^\s*current\s*week\s*\(([^)]+)\)\s*$",
    re.IGNORECASE,
)
_HEADER_CREDIT_RE = re.compile(r"credits?$", re.IGNORECASE)
"""The end of a credit section's heading. Such a section can draw an
``NN% used`` gauge but is no usage window: ``Usage credits`` with a
monthly limit, ``Claude Code and Cowork credit``, ``Credit``."""

_PANEL_FOOTER_RE = re.compile(
    r"^[^\S\n]*(?:(?P<keys>\S[^\n]*?)[^\S\n]*·[^\S\n]*)?Esc[^\S\n]*to[^\S\n]*cancel[^\S\n]*$",
    re.MULTILINE,
)
"""The closing line of the ``/usage`` panel: ``Esc to cancel`` alone, or
``r to retry · Esc to cancel`` once its refresh has failed. Other dialogs
end a line the same way after other keys (``Enter to confirm · Esc to
cancel``); ``keys`` holds whatever comes before the last ``·``."""

_RETRY_RE = re.compile(r"to\s*retry", re.IGNORECASE)

_PANEL_IN_FLIGHT_RE = re.compile(
    r"^[^\S\n]*(?:Refreshing|Loading[^\S\n]*usage[^\S\n]*data)(?:…|\.\.\.)[^\S\n]*$",
    re.MULTILINE,
)
"""The line the panel draws above its closing line while it fetches,
ending in an ellipsis or three dots: ``Loading usage data…`` before it has
numbers to show, ``Refreshing…`` while the numbers on screen are the ones
it opened with (the last fetch saved in ``.claude.json``, or the process's
own rate-limit headers). Those can be an hour old and can lack sections
the fetch returns."""

_DIALOG_DISMISSED_RE = re.compile(r"dialog\s*dismissed", re.IGNORECASE)
"""What ``claude`` prints in place of the panel once Esc closes it
(``⎿  Settings dialog dismissed``). It redraws the terminal's rows only, so
the render keeps the panel's lower rows below them without the sections
the redraw erased."""


def panel_pending(text: str) -> str | None:
    """What the ``/usage`` panel in rendered ``text`` has yet to draw, or ``None``.

    ``None`` means the panel is complete: its closing line reads ``Esc to
    cancel`` alone and nothing is in flight (:data:`_PANEL_IN_FLIGHT_RE`),
    so the numbers on screen are the ones the panel's own fetch returned,
    in every section that fetch returned. A panel whose refresh failed is
    never complete; :func:`panel_refresh_failure` names it.
    """
    if not any(match.group("keys") is None for match in _PANEL_FOOTER_RE.finditer(text)):
        return "its closing line 'Esc to cancel' is not drawn"
    in_flight = _PANEL_IN_FLIGHT_RE.search(text)
    if in_flight is not None:
        return f"it still shows {in_flight.group(0).strip()!r}"
    return None


def panel_refresh_failure(text: str) -> str | None:
    """The message of a ``/usage`` panel whose refresh failed, or ``None``.

    Such a panel offers a retry in its closing line (``r to retry · Esc to
    cancel``) under its error (``Error: …``), or under a note on the older
    numbers it still shows (``Could not refresh usage data``, ``Showing
    last-known usage …``). Returns the nearest non-blank line above that
    closing line, or the closing line itself when there is none.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = _PANEL_FOOTER_RE.match(line)
        if match is None or match.group("keys") is None or not _RETRY_RE.search(match["keys"]):
            continue
        above = [prior.strip() for prior in lines[:index] if prior.strip()]
        return above[-1] if above else line.strip()
    return None


@dataclass(frozen=True)
class _Block:
    """One usage block extracted from the rendered TUI text."""

    header: str | None
    pct: int
    resets_raw: str


@dataclass(frozen=True)
class _Gauge:
    """One ``NN% used`` line and the non-blank line drawn above it."""

    heading: str | None
    pct: int


def _classify_header(header: str | None) -> str:
    """Map a header line to a window class.

    Returns one of: ``"five_hour"``, ``"weekly_all"``,
    ``"weekly_other:<label>"``, or ``"unknown"``.
    """
    if header is None:
        return "unknown"
    if _HEADER_5H_RE.match(header):
        return "five_hour"
    if _HEADER_WEEKLY_ALL_RE.match(header):
        return "weekly_all"
    weekly_other = _HEADER_WEEKLY_OTHER_RE.match(header)
    if weekly_other is not None:
        label = weekly_other.group(1).strip()
        if label.lower() != "all models":
            return f"weekly_other:{label}"
        return "weekly_all"
    return "unknown"


@dataclass
class _BlockBuilder:
    """Walks the text accumulating headers, percentages, and Resets lines.

    Sections are emitted only when a complete (pct, resets) pair has
    been seen. The most recent header observed is associated with the
    next emitted block, unless a heading the parser does not recognise
    comes in between. Every percentage line is also kept as a
    :class:`_Gauge`, whether or not a Resets line completes it.
    """

    blocks: list[_Block] = field(default_factory=list)
    gauges: list[_Gauge] = field(default_factory=list)
    pending_header: str | None = None
    pending_pct: int | None = None
    last_line: str | None = None

    def feed(self, line: str) -> None:
        """Process one line of rendered text."""
        stripped = line.strip()
        if not stripped:
            return
        above, self.last_line = self.last_line, stripped

        # Section header — replaces any pending header. We don't reset
        # pending_pct here because some renders place the header before
        # the bar and others after; the pct slot accumulates either way.
        if (
            _HEADER_5H_RE.match(stripped)
            or _HEADER_WEEKLY_ALL_RE.match(stripped)
            or _HEADER_WEEKLY_OTHER_RE.match(stripped)
        ):
            self.pending_header = stripped
            return

        pct_match = _PCT_USED_RE.search(stripped)
        if pct_match is not None:
            # One to three digits: int() cannot fail.
            self.pending_pct = int(pct_match.group(1))
            self.gauges.append(_Gauge(heading=above, pct=self.pending_pct))
            return

        resets_match = _RESETS_RE.match(stripped)
        if resets_match is None:
            # A line with words that is no header, gauge or reset is a
            # heading the parser does not recognise. It starts another
            # section, so the pending header (one drawn without a gauge of
            # its own) does not carry over to that section's numbers. A bar
            # drawn on a line of its own has no letters and ends nothing.
            if any(char.isalpha() for char in stripped):
                self.pending_header = None
            return

        if self.pending_pct is not None:
            self.blocks.append(
                _Block(
                    header=self.pending_header,
                    pct=self.pending_pct,
                    resets_raw=resets_match.group(1),
                )
            )
            self.pending_header = None
            self.pending_pct = None


def _scan(text: str) -> _BlockBuilder:
    """Walk the rendered text; the result holds its blocks and gauges."""
    builder = _BlockBuilder()
    for line in text.splitlines():
        builder.feed(line)
    return builder


def _extract_blocks(text: str) -> list[_Block]:
    """Walk the rendered text and return all complete usage blocks."""
    return _scan(text).blocks


def _pick_window(blocks: list[_Block], wanted: str) -> _Block | None:
    """The first block whose header classifies as ``wanted``, if any."""
    for block in blocks:
        if _classify_header(block.header) == wanted:
            return block
    return None


def _unheaded_block(blocks: list[_Block], index: int) -> _Block | None:
    """The block at ``index`` when its header names no window, else ``None``.

    The positional stand-in for a window whose heading the parser cannot
    read: the synthetic fixtures and tests draw sections without one. A
    block under a heading that names another window (``Current week
    (Fable)``) never stands in for the 5-hour or the all-models window.
    """
    if len(blocks) > index and _classify_header(blocks[index].header) == "unknown":
        return blocks[index]
    return None


def _names_no_known_section(heading: str | None) -> bool:
    """Whether a gauge's ``heading`` names neither a usage window nor a credit."""
    if _classify_header(heading) != "unknown":
        return False
    return heading is None or _HEADER_CREDIT_RE.search(heading) is None


def _no_five_hour_section_doubts(text: str, gauges: list[_Gauge]) -> list[str]:
    """Why a panel without a 5-hour block may still have a 5-hour window.

    ``text`` is the rendered screen, ``gauges`` every ``NN% used`` line on
    it. The caller has found a ``Current week (all models)`` block and no
    5-hour block. An empty list means the panel shows that no 5-hour
    window applies, so it reads with ``five_hour=None``. That takes all
    of:

    * the panel complete (:func:`panel_pending`), not a refresh that
      failed (:func:`panel_refresh_failure`), and not dismissed: an
      unfinished or failed panel may lack sections its fetch would draw,
      and a dismissed one has lost its top rows;
    * no 5-hour heading (``Current session``) anywhere on the screen,
      which would be a 5-hour section without its percentage or reset;
    * every gauge under a weekly heading (``Current week (…)``) or a
      credit heading (:data:`_HEADER_CREDIT_RE`): a gauge under any other
      heading, or none, may be the 5-hour window renamed.
    """
    doubts: list[str] = []
    failure = panel_refresh_failure(text)
    pending = panel_pending(text)
    if failure is not None:
        doubts.append(f"the panel could not refresh its numbers ({failure!r})")
    elif pending is not None:
        doubts.append(f"the panel is not complete: {pending}")
    if _DIALOG_DISMISSED_RE.search(text):
        doubts.append("the panel was dismissed, so the screen holds only part of it")
    five_hour_headings = [line.strip() for line in text.splitlines() if _HEADER_5H_RE.match(line)]
    if five_hour_headings:
        doubts.append(f"5-hour heading {five_hour_headings[0]!r} without a percentage and reset")
    unknown = [gauge.heading for gauge in gauges if _names_no_known_section(gauge.heading)]
    if unknown:
        doubts.append(f"usage gauges under headings that name no known window: {unknown}")
    return doubts


def parse(raw: bytes, captured_at: datetime, clock: Clock) -> UsageReading:
    """Parse a raw ``claude /usage`` PTY capture into a :class:`UsageReading`.

    Windows are found by their headings. A panel with a ``Current week
    (all models)`` section and no ``Current session`` section reads with
    ``five_hour=None`` only when :func:`_no_five_hour_section_doubts`
    finds nothing. A panel without either heading falls back to position
    for the blocks it cannot classify (:func:`_unheaded_block`).

    Raises
    ------
    UsageFormatDrift
        If the input has no weekly (all models) window, has no 5-hour
        window and does not show that none applies, or has a percentage
        outside [0, 100].
    """
    if not raw:
        raise UsageFormatDrift("empty capture", raw=raw)

    text = render_mod.render(raw)
    scan = _scan(text)
    blocks = scan.blocks

    if not blocks:
        raise UsageFormatDrift("no usage blocks found in rendered TUI", raw=raw)

    five_hour_block = _pick_window(blocks, "five_hour")
    weekly_block = _pick_window(blocks, "weekly_all")

    if five_hour_block is None and weekly_block is not None:
        doubts = _no_five_hour_section_doubts(text, scan.gauges)
        if doubts:
            raise UsageFormatDrift(
                "5-hour usage block not found, and the panel does not show that no "
                f"5-hour window applies: {'; '.join(doubts)}",
                raw=raw,
            )
    else:
        five_hour_block = five_hour_block or _unheaded_block(blocks, 0)
        weekly_block = weekly_block or _unheaded_block(blocks, 1)
        if five_hour_block is None:
            raise UsageFormatDrift("5-hour usage block not found", raw=raw)
        if weekly_block is None:
            raise UsageFormatDrift("weekly (all models) usage block not found", raw=raw)

    if five_hour_block is not None and not (0 <= five_hour_block.pct <= 100):
        raise UsageFormatDrift(
            f"5-hour utilization {five_hour_block.pct}% outside [0, 100]",
            raw=raw,
        )
    if not (0 <= weekly_block.pct <= 100):
        raise UsageFormatDrift(
            f"weekly utilization {weekly_block.pct}% outside [0, 100]",
            raw=raw,
        )

    extras: list[ExtraWindow] = []
    for block in blocks:
        kind = _classify_header(block.header)
        if kind.startswith("weekly_other:"):
            label = kind.split(":", 1)[1]
            if not (0 <= block.pct <= 100):
                # Don't fail the whole parse on a malformed extra; skip it.
                continue
            extras.append(
                ExtraWindow(
                    label=label,
                    utilization_pct=block.pct,
                    resets_at_raw=block.resets_raw,
                    resets_at=parse_weekly(block.resets_raw, clock)
                    or parse_five_hour(block.resets_raw, clock),
                )
            )

    five_hour = (
        None
        if five_hour_block is None
        else WindowReading(
            utilization_pct=five_hour_block.pct,
            resets_at_raw=five_hour_block.resets_raw,
            resets_at=parse_five_hour(five_hour_block.resets_raw, clock),
        )
    )
    return UsageReading(
        captured_at=captured_at,
        five_hour=five_hour,
        seven_day=WindowReading(
            utilization_pct=weekly_block.pct,
            resets_at_raw=weekly_block.resets_raw,
            resets_at=parse_weekly(weekly_block.resets_raw, clock)
            or parse_five_hour(weekly_block.resets_raw, clock),
        ),
        extra_windows=extras,
    )
