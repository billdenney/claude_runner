"""Tests for the API usage source and its TTY fall-through composite.

Coverage:

* ``_headers_to_reading`` — happy path, missing/renamed headers,
  unparseable values, unknown ``-status`` (warned, not raised).
* ``_read_oauth_token`` — finds tokens at each documented key path,
  raises on missing file / malformed JSON / no matching key.
* ``ApiUsageSource.read`` — mocks ``urllib.request.urlopen``; verifies
  the request shape, header mapping, 401/403 → ``UsageApiAuthExpired``,
  network errors → ``UsageApiNetworkError``.
* ``ApiThenTtyUsageSource`` — falls through on each documented API
  exception, propagates TTY errors unchanged, returns API result when
  it succeeds.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.usage.api_source import (
    ANTHROPIC_API_URL,
    ANTHROPIC_VERSION,
    ApiUsageSource,
    CredentialsNotFound,
    _headers_to_reading,
    _read_oauth_token,
)
from claude_task_runner.usage.drift import (
    UsageApiAuthExpired,
    UsageApiHeaderMissing,
    UsageApiNetworkError,
    UsageFormatDrift,
)
from claude_task_runner.usage.models import UsageReading
from claude_task_runner.usage.source import (
    ApiThenTtyUsageSource,
    FakeUsageSource,
)

# ---------------------------------------------------------------------------
# _headers_to_reading — pure parsing path. All these tests are
# deterministic and free of I/O.
# ---------------------------------------------------------------------------


_VALID_HEADERS = {
    "anthropic-ratelimit-unified-5h-reset": "1765944000",
    "anthropic-ratelimit-unified-5h-status": "allowed",
    "anthropic-ratelimit-unified-5h-utilization": "0.042598363636363636",
    "anthropic-ratelimit-unified-7d-reset": "1766030400",
    "anthropic-ratelimit-unified-7d-status": "allowed",
    "anthropic-ratelimit-unified-7d-utilization": "0.3068459187383675",
    "anthropic-ratelimit-unified-fallback-percentage": "0.5",
}


def test_headers_to_reading_happy_path() -> None:
    captured_at = datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)
    reading = _headers_to_reading(dict(_VALID_HEADERS), captured_at)

    assert isinstance(reading, UsageReading)
    assert reading.captured_at == captured_at

    # 0.042598... * 100 ≈ 4.26, rounded → 4
    assert reading.five_hour.utilization_pct == 4
    assert reading.five_hour.resets_at == datetime.fromtimestamp(1765944000, tz=UTC)
    assert reading.five_hour.resets_at_raw == "1765944000"

    # 0.30684... * 100 ≈ 30.68, rounded → 31
    assert reading.seven_day.utilization_pct == 31
    assert reading.seven_day.resets_at == datetime.fromtimestamp(1766030400, tz=UTC)


def test_headers_to_reading_is_case_insensitive() -> None:
    """HTTP/2 servers can lower-case headers; HTTP/1.1 preserves casing."""
    captured_at = datetime(2026, 5, 21, tzinfo=UTC)
    upper = {k.upper(): v for k, v in _VALID_HEADERS.items()}
    reading = _headers_to_reading(upper, captured_at)
    assert reading.five_hour.utilization_pct == 4


def test_headers_to_reading_clamps_utilization_to_0_100() -> None:
    """A bogus 1.5 ratio (utilization > 100%) clamps to 100; -0.1 clamps to 0."""
    h = dict(_VALID_HEADERS)
    h["anthropic-ratelimit-unified-5h-utilization"] = "1.5"
    h["anthropic-ratelimit-unified-7d-utilization"] = "-0.1"
    captured_at = datetime(2026, 5, 21, tzinfo=UTC)
    r = _headers_to_reading(h, captured_at)
    assert r.five_hour.utilization_pct == 100
    assert r.seven_day.utilization_pct == 0


@pytest.mark.parametrize(
    "missing_key",
    [
        "anthropic-ratelimit-unified-5h-utilization",
        "anthropic-ratelimit-unified-5h-reset",
        "anthropic-ratelimit-unified-7d-utilization",
        "anthropic-ratelimit-unified-7d-reset",
    ],
)
def test_headers_to_reading_missing_required_header_raises(missing_key: str) -> None:
    """Each of the four required headers is checked individually."""
    h = dict(_VALID_HEADERS)
    del h[missing_key]
    with pytest.raises(UsageApiHeaderMissing) as exc_info:
        _headers_to_reading(h, datetime(2026, 5, 21, tzinfo=UTC))
    assert missing_key in str(exc_info.value)


def test_headers_to_reading_missing_status_is_not_fatal() -> None:
    """The -status header is informational; readings work without it."""
    h = dict(_VALID_HEADERS)
    del h["anthropic-ratelimit-unified-5h-status"]
    del h["anthropic-ratelimit-unified-7d-status"]
    r = _headers_to_reading(h, datetime(2026, 5, 21, tzinfo=UTC))
    assert r.five_hour.utilization_pct == 4


def test_headers_to_reading_unparseable_utilization_raises() -> None:
    h = dict(_VALID_HEADERS)
    h["anthropic-ratelimit-unified-5h-utilization"] = "not-a-number"
    with pytest.raises(UsageApiHeaderMissing):
        _headers_to_reading(h, datetime(2026, 5, 21, tzinfo=UTC))


def test_headers_to_reading_unparseable_reset_raises() -> None:
    h = dict(_VALID_HEADERS)
    h["anthropic-ratelimit-unified-5h-reset"] = "yesterday"
    with pytest.raises(UsageApiHeaderMissing):
        _headers_to_reading(h, datetime(2026, 5, 21, tzinfo=UTC))


def test_headers_to_reading_unknown_status_warns_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unknown -status values are logged so ops can build the enumeration
    over time, but they don't gate the reading itself."""
    h = dict(_VALID_HEADERS)
    h["anthropic-ratelimit-unified-5h-status"] = "throttled"
    with caplog.at_level(logging.WARNING, logger="claude_task_runner.usage.api_source"):
        _headers_to_reading(h, datetime(2026, 5, 21, tzinfo=UTC))
    assert any("status='throttled'" in r.message for r in caplog.records)


@pytest.mark.parametrize("ok_status", ["allowed", "allowed_warning"])
def test_headers_to_reading_recognised_statuses_silent(
    caplog: pytest.LogCaptureFixture,
    ok_status: str,
) -> None:
    """``allowed`` and ``allowed_warning`` are both benign — no log warning,
    no special handling. Observed in live traffic 2026-05-21: the 7d
    window returns ``allowed_warning`` above ~70% utilization, which is
    a notification not a throttle signal."""
    h = dict(_VALID_HEADERS)
    h["anthropic-ratelimit-unified-5h-status"] = ok_status
    h["anthropic-ratelimit-unified-7d-status"] = ok_status
    with caplog.at_level(logging.WARNING, logger="claude_task_runner.usage.api_source"):
        _headers_to_reading(h, datetime(2026, 5, 21, tzinfo=UTC))
    assert caplog.records == []


# ---------------------------------------------------------------------------
# _read_oauth_token — uses tmp_path to write throwaway credentials files.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "shape",
    [
        {"claudeAiOauth": {"accessToken": "tok-A"}},
        {"oauth": {"accessToken": "tok-A"}},
        {"oauth": {"access_token": "tok-A"}},
        {"accessToken": "tok-A"},
        {"access_token": "tok-A"},
    ],
)
def test_read_oauth_token_finds_each_known_shape(tmp_path: Path, shape: dict) -> None:
    """Each documented key path in _CREDENTIAL_KEY_PATHS resolves correctly."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / ".credentials.json").write_text(json.dumps(shape), encoding="utf-8")
    assert _read_oauth_token(str(config_dir)) == "tok-A"


def test_read_oauth_token_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(CredentialsNotFound) as exc_info:
        _read_oauth_token(str(tmp_path / "nonexistent"))
    assert "claude /login" in str(exc_info.value)


def test_read_oauth_token_invalid_json_raises(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / ".credentials.json").write_text("not json {", encoding="utf-8")
    with pytest.raises(CredentialsNotFound):
        _read_oauth_token(str(config_dir))


def test_read_oauth_token_no_matching_key_raises(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / ".credentials.json").write_text(
        json.dumps({"some_unknown_shape": {"foo": "bar"}}),
        encoding="utf-8",
    )
    with pytest.raises(CredentialsNotFound) as exc_info:
        _read_oauth_token(str(config_dir))
    # Error lists all the key paths we tried so the operator can add
    # whichever path matches their credentials file.
    assert "claudeAiOauth.accessToken" in str(exc_info.value)


# ---------------------------------------------------------------------------
# PR 14: long-lived token file takes precedence over .credentials.json.
# ---------------------------------------------------------------------------


def test_long_lived_token_file_takes_precedence(tmp_path: Path) -> None:
    """When `<config_dir>/oauth-token` exists, ignore .credentials.json
    entirely — a stale short-lived accessToken must NOT poison the lookup
    when the operator has provided a fresh long-lived token."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "stale-short-lived"}}),
        encoding="utf-8",
    )
    (config_dir / "oauth-token").write_text("sk-ant-oat01-LONG\n", encoding="utf-8")
    assert _read_oauth_token(str(config_dir)) == "sk-ant-oat01-LONG"


def test_long_lived_token_file_only_no_credentials_json(tmp_path: Path) -> None:
    """A queue can drop credentials.json entirely and rely on
    setup-token alone — the lookup still succeeds."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / "oauth-token").write_text("sk-ant-oat01-LONG\n", encoding="utf-8")
    assert _read_oauth_token(str(config_dir)) == "sk-ant-oat01-LONG"


def test_empty_long_lived_file_falls_through_to_credentials_json(
    tmp_path: Path,
) -> None:
    """A half-written oauth-token file (empty / whitespace-only) must
    not poison the lookup — the short-lived path still serves."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / "oauth-token").write_text("   \n", encoding="utf-8")
    (config_dir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "tok-A"}}),
        encoding="utf-8",
    )
    assert _read_oauth_token(str(config_dir)) == "tok-A"


# ---------------------------------------------------------------------------
# ApiUsageSource.read — mock urllib so the test is hermetic.
# ---------------------------------------------------------------------------


def _make_creds(tmp_path: Path, token: str = "test-token") -> str:
    config_dir = tmp_path / "claude"
    config_dir.mkdir()
    (config_dir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": token}}),
        encoding="utf-8",
    )
    return str(config_dir)


class _MockResponse:
    """Minimal stand-in for urlopen()'s context-manager return."""

    def __init__(self, headers: dict[str, str], body: bytes = b"{}") -> None:
        self.headers = _MockHeaders(headers)
        self._body = BytesIO(body)

    def __enter__(self) -> _MockResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body.read()


class _MockHeaders:
    """Mimics http.client.HTTPMessage's ``.items()`` interface."""

    def __init__(self, h: dict[str, str]) -> None:
        self._h = h

    def items(self) -> list[tuple[str, str]]:
        return list(self._h.items())


def test_api_source_read_happy_path(tmp_path: Path) -> None:
    config_dir = _make_creds(tmp_path, "test-token")
    clock = FakeClock(start=datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC))
    src = ApiUsageSource(clock, config_dir=config_dir)

    captured_request: list[urllib.request.Request] = []

    def _urlopen(req: urllib.request.Request, *, timeout: float) -> _MockResponse:
        captured_request.append(req)
        return _MockResponse(headers=_VALID_HEADERS)

    with patch("urllib.request.urlopen", side_effect=_urlopen):
        reading = src.read()

    # The request was built correctly.
    assert len(captured_request) == 1
    req = captured_request[0]
    assert req.full_url == ANTHROPIC_API_URL
    assert req.get_method() == "POST"
    # Headers (urllib lower-cases ``key`` lookups; we check via header_items).
    items = {k.lower(): v for k, v in req.header_items()}
    assert items["authorization"] == "Bearer test-token"
    assert items["anthropic-version"] == ANTHROPIC_VERSION
    assert items["content-type"] == "application/json"
    body = json.loads(req.data)  # type: ignore[arg-type]
    assert body["max_tokens"] == 1
    assert body["messages"] == [{"role": "user", "content": "."}]
    assert body["model"] == "claude-haiku-4-5"

    # The reading mapped the headers correctly.
    assert reading.five_hour.utilization_pct == 4
    assert reading.seven_day.utilization_pct == 31
    assert reading.captured_at == datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize("code", [401, 403])
def test_api_source_read_raises_auth_expired_on_401_403(tmp_path: Path, code: int) -> None:
    config_dir = _make_creds(tmp_path)
    src = ApiUsageSource(FakeClock(start=datetime.now(UTC)), config_dir=config_dir)

    err = urllib.error.HTTPError(
        ANTHROPIC_API_URL,
        code,
        "Unauthorized",
        hdrs=None,
        fp=None,  # type: ignore[arg-type]
    )
    with patch("urllib.request.urlopen", side_effect=err), pytest.raises(UsageApiAuthExpired):
        src.read()


def test_api_source_read_raises_network_error_on_500(tmp_path: Path) -> None:
    config_dir = _make_creds(tmp_path)
    src = ApiUsageSource(FakeClock(start=datetime.now(UTC)), config_dir=config_dir)

    err = urllib.error.HTTPError(
        ANTHROPIC_API_URL,
        500,
        "Server Error",
        hdrs=None,
        fp=None,  # type: ignore[arg-type]
    )
    with patch("urllib.request.urlopen", side_effect=err), pytest.raises(UsageApiNetworkError):
        src.read()


def test_api_source_read_raises_network_error_on_url_error(tmp_path: Path) -> None:
    config_dir = _make_creds(tmp_path)
    src = ApiUsageSource(FakeClock(start=datetime.now(UTC)), config_dir=config_dir)

    with (
        patch("urllib.request.urlopen", side_effect=urllib.error.URLError("DNS fail")),
        pytest.raises(UsageApiNetworkError),
    ):
        src.read()


def test_api_source_read_raises_auth_expired_when_creds_missing(tmp_path: Path) -> None:
    """A missing creds file is surfaced as UsageApiAuthExpired (subclass of
    CredentialsNotFound) so the composite source falls through to TTY."""
    src = ApiUsageSource(
        FakeClock(start=datetime.now(UTC)),
        config_dir=str(tmp_path / "nonexistent"),
    )
    with pytest.raises(UsageApiAuthExpired):
        src.read()


def test_api_source_read_raises_header_missing_when_headers_absent(tmp_path: Path) -> None:
    config_dir = _make_creds(tmp_path)
    src = ApiUsageSource(FakeClock(start=datetime.now(UTC)), config_dir=config_dir)

    with (
        patch(
            "urllib.request.urlopen",
            return_value=_MockResponse(headers={"content-type": "application/json"}),
        ),
        pytest.raises(UsageApiHeaderMissing),
    ):
        src.read()


# ---------------------------------------------------------------------------
# ApiThenTtyUsageSource — composite fall-through.
# ---------------------------------------------------------------------------


def _reading(util_5h: int = 7, util_7d: int = 33) -> UsageReading:
    from claude_task_runner.usage.models import WindowReading

    return UsageReading(
        captured_at=datetime(2026, 5, 21, tzinfo=UTC),
        five_hour=WindowReading(
            utilization_pct=util_5h,
            resets_at_raw="x",
            resets_at=datetime(2026, 5, 21, 17, tzinfo=UTC),
        ),
        seven_day=WindowReading(
            utilization_pct=util_7d,
            resets_at_raw="x",
            resets_at=datetime(2026, 5, 28, tzinfo=UTC),
        ),
    )


def test_composite_returns_api_reading_on_success() -> None:
    api_reading = _reading(util_5h=7, util_7d=33)
    api = FakeUsageSource([api_reading])
    tty = MagicMock()
    composite = ApiThenTtyUsageSource(api=api, tty=tty)
    out = composite.read()
    assert out.five_hour.utilization_pct == 7
    tty.read.assert_not_called()


@pytest.mark.parametrize(
    "api_exc",
    [
        UsageApiAuthExpired("token expired"),
        UsageApiHeaderMissing("headers gone"),
        UsageApiNetworkError("connection refused"),
    ],
)
def test_composite_falls_through_on_documented_api_failures(api_exc: Exception) -> None:
    api = MagicMock()
    api.read.side_effect = api_exc
    tty_reading = _reading(util_5h=42, util_7d=55)
    tty = FakeUsageSource([tty_reading])
    composite = ApiThenTtyUsageSource(api=api, tty=tty)
    out = composite.read()
    assert out.five_hour.utilization_pct == 42


def test_composite_does_not_swallow_format_drift_from_tty() -> None:
    """If the API fails AND the TTY produces format drift, the TTY error
    propagates so the supervisor's existing drift handling fires."""
    api = MagicMock()
    api.read.side_effect = UsageApiAuthExpired("token expired")
    tty = MagicMock()
    tty.read.side_effect = UsageFormatDrift("tui changed")
    composite = ApiThenTtyUsageSource(api=api, tty=tty)
    with pytest.raises(UsageFormatDrift):
        composite.read()


def test_composite_does_not_fall_through_on_unrelated_exception() -> None:
    """An exception class outside the documented fall-through set
    propagates, surfacing a real bug rather than silently masking it."""
    api = MagicMock()
    api.read.side_effect = RuntimeError("something else")
    tty = MagicMock()
    composite = ApiThenTtyUsageSource(api=api, tty=tty)
    with pytest.raises(RuntimeError):
        composite.read()
    tty.read.assert_not_called()
