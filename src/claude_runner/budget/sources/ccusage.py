"""Budget source that shells out to the `ccusage` community tool."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import UTC, date, datetime

from claude_runner.budget.sources import UsageSnapshot

_log = logging.getLogger(__name__)


class CCUsageError(RuntimeError):
    pass


class CCUsageSource:
    """Parses `ccusage blocks --json` (5h sessions) and `ccusage daily --json`.

    Resolution order for the backing command:
      1. Explicit ``binary`` argument if it is on PATH.
      2. ``ccusage`` on PATH (e.g. ``npm install -g ccusage``).
      3. ``npx -y ccusage`` if ``npx`` is on PATH — lets the tool run without
         a separate install step whenever Node.js is available.
    """

    name = "ccusage"

    def __init__(self, *, binary: str = "ccusage") -> None:
        self._binary = binary
        self._cmd = self._resolve_command(binary)

    @staticmethod
    def _resolve_command(binary: str) -> list[str] | None:
        if shutil.which(binary) is not None:
            return [binary]
        if binary != "ccusage" and shutil.which("ccusage") is not None:
            return ["ccusage"]
        if shutil.which("npx") is not None:
            return ["npx", "-y", "ccusage"]
        return None

    def available(self) -> bool:
        return self._cmd is not None

    def _run(self, *args: str) -> dict[str, object]:
        if self._cmd is None:
            raise CCUsageError(
                "ccusage not found on PATH and npx is unavailable; "
                "install Node.js or run `npm install -g ccusage`"
            )
        display = " ".join(self._cmd)
        try:
            result = subprocess.run(
                [*self._cmd, *args, "--json"],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.CalledProcessError as exc:
            raise CCUsageError(
                f"{display} {' '.join(args)} failed: {exc.stderr.strip()}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CCUsageError(f"{display} {' '.join(args)} timed out") from exc
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CCUsageError(f"{self._binary} returned non-JSON output") from exc
        if not isinstance(parsed, dict):
            raise CCUsageError(f"{self._binary} returned unexpected JSON type")
        return parsed

    def snapshot(self) -> UsageSnapshot:
        blocks_used, next_reset = self._active_block()
        weekly_used = self._week_total()
        return UsageSnapshot(
            used_5h=blocks_used,
            used_week=weekly_used,
            next_5h_reset=next_reset,
            source=self.name,
        )

    def _active_block(self) -> tuple[int, datetime | None]:
        try:
            data = self._run("blocks")
        except CCUsageError as exc:
            _log.warning("ccusage blocks failed: %s", exc)
            return 0, None
        blocks = data.get("blocks") or data.get("data") or []
        if not isinstance(blocks, list):
            return 0, None
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("isActive") or block.get("active"):
                tokens = _extract_total_tokens(block)
                reset = _parse_dt(block.get("endTime") or block.get("endsAt"))
                return tokens, reset
        return 0, None

    def _week_total(self) -> int:
        try:
            data = self._run("daily")
        except CCUsageError as exc:
            _log.warning("ccusage daily failed: %s", exc)
            return 0
        from datetime import timedelta

        now = datetime.now(tz=UTC)
        week_anchor = (now - timedelta(days=now.weekday())).date()
        rows = data.get("daily") or data.get("data") or []
        total = 0
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                date = _parse_date(row.get("date"))
                if date is None or date < week_anchor:
                    continue
                total += _extract_total_tokens(row)
        return total


def _extract_total_tokens(row: dict[str, object]) -> int:
    for key in ("totalTokens", "total_tokens", "tokens", "allTokens"):
        v = row.get(key)
        if isinstance(v, (int, float)):
            return int(v)
    # Fallback: sum input/output/cache fields if present.
    total = 0
    for key in ("inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens"):
        v = row.get(key)
        if isinstance(v, (int, float)):
            total += int(v)
    return total


def _parse_dt(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_date(value: object) -> date | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None
