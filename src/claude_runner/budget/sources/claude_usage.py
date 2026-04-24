"""Budget source backed by the `claude-usage` helper (real OAuth rate-limit API).

Shells out to ``claude-usage json`` (a small bash helper that hits
``https://api.anthropic.com/api/oauth/usage`` with the OAuth token stored
in ``~/.claude/.credentials.json``). Unlike ``ccusage`` — which counts
tokens from local session transcripts and tries to infer rate-limit
progress — this source reads the **actual** utilization percentages the
Anthropic API itself reports for the operator's plan. That is the
ground truth the ``/usage`` slash-command displays inside Claude Code.

The API returns utilization as a 0-100 percentage per window
(``five_hour``, ``seven_day``, and model-specific variants). The source
converts back to a token-equivalent count against the configured
``budget_5h_tokens`` / ``budget_weekly_tokens`` so the existing
``TokenBudgetController`` math (remaining = budget - used) works
without unit mismatches.

Install: the helper lives at ``~/.local/bin/claude-usage`` (bash +
``curl`` + ``jq``); see the companion script in the operator's dotfiles.
This source emits zeros and logs a warning when the helper is missing
or the API returns a non-200 — the controller then falls back to its
internal rolling window and ``ccusage`` historical calibration.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import datetime

from claude_runner.budget.sources import UsageSnapshot

_log = logging.getLogger(__name__)

_DEFAULT_BINARY = "claude-usage"


class ClaudeUsageError(RuntimeError):
    pass


class ClaudeUsageSource:
    """Queries ``claude-usage json`` for live 5h + weekly utilization.

    Construct with the plan's resolved 5h and weekly budgets (tokens).
    ``snapshot()`` translates the API's percentage utilization back to
    token-equivalent counts against those budgets so
    ``TokenBudgetController`` can compare used vs. budget in one unit.

    Args:
        budget_5h_tokens: Tokens in the 5-hour window per the user's plan.
        budget_weekly_tokens: Tokens in the 7-day window per the plan.
        binary: Path or PATH-resolvable name of the ``claude-usage``
            helper. Defaults to ``claude-usage``.
    """

    name = "claude_usage"

    def __init__(
        self,
        *,
        budget_5h_tokens: int,
        budget_weekly_tokens: int,
        binary: str = _DEFAULT_BINARY,
    ) -> None:
        self._budget_5h = max(int(budget_5h_tokens), 1)
        self._budget_week = max(int(budget_weekly_tokens), 1)
        self._binary = binary
        self._cmd = self._resolve_command(binary)

    @staticmethod
    def _resolve_command(binary: str) -> list[str] | None:
        # Accept an absolute path directly; otherwise require it on PATH.
        if "/" in binary:
            return [binary]
        if shutil.which(binary) is not None:
            return [binary]
        return None

    def available(self) -> bool:
        return self._cmd is not None

    def _fetch(self) -> dict[str, object]:
        if self._cmd is None:
            raise ClaudeUsageError(
                f"{self._binary} not found on PATH; "
                "install ~/.local/bin/claude-usage or set budget_source to a different value"
            )
        try:
            result = subprocess.run(
                [*self._cmd, "json"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.CalledProcessError as exc:
            raise ClaudeUsageError(
                f"{self._binary} json exited {exc.returncode}: {exc.stderr.strip()}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ClaudeUsageError(f"{self._binary} json timed out") from exc
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ClaudeUsageError(
                f"{self._binary} json returned non-JSON output: {result.stdout[:200]!r}"
            ) from exc
        if not isinstance(parsed, dict):
            raise ClaudeUsageError(f"{self._binary} json returned unexpected type {type(parsed)}")
        return parsed

    def snapshot(self) -> UsageSnapshot:
        try:
            payload = self._fetch()
        except ClaudeUsageError as exc:
            _log.warning("claude-usage unavailable: %s", exc)
            return UsageSnapshot(used_5h=0, used_week=0, source=self.name)

        five_hour = _window(payload, "five_hour")
        seven_day = _window(payload, "seven_day")

        pct_5h = _utilization_pct(five_hour)
        pct_week = _utilization_pct(seven_day)

        used_5h = round(pct_5h / 100.0 * self._budget_5h)
        used_week = round(pct_week / 100.0 * self._budget_week)

        reset_5h = _resets_at(five_hour)
        return UsageSnapshot(
            used_5h=used_5h,
            used_week=used_week,
            next_5h_reset=reset_5h,
            source=self.name,
        )


def _window(payload: dict[str, object], key: str) -> dict[str, object] | None:
    value = payload.get(key)
    if isinstance(value, dict):
        return value
    return None


def _utilization_pct(window: dict[str, object] | None) -> float:
    if window is None:
        return 0.0
    raw = window.get("utilization")
    if raw is None:
        return 0.0
    try:
        pct = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    # Clamp to [0, 100] — the API occasionally reports slightly over 100
    # at window end, which would over-report usage in token-equivalents.
    if pct < 0.0:
        return 0.0
    if pct > 100.0:
        return 100.0
    return pct


def _resets_at(window: dict[str, object] | None) -> datetime | None:
    if window is None:
        return None
    raw = window.get("resets_at")
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
