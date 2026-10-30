"""An integer one digit past the interpreter's limit for ``int()`` of a
string, and the ``ValueError`` it gets.

The YAML, JSON and TOML parsers all convert an integer literal with
``int()``, so a literal past the limit (4300 digits by default) raises a
bare ``ValueError`` from inside each of them. Its wording differs by
version: "Exceeds the limit (4300 digits) ..." from Python 3.12 on,
"Exceeds the limit (4300) ..." on 3.11.1. Tests compare against
:func:`digit_limit_error` instead of either spelling.
"""

from __future__ import annotations

import sys

PAST_DIGIT_LIMIT = "9" * (sys.get_int_max_str_digits() + 1)
"""Decimal digits one past this interpreter's limit."""


def digit_limit_error() -> str:
    """What ``int()`` says about :data:`PAST_DIGIT_LIMIT` on this interpreter."""
    try:
        int(PAST_DIGIT_LIMIT)
    except ValueError as exc:
        return str(exc)
    raise AssertionError(f"int() accepted {len(PAST_DIGIT_LIMIT)} digits")
