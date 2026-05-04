"""VT100-emulator-based rendering of captured ``claude /usage`` output.

The TUI uses cursor-position ANSI sequences to overwrite the placeholder
panel in-place once the OAuth API responds. Simple regex-based ANSI
stripping conflates the placeholder values with the final values
(stream contains both, with cursor moves between them).

We use :mod:`pyte` to feed the captured bytes into a virtual terminal
and read back the **final rendered screen state** as plain text. That
is what the user actually sees in their terminal — including any
in-place redraws.

This module is the only file in :mod:`claude_task_runner.usage` that
depends on :mod:`pyte`. Other modules (parser, drift, source) consume
its output as plain text.
"""

from __future__ import annotations

import pyte

# Generous defaults so wide TUIs (multi-column layouts, the full /usage
# panel including the right-side "Tips for getting started" sidebar)
# render without wrapping. 100 rows is enough for the documented panel
# variants we've observed.
DEFAULT_COLUMNS = 200
DEFAULT_ROWS = 100


def render(raw: bytes, *, columns: int = DEFAULT_COLUMNS, rows: int = DEFAULT_ROWS) -> str:
    """Render captured PTY bytes into the final on-screen text.

    Parameters
    ----------
    raw
        The raw byte stream captured from the ``claude`` PTY (including
        all ANSI escape sequences). Empty input yields an empty string.
    columns, rows
        Virtual terminal dimensions. Defaults are intentionally large
        so the actual TUI rendering fits without wrapping.

    Returns
    -------
    str
        The final screen state as a newline-joined string of rows.
        Each row is exactly ``columns`` characters wide; trailing
        whitespace is preserved so column-based parsing works.
    """
    if not raw:
        return ""

    screen = pyte.Screen(columns, rows)
    stream = pyte.Stream(screen)
    # pyte expects str; decode permissively so undecodable bytes don't crash.
    stream.feed(raw.decode("utf-8", errors="replace"))
    return "\n".join(screen.display)
