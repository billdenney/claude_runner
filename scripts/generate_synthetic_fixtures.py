"""Generate synthetic `claude /usage` fixtures for parser tests.

These fixtures match the documented TUI output format. They include enough
ANSI noise to exercise the parser's ANSI-stripping path. Real captures
(via `claude-task-runner usage capture --save`) are preferred when
available; this script provides initial coverage so the parser has tests
even before any real `claude` invocation.

Run:
    python scripts/generate_synthetic_fixtures.py
"""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent.parent / "tests" / "fixtures" / "usage"


def _bar(filled: int, total: int = 21) -> bytes:
    """Build a bar with ANSI dim / reset codes around the unfilled portion."""
    if filled > total:
        filled = total
    full = "█" * filled
    empty = "░" * (total - filled)
    # Bracket the empty portion in ANSI dim+reset to exercise the strip path.
    return f"\x1b[32m{full}\x1b[2m{empty}\x1b[0m".encode()


def _make_capture(
    five_pct: int,
    five_resets: str,
    week_pct: int,
    week_resets: str,
) -> bytes:
    """Build a synthetic raw .cap with two blocks plus surrounding noise."""
    # Welcome / shortcuts noise
    parts: list[bytes] = [
        b"\x1b[?1049h\x1b[H\x1b[J",   # alt screen + clear
        b"   Welcome to Claude Code\r\n",
        b"\x1b[2m   shortcuts:  /help  /usage  /exit\x1b[0m\r\n",
        b"\r\n",
        # 5-hour block — match real TUI section header.
        b"   Current session\r\n",
        b"   ",
        _bar(int(five_pct / 100 * 21)),
        f"  {five_pct}% used\r\n".encode(),
        f"   Resets {five_resets}\r\n".encode(),
        b"\r\n",
        # 7-day block — match real TUI "all models" header.
        b"   Current week (all models)\r\n",
        b"   ",
        _bar(int(week_pct / 100 * 21)),
        f"  {week_pct}% used\r\n".encode(),
        f"   Resets {week_resets}\r\n".encode(),
        b"\r\n",
        # Trailing noise (input prompt, exit echo)
        b"\x1b[2m> \x1b[0m\r\n",
        b"\x1b[?1049l",  # leave alt screen
    ]
    return b"".join(parts)


SCENARIOS: list[tuple[str, int, str, int, str]] = [
    # (label, 5h pct, 5h resets text, weekly pct, weekly resets text)
    ("synthetic_normal", 38, "2:10am (UTC)", 20, "May 4, 3am (UTC)"),
    ("synthetic_high_5h", 92, "11:45pm (UTC)", 47, "May 8, 3am (UTC)"),
    ("synthetic_weekly_capped", 50, "8:00pm (UTC)", 91, "May 4, 3am (UTC)"),
    ("synthetic_zero", 0, "12am (UTC)", 0, "May 9, 12am (UTC)"),
    ("synthetic_full_5h", 100, "4:55pm (UTC)", 65, "May 6, 3am (UTC)"),
    ("synthetic_minute_precision", 77, "3:42pm (UTC)", 33, "May 4, 11:30pm (UTC)"),
]


def main() -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    for label, five_pct, five_resets, week_pct, week_resets in SCENARIOS:
        cap_bytes = _make_capture(five_pct, five_resets, week_pct, week_resets)

        cap_path = FIXTURES_DIR / f"{label}.cap"
        cap_path.write_bytes(cap_bytes)

        expected = {
            "five_hour": {
                "utilization": five_pct,
                "resets_at_raw": five_resets,
            },
            "seven_day": {
                "utilization": week_pct,
                "resets_at_raw": week_resets,
            },
        }
        expected_path = FIXTURES_DIR / f"{label}.expected.json"
        expected_path.write_text(json.dumps(expected, indent=2) + "\n")

        print(f"wrote {cap_path.name} ({len(cap_bytes)} bytes) and {expected_path.name}")


if __name__ == "__main__":
    main()
