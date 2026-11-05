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

import codecs

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

    screen = LiveScreen(columns=columns, rows=rows)
    screen.feed(raw, final=True)
    return screen.text()


class LiveScreen:
    """A virtual terminal fed the PTY stream piece by piece as it arrives.

    The usage capture reads the screen while ``claude`` is still drawing it,
    so each read feeds only the new bytes instead of re-rendering the whole
    stream. A UTF-8 sequence split across two reads is decoded once both
    halves have arrived.
    """

    def __init__(self, *, columns: int, rows: int) -> None:
        self._screen = pyte.Screen(columns, rows)
        self._stream = pyte.Stream(self._screen)
        # pyte expects str; decode permissively so undecodable bytes don't crash.
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def feed(self, data: bytes, *, final: bool = False) -> None:
        """Draw ``data``; ``final`` flushes a trailing incomplete UTF-8 sequence."""
        self._stream.feed(self._decoder.decode(data, final=final))

    def text(self) -> str:
        """The screen as newline-joined rows, each exactly ``columns`` wide."""
        return "\n".join(self._screen.display)
