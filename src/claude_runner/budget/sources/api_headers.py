"""Budget source for the raw API regime — reads anthropic-ratelimit-* headers."""

from __future__ import annotations

import logging

from claude_runner.budget.sources import UsageSnapshot

_log = logging.getLogger(__name__)


class ApiHeadersSource:
    """Probe the raw Anthropic API for current rate-limit headers.

    Requires the `anthropic` extra (`pip install claude-runner[api]`). This
    source is only meaningful when `regime = "api"`; under Pro/Max/Teams the
    headers reflect a different set of limits and should not be trusted.
    """

    name = "api_headers"

    def __init__(self, *, itpm_budget: int, weekly_budget: int) -> None:
        self._itpm = itpm_budget
        self._weekly = weekly_budget

    def snapshot(self) -> UsageSnapshot:
        try:
            import anthropic
        except ImportError:
            _log.warning("anthropic SDK not installed; install with claude-runner[api]")
            return UsageSnapshot(used_5h=0, used_week=0, source=self.name)

        client = anthropic.Anthropic()
        try:
            response = client.messages.with_raw_response.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1,
                messages=[{"role": "user", "content": "."}],
            )
        except Exception as exc:
            _log.warning("API probe failed: %s", exc)
            return UsageSnapshot(used_5h=0, used_week=0, source=self.name)

        headers = response.headers
        input_remaining = _int(headers.get("anthropic-ratelimit-input-tokens-remaining"))
        input_limit = _int(headers.get("anthropic-ratelimit-input-tokens-limit"))
        used_minute = max(input_limit - input_remaining, 0) if input_limit else 0

        # Under the API regime we do not have weekly accounting from headers;
        # the controller's own WeeklyWindow takes over via record() calls.
        return UsageSnapshot(used_5h=used_minute, used_week=0, source=self.name)


def _int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0
