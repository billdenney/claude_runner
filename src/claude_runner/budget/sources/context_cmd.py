"""Fallback budget source that parses `claude /context` output."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess

from claude_runner.budget.sources import UsageSnapshot

_log = logging.getLogger(__name__)

# Matches things like "Tokens used this 5h: 1,234,567"
_FIVE_H_RE = re.compile(r"(?i)5[-\s]*h(?:our)?[^\n]*?([\d,]+)\s*(?:tokens|tok)?")
_WEEK_RE = re.compile(r"(?i)(?:weekly|week)[^\n]*?([\d,]+)\s*(?:tokens|tok)?")


class ContextCmdSource:
    name = "context"

    def __init__(self, *, binary: str = "claude") -> None:
        self._binary = binary

    def available(self) -> bool:
        return shutil.which(self._binary) is not None

    def snapshot(self) -> UsageSnapshot:
        if not self.available():
            _log.debug("claude CLI not on PATH; context source returning zeros")
            return UsageSnapshot(used_5h=0, used_week=0, source=self.name)
        try:
            result = subprocess.run(
                [self._binary, "-p", "/context"],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            _log.warning("claude /context failed: %s", exc)
            return UsageSnapshot(used_5h=0, used_week=0, source=self.name)
        out = result.stdout + "\n" + result.stderr
        used_5h = _extract(_FIVE_H_RE, out)
        used_week = _extract(_WEEK_RE, out)
        return UsageSnapshot(used_5h=used_5h, used_week=used_week, source=self.name)


def _extract(regex: re.Pattern[str], text: str) -> int:
    m = regex.search(text)
    if not m:
        return 0
    try:
        return int(m.group(1).replace(",", ""))
    except (IndexError, ValueError):  # pragma: no cover - regex only captures digit/comma strings
        return 0
