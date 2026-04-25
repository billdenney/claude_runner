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

**Robustness layers (added 2026-04-25 after observing real-world
HTTP 429 / 401 patterns at refresh_interval_s = 30 in production):**

1. **In-process throttling.** Anthropic rate-limits the
   ``/api/oauth/usage`` endpoint, and a runner that polls every 30s
   (the default ``refresh_interval_s``) will rapidly hit HTTP 429 — at
   that point every snapshot returns zeros and the controller falls
   back to its internal rolling window. The source now caches the
   most recent successful payload in-process for ``cache_ttl_s``
   seconds (default 300 = 5 minutes) and serves from that cache
   without shelling out. Utilization changes slowly enough at the
   token-budget granularity (one task burns ~3M tokens / a few
   minutes; the budget is hundreds of millions per 5h) that 5-minute
   freshness is more than sufficient for scheduling decisions.
2. **Disk-cache fallback.** The ``claude-usage`` bash helper writes
   ``~/.claude/usage-cache.json`` after every successful API call.
   When the in-process cache is stale AND the subprocess fails (HTTP
   429 / 401 / network error), the source reads that disk cache as a
   last-resort fallback before emitting zeros. Stale-but-real data
   beats zeros for budget gating.
3. **Hard max-age cap (15 minutes).** Both the in-process and disk
   caches are bounded by ``max_cache_age_s`` (default 900 s = 15 min).
   Operator preference: heavy concurrent extraction work can move
   utilization materially within 15 minutes, so anything older risks
   under-counting and over-dispatching. Past the cap the cache is
   ignored entirely and the snapshot returns zeros (the controller's
   internal rolling window then governs).

Failure path: subprocess error → in-process cache (if any, ≤ 15min) →
disk cache (if any, ≤ 15min) → zeros + warn-log; the controller's
internal rolling window then governs.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from claude_runner.budget.sources import UsageSnapshot

_log = logging.getLogger(__name__)

_DEFAULT_BINARY = "claude-usage"
_DEFAULT_CACHE_TTL_S = 300
# Hard maximum on cache age, beyond which neither the in-process nor disk
# cache may be served. Set to 15 minutes by operator preference: usage can
# climb materially within that window during heavy concurrent extraction
# work, and stale numbers risk under-counting and over-dispatching.
# Anything older than this is treated as unavailable and the snapshot
# returns zeros (the controller's internal rolling window then governs).
_DEFAULT_MAX_CACHE_AGE_S = 15 * 60  # 900 seconds
_DISK_CACHE_PATH = Path.home() / ".claude" / "usage-cache.json"


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
        cache_ttl_s: Seconds to serve the in-process cache without
            shelling out. Defaults to 300 (5 minutes). Set to 0 to
            disable in-process caching (every snapshot triggers a
            subprocess call). Implicitly bounded by ``max_cache_age_s``.
        max_cache_age_s: Hard ceiling on cache age — any cached payload
            (in-process or on-disk) older than this is treated as
            unavailable and the snapshot returns zeros instead. Default
            900 (15 minutes) per operator preference: heavy concurrent
            extraction work can move utilization meaningfully within
            that window, so older numbers risk under-counting and
            over-dispatching. Set very high to disable the cap (not
            recommended).
        disk_cache_path: Path to the helper's on-disk cache file. The
            source falls back to reading this when the in-process cache
            is stale AND the subprocess fails (e.g. HTTP 429 / 401).
            Defaults to ``~/.claude/usage-cache.json`` to match the
            helper's own location. Pass ``None`` to disable the disk
            fallback.
        clock: Optional callable returning the current ``datetime``
            (UTC). Test hook; defaults to ``datetime.now()``.
    """

    name = "claude_usage"

    def __init__(
        self,
        *,
        budget_5h_tokens: int,
        budget_weekly_tokens: int,
        binary: str = _DEFAULT_BINARY,
        cache_ttl_s: int = _DEFAULT_CACHE_TTL_S,
        max_cache_age_s: int = _DEFAULT_MAX_CACHE_AGE_S,
        disk_cache_path: Path | None = _DISK_CACHE_PATH,
        clock: object | None = None,
    ) -> None:
        self._budget_5h = max(int(budget_5h_tokens), 1)
        self._budget_week = max(int(budget_weekly_tokens), 1)
        self._binary = binary
        self._cmd = self._resolve_command(binary)
        self._cache_ttl_s = max(int(cache_ttl_s), 0)
        self._max_cache_age_s = max(int(max_cache_age_s), 0)
        self._disk_cache_path = disk_cache_path
        self._clock = clock or datetime.now

        # In-process cache state.
        self._cached_payload: dict[str, object] | None = None
        self._cached_at: datetime | None = None

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

    def _now(self) -> datetime:
        clock = self._clock
        return clock() if callable(clock) else clock  # type: ignore[return-value]

    def _cache_is_fresh(self) -> bool:
        if self._cached_payload is None or self._cached_at is None:
            return False
        age = (self._now() - self._cached_at).total_seconds()
        # Bound by both the soft TTL (avoid hammering the helper) and the
        # hard max-age cap (never serve data that may have spanned a
        # rate-limit window reset).
        if age >= self._max_cache_age_s:
            return False
        if self._cache_ttl_s <= 0:
            return False
        return age < self._cache_ttl_s

    def _read_disk_cache(self) -> dict[str, object] | None:
        """Return the bash helper's last-successful payload, or None.

        The helper writes ``~/.claude/usage-cache.json`` after every
        successful API call (even if the next call returns 429 / 401).
        Reading it directly lets us serve stale-but-real numbers when
        the subprocess can't reach the API at all.

        Refuses to return data older than ``max_cache_age_s`` (4.5h
        default) — the 5-hour rate-limit window resets every 5 hours,
        so a cache spanning a reset would over-report usage. Stale
        beyond the cap → emit zeros and let the controller's internal
        rolling window govern.
        """
        if self._disk_cache_path is None:
            return None
        try:
            stat = self._disk_cache_path.stat()
            text = self._disk_cache_path.read_text(encoding="utf-8")
        except OSError:
            return None
        # Disk-cache age is measured from the file mtime against our clock.
        # If the file is older than max_cache_age_s, it's almost certainly
        # spanned a rate-limit window reset and is unsafe to serve.
        try:
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=self._now().tzinfo)
        except (OSError, OverflowError, ValueError):
            mtime = None
        if mtime is not None:
            age = (self._now() - mtime).total_seconds()
            if age >= self._max_cache_age_s:
                _log.info(
                    "claude-usage disk cache rejected: %.0fs old > max_cache_age_s=%d",
                    age,
                    self._max_cache_age_s,
                )
                return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        return parsed

    def _fetch_subprocess(self) -> dict[str, object]:
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

    def _resolve_payload(self) -> tuple[dict[str, object] | None, str]:
        """Return ``(payload, provenance)`` for the freshest available data.

        Resolution order:
            1. In-process cache if still within ``cache_ttl_s``.
            2. Subprocess (live API call).
            3. Disk cache as last-resort fallback when subprocess fails.
            4. ``(None, "unavailable")`` to signal "emit zeros".
        """
        if self._cache_is_fresh():
            return self._cached_payload, "memory_cache"
        try:
            payload = self._fetch_subprocess()
        except ClaudeUsageError as exc:
            _log.warning("claude-usage subprocess failed: %s", exc)
            disk = self._read_disk_cache()
            if disk is not None:
                _log.info("claude-usage: serving from disk cache fallback")
                # Refresh in-process cache so we don't pound the helper or disk
                # while the API stays unavailable.
                self._cached_payload = disk
                self._cached_at = self._now()
                return disk, "disk_cache"
            return None, "unavailable"

        # Subprocess succeeded — refresh both layers.
        self._cached_payload = payload
        self._cached_at = self._now()
        return payload, "live"

    def snapshot(self) -> UsageSnapshot:
        payload, provenance = self._resolve_payload()
        if payload is None:
            _log.warning("claude-usage unavailable and no cache; emitting zeros")
            return UsageSnapshot(used_5h=0, used_week=0, source=self.name)

        five_hour = _window(payload, "five_hour")
        seven_day = _window(payload, "seven_day")

        pct_5h = _utilization_pct(five_hour)
        pct_week = _utilization_pct(seven_day)

        used_5h = round(pct_5h / 100.0 * self._budget_5h)
        used_week = round(pct_week / 100.0 * self._budget_week)

        reset_5h = _resets_at(five_hour)

        # Provenance is recorded in the source name when not "live", so the
        # operator can tell from `claude-runner status` whether the budget
        # numbers are fresh or cached.
        source_name = self.name if provenance == "live" else f"{self.name}/{provenance}"

        return UsageSnapshot(
            used_5h=used_5h,
            used_week=used_week,
            next_5h_reset=reset_5h,
            source=source_name,
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
