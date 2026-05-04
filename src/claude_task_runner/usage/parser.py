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
   are exposed via :attr:`UsageReading.extra_windows` so EMA / cohort
   reasoning can use them later.
4. Validation — percentages in [0, 100], both windows present.

The parser is pure: it raises :class:`UsageFormatDrift` on any
structural deviation (missing primary windows, percentages out of
range). The capture layer is responsible for waiting long enough
that the OAuth response has rendered.
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
_RESETS_RE = re.compile(r"^\s*resets\s+(.+?)\s*$", re.IGNORECASE)
# Section header patterns observed in the live TUI.
_HEADER_5H_RE = re.compile(
    r"^\s*(?:current\s+session|5-?hour\s+session|5-?h\s+session)\s*$",
    re.IGNORECASE,
)
_HEADER_WEEKLY_ALL_RE = re.compile(
    r"^\s*(?:current\s+week\s*\(all\s+models\)|7-?day\s+weekly)\s*$",
    re.IGNORECASE,
)
_HEADER_WEEKLY_OTHER_RE = re.compile(
    r"^\s*current\s+week\s*\(([^)]+)\)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _Block:
    """One usage block extracted from the rendered TUI text."""

    header: str | None
    pct: int
    resets_raw: str


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
    next emitted block.
    """

    blocks: list[_Block] = field(default_factory=list)
    pending_header: str | None = None
    pending_pct: int | None = None

    def feed(self, line: str) -> None:
        """Process one line of rendered text."""
        stripped = line.strip()
        if not stripped:
            return

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
            try:
                self.pending_pct = int(pct_match.group(1))
            except ValueError:
                self.pending_pct = None
            return

        if self.pending_pct is not None:
            resets_match = _RESETS_RE.match(stripped)
            if resets_match is not None:
                self.blocks.append(
                    _Block(
                        header=self.pending_header,
                        pct=self.pending_pct,
                        resets_raw=resets_match.group(1),
                    )
                )
                self.pending_header = None
                self.pending_pct = None


def _extract_blocks(text: str) -> list[_Block]:
    """Walk the rendered text and return all complete usage blocks."""
    builder = _BlockBuilder()
    for line in text.splitlines():
        builder.feed(line)
    return builder.blocks


def _pick_window(blocks: list[_Block], wanted: str, fallback_index: int | None) -> _Block | None:
    """Find the block matching the wanted classification, or fall back to
    a positional pick when no header is available.

    The fallback supports our synthetic fixtures whose headers may not
    match the live TUI exactly. ``fallback_index`` of ``None`` means
    no fallback (the block must be header-classified).
    """
    classified = [(_classify_header(b.header), b) for b in blocks]
    for kind, block in classified:
        if kind == wanted:
            return block
    if fallback_index is None:
        return None
    if len(blocks) > fallback_index:
        return blocks[fallback_index]
    return None


def parse(raw: bytes, captured_at: datetime, clock: Clock) -> UsageReading:
    """Parse a raw ``claude /usage`` PTY capture into a :class:`UsageReading`.

    Raises
    ------
    UsageFormatDrift
        If the input does not contain the two primary windows (5-hour
        and weekly-all-models) with valid percentages in [0, 100].
    """
    if not raw:
        raise UsageFormatDrift("empty capture", raw=raw)

    text = render_mod.render(raw)
    blocks = _extract_blocks(text)

    if not blocks:
        raise UsageFormatDrift("no usage blocks found in rendered TUI", raw=raw)

    five_hour_block = _pick_window(blocks, "five_hour", fallback_index=0)
    weekly_block = _pick_window(blocks, "weekly_all", fallback_index=1)

    if five_hour_block is None:
        raise UsageFormatDrift("5-hour usage block not found", raw=raw)
    if weekly_block is None:
        raise UsageFormatDrift("weekly (all models) usage block not found", raw=raw)
    if five_hour_block is weekly_block:
        # Only one block found; the fallback gave us the same one twice.
        raise UsageFormatDrift(
            "found only 1 distinct usage block; expected 5-hour and weekly",
            raw=raw,
        )

    if not (0 <= five_hour_block.pct <= 100):
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

    return UsageReading(
        captured_at=captured_at,
        five_hour=WindowReading(
            utilization_pct=five_hour_block.pct,
            resets_at_raw=five_hour_block.resets_raw,
            resets_at=parse_five_hour(five_hour_block.resets_raw, clock),
        ),
        seven_day=WindowReading(
            utilization_pct=weekly_block.pct,
            resets_at_raw=weekly_block.resets_raw,
            resets_at=parse_weekly(weekly_block.resets_raw, clock)
            or parse_five_hour(weekly_block.resets_raw, clock),
        ),
        extra_windows=extras,
    )
