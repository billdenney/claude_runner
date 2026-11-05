"""Tests for the API usage source and its TTY fall-through composite.

Coverage:

* ``_headers_to_reading`` — happy path, missing/renamed headers,
  unparseable, non-finite, out-of-range and unrepresentable values
  (each the typed drift error), unknown ``-status`` (warned, not raised).
* ``_read_oauth_token`` — finds tokens at each documented key path,
  raises on missing file / malformed JSON / no matching key.
* ``ApiUsageSource.read`` — mocks ``urllib.request.urlopen``; verifies
  the request shape, header mapping, 401/403 → ``UsageApiAuthExpired``,
  network errors → ``UsageApiNetworkError``.
* ``ApiThenTtyUsageSource`` — falls through on each documented API
  exception, propagates TTY errors unchanged, returns API result when
  it succeeds.
* An unrepresentable header value, read through a real
  ``ApiUsageSource``, falls through to TTY in the composite and reaches
  the daemon's ``safe_poll`` as drift.
"""

from __future__ import annotations

import ast
import functools
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.supervisor.daemon import safe_poll
from claude_task_runner.usage.api_source import (
    ANTHROPIC_API_URL,
    ANTHROPIC_VERSION,
    ApiUsageSource,
    CredentialsNotFound,
    _headers_to_reading,
    _read_oauth_token,
)
from claude_task_runner.usage.drift import (
    AUTH_FAILURE_MARKERS,
    UsageApiAuthExpired,
    UsageApiHeaderMissing,
    UsageApiNetworkError,
    UsageFormatDrift,
    auth_failure_message,
    usage_error_kind,
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


_UTILIZATION_HEADERS = [
    "anthropic-ratelimit-unified-5h-utilization",
    "anthropic-ratelimit-unified-7d-utilization",
]
_RESET_HEADERS = [
    "anthropic-ratelimit-unified-5h-reset",
    "anthropic-ratelimit-unified-7d-reset",
]


@pytest.mark.parametrize("header", _UTILIZATION_HEADERS)
@pytest.mark.parametrize(
    ("value", "expected_pct"),
    [("0", 0), ("1.0", 100), ("-0.004", 0), ("1.004", 100)],
)
def test_headers_to_reading_utilization_rounding_into_range_is_accepted(
    header: str, value: str, expected_pct: int
) -> None:
    """The range check runs on the rounded percent, as the TTY parser's
    does, so float noise under half a percent past either end still reads."""
    h = dict(_VALID_HEADERS)
    h[header] = value
    r = _headers_to_reading(h, datetime(2026, 5, 21, tzinfo=UTC))
    window = r.five_hour if "-5h-" in header else r.seven_day
    assert window.utilization_pct == expected_pct


@pytest.mark.parametrize("header", _UTILIZATION_HEADERS)
@pytest.mark.parametrize(
    ("value", "expected_pct"),
    [("1.5", 150), ("-0.1", -10), ("1.006", 101), ("-0.006", -1), ("42.5", 4250)],
)
def test_headers_to_reading_out_of_range_utilization_raises(
    header: str, value: str, expected_pct: int
) -> None:
    """A ratio whose percent rounds outside [0, 100] is drift. It used to
    be clamped, so a percent sent in place of the ratio ("42.5") read as
    100% instead of surfacing."""
    h = dict(_VALID_HEADERS)
    h[header] = value
    with pytest.raises(UsageApiHeaderMissing) as exc_info:
        _headers_to_reading(h, datetime(2026, 5, 21, tzinfo=UTC))
    assert str(exc_info.value) == f"{header} value {value!r} is {expected_pct}%, outside [0, 100]"


@pytest.mark.parametrize("header", _UTILIZATION_HEADERS)
@pytest.mark.parametrize("value", ["inf", "-inf", "nan", "1e400", "-1e400", "1e308"])
def test_headers_to_reading_non_finite_utilization_raises_typed(header: str, value: str) -> None:
    """float() accepts each of these. round() raised OverflowError on the
    infinite ones, and on 1e308, a finite ratio that overflows once scaled
    to a percent. Only the typed error reaches the TTY fall-through and
    the daemon's safe_poll."""
    h = dict(_VALID_HEADERS)
    h[header] = value
    with pytest.raises(UsageApiHeaderMissing) as exc_info:
        _headers_to_reading(h, datetime(2026, 5, 21, tzinfo=UTC))
    assert str(exc_info.value) == f"{header} value {value!r} is not a finite percentage"


@pytest.mark.parametrize("header", _RESET_HEADERS)
@pytest.mark.parametrize(
    "value",
    [
        "inf",
        "-inf",
        "nan",
        "1e400",
        str(10**20),  # OverflowError: past the platform's time_t
        str(-(10**20)),
        str(2**63 - 1),  # OSError on Linux: gmtime fails with errno 75
        "253402300800",  # ValueError: year 10000
    ],
)
def test_headers_to_reading_unrepresentable_reset_raises_typed(header: str, value: str) -> None:
    """A reset timestamp that datetime cannot represent is drift, whatever
    fromtimestamp raises for it."""
    h = dict(_VALID_HEADERS)
    h[header] = value
    with pytest.raises(UsageApiHeaderMissing) as exc_info:
        _headers_to_reading(h, datetime(2026, 5, 21, tzinfo=UTC))
    assert str(exc_info.value).startswith(f"unparseable {header} header value {value!r}: ")


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
    with pytest.raises(UsageApiHeaderMissing) as exc_info:
        _headers_to_reading(h, datetime(2026, 5, 21, tzinfo=UTC))
    assert str(exc_info.value).startswith(
        "unparseable anthropic-ratelimit-unified-5h-utilization header value 'not-a-number': "
    )


def test_headers_to_reading_unparseable_reset_raises() -> None:
    h = dict(_VALID_HEADERS)
    h["anthropic-ratelimit-unified-5h-reset"] = "yesterday"
    with pytest.raises(UsageApiHeaderMissing) as exc_info:
        _headers_to_reading(h, datetime(2026, 5, 21, tzinfo=UTC))
    assert str(exc_info.value).startswith(
        "unparseable anthropic-ratelimit-unified-5h-reset header value 'yesterday': "
    )


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
# A response with no 5-hour window (ADR-0038). The two header sets are the
# rate-limit headers of real responses: an account the server reports only
# the 7-day window for, and one it reports both windows for.
# ---------------------------------------------------------------------------


_WEEKLY_ONLY_HEADERS = {
    "anthropic-ratelimit-unified-7d-reset": "1794394800",
    "anthropic-ratelimit-unified-7d-status": "allowed",
    "anthropic-ratelimit-unified-7d-utilization": "0.12",
    "anthropic-ratelimit-unified-fallback-percentage": "0.5",
    "anthropic-ratelimit-unified-overage-reset": "1796083200",
    "anthropic-ratelimit-unified-overage-status": "allowed",
    "anthropic-ratelimit-unified-overage-utilization": "0.0",
    "anthropic-ratelimit-unified-representative-claim": "seven_day",
    "anthropic-ratelimit-unified-reset": "1794394800",
    "anthropic-ratelimit-unified-status": "allowed",
}

_BOTH_WINDOWS_HEADERS = {
    "anthropic-ratelimit-unified-5h-reset": "1793908800",
    "anthropic-ratelimit-unified-5h-status": "allowed",
    "anthropic-ratelimit-unified-5h-utilization": "0.11",
    "anthropic-ratelimit-unified-7d-reset": "1794193200",
    "anthropic-ratelimit-unified-7d-status": "allowed",
    "anthropic-ratelimit-unified-7d-utilization": "0.61",
    "anthropic-ratelimit-unified-fallback-percentage": "0.5",
    "anthropic-ratelimit-unified-overage-reset": "1796083200",
    "anthropic-ratelimit-unified-overage-status": "allowed",
    "anthropic-ratelimit-unified-overage-utilization": "0.0",
    "anthropic-ratelimit-unified-representative-claim": "five_hour",
    "anthropic-ratelimit-unified-reset": "1793908800",
    "anthropic-ratelimit-unified-status": "allowed",
}

_CAPTURED_AT = datetime(2026, 10, 31, 1, 10, tzinfo=UTC)


def test_weekly_only_response_reads_with_no_five_hour_window(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="claude_task_runner.usage.api_source"):
        reading = _headers_to_reading(dict(_WEEKLY_ONLY_HEADERS), _CAPTURED_AT)

    assert reading.five_hour is None
    assert reading.five_hour_util_pct is None
    assert reading.seven_day.utilization_pct == 12
    assert reading.seven_day.resets_at == datetime(2026, 11, 11, 11, 0, tzinfo=UTC)
    assert reading.seven_day.resets_at_raw == "1794394800"
    assert reading.captured_at == _CAPTURED_AT
    assert caplog.records == []


def test_both_windows_response_reads_both() -> None:
    reading = _headers_to_reading(dict(_BOTH_WINDOWS_HEADERS), _CAPTURED_AT)

    assert reading.five_hour is not None
    assert reading.five_hour.utilization_pct == 11
    assert reading.five_hour_util_pct == 11
    assert reading.five_hour.resets_at == datetime(2026, 11, 5, 20, 0, tzinfo=UTC)
    assert reading.seven_day.utilization_pct == 61
    assert reading.seven_day.resets_at == datetime(2026, 11, 9, 3, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("present", "absent"),
    [
        ("anthropic-ratelimit-unified-5h-utilization", "anthropic-ratelimit-unified-5h-reset"),
        ("anthropic-ratelimit-unified-5h-reset", "anthropic-ratelimit-unified-5h-utilization"),
    ],
)
def test_one_five_hour_header_without_the_other_raises(present: str, absent: str) -> None:
    """Half a 5h pair is a broken response, not an account with no 5-hour window."""
    h = {**_WEEKLY_ONLY_HEADERS, present: _BOTH_WINDOWS_HEADERS[present]}
    with pytest.raises(UsageApiHeaderMissing) as exc_info:
        _headers_to_reading(h, _CAPTURED_AT)
    assert str(exc_info.value) == (
        f"missing rate-limit headers from /v1/messages: [{absent!r}]. "
        "Possible Anthropic header rename — falling back to TTY source."
    )


_SEVEN_DAY_PAIR = [
    "anthropic-ratelimit-unified-7d-utilization",
    "anthropic-ratelimit-unified-7d-reset",
]


@pytest.mark.parametrize(
    ("base", "missing"),
    [
        (_BOTH_WINDOWS_HEADERS, _SEVEN_DAY_PAIR),
        (
            _WEEKLY_ONLY_HEADERS,
            [
                "anthropic-ratelimit-unified-5h-utilization",
                "anthropic-ratelimit-unified-5h-reset",
                *_SEVEN_DAY_PAIR,
            ],
        ),
    ],
    ids=["with-5h", "without-5h"],
)
def test_missing_seven_day_pair_raises(base: dict[str, str], missing: list[str]) -> None:
    h = {k: v for k, v in base.items() if k not in _SEVEN_DAY_PAIR}
    with pytest.raises(UsageApiHeaderMissing) as exc_info:
        _headers_to_reading(h, _CAPTURED_AT)
    assert str(exc_info.value) == (
        f"missing rate-limit headers from /v1/messages: {missing}. "
        "Possible Anthropic header rename — falling back to TTY source."
    )


@pytest.mark.parametrize("absent", _SEVEN_DAY_PAIR)
def test_weekly_only_response_missing_one_seven_day_header_raises(absent: str) -> None:
    h = {k: v for k, v in _WEEKLY_ONLY_HEADERS.items() if k != absent}
    with pytest.raises(UsageApiHeaderMissing) as exc_info:
        _headers_to_reading(h, _CAPTURED_AT)
    assert str(exc_info.value).startswith(
        "missing rate-limit headers from /v1/messages: ["
        "'anthropic-ratelimit-unified-5h-utilization', "
        f"'anthropic-ratelimit-unified-5h-reset', {absent!r}]."
    )


_NO_5H_PAIR = (
    "missing rate-limit headers from /v1/messages: ["
    "'anthropic-ratelimit-unified-5h-utilization', 'anthropic-ratelimit-unified-5h-reset'], "
    "and the response does not show that no 5-hour window applies: "
)
_FALL_BACK = ". Possible Anthropic header rename — falling back to TTY source."
_CLAIM = "anthropic-ratelimit-unified-representative-claim"


def _without(headers: dict[str, str], *names: str) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k not in names}


@pytest.mark.parametrize(
    ("headers", "doubts"),
    [
        (
            {**_WEEKLY_ONLY_HEADERS, _CLAIM: "five_hour"},
            f"{_CLAIM} is 'five_hour'",
        ),
        (
            {
                **_WEEKLY_ONLY_HEADERS,
                "anthropic-ratelimit-5h-utilization": "0.11",
                "anthropic-ratelimit-5h-reset": "1793908800",
            },
            "5-hour headers ['anthropic-ratelimit-5h-reset', 'anthropic-ratelimit-5h-utilization']",
        ),
        (
            {**_WEEKLY_ONLY_HEADERS, "anthropic-ratelimit-unified-5h-status": "allowed"},
            "5-hour headers ['anthropic-ratelimit-unified-5h-status']",
        ),
        (
            {
                "anthropic-ratelimit-unified-7d-utilization": "0.12",
                "anthropic-ratelimit-unified-7d-reset": "1794394800",
            },
            f"no {_CLAIM} header",
        ),
        (
            _without(_WEEKLY_ONLY_HEADERS, _CLAIM),
            f"no {_CLAIM} header",
        ),
        (
            {**_WEEKLY_ONLY_HEADERS, "anthropic-ratelimit-unified-3h-utilization": "0.4"},
            "unrecognised window headers ['anthropic-ratelimit-unified-3h-utilization']",
        ),
        (
            {**_WEEKLY_ONLY_HEADERS, "Anthropic-Ratelimit-Unified-3H-Utilization": "0.4"},
            "unrecognised window headers ['anthropic-ratelimit-unified-3h-utilization']",
        ),
        (
            {
                **_WEEKLY_ONLY_HEADERS,
                _CLAIM: "five_hour",
                "anthropic-ratelimit-unified-five_hour-utilization": "0.4",
            },
            f"{_CLAIM} is 'five_hour'; "
            "5-hour headers ['anthropic-ratelimit-unified-five_hour-utilization']; "
            "unrecognised window headers ['anthropic-ratelimit-unified-five_hour-utilization']",
        ),
    ],
    ids=[
        "claim-five_hour",
        "5h-without-unified-prefix",
        "lone-5h-status",
        "only-the-7d-pair",
        "no-claim",
        "unknown-3h-window",
        "unknown-3h-window-mixed-case",
        "renamed-5h-window",
    ],
)
def test_missing_5h_pair_without_proof_of_no_5h_window_raises(
    headers: dict[str, str], doubts: str
) -> None:
    """Without the 5h pair, a response reads as having no 5-hour window only
    when it shows none applies. A claim of five_hour, a 5h header under
    another name, no claim at all, or a window this module does not know
    each leave a 5-hour window possible, so each raises."""
    with pytest.raises(UsageApiHeaderMissing) as exc_info:
        _headers_to_reading(headers, _CAPTURED_AT)
    assert str(exc_info.value) == _NO_5H_PAIR + doubts + _FALL_BACK
    assert usage_error_kind(str(exc_info.value)) == "format"


@pytest.mark.parametrize("claim", ["seven_day", "Seven_Day ", "overage"])
def test_weekly_only_response_with_another_claim_reads(claim: str) -> None:
    """Any representative claim that names no 5-hour window is evidence."""
    reading = _headers_to_reading({**_WEEKLY_ONLY_HEADERS, _CLAIM: claim}, _CAPTURED_AT)
    assert reading.five_hour is None
    assert reading.seven_day.utilization_pct == 12


def test_unrecognised_window_beside_both_windows_is_ignored() -> None:
    """With the 5h pair present nothing is missing; an extra window is
    ignored, as every header the reading does not use is."""
    h = {**_BOTH_WINDOWS_HEADERS, "anthropic-ratelimit-unified-3h-utilization": "0.4"}
    reading = _headers_to_reading(h, _CAPTURED_AT)
    assert reading.five_hour is not None
    assert reading.five_hour.utilization_pct == 11
    assert reading.seven_day.utilization_pct == 61


@pytest.mark.parametrize(
    ("header", "value", "message"),
    [
        (
            "anthropic-ratelimit-unified-7d-utilization",
            "nan",
            "anthropic-ratelimit-unified-7d-utilization value 'nan' is not a finite percentage",
        ),
        (
            "anthropic-ratelimit-unified-7d-utilization",
            "12",
            "anthropic-ratelimit-unified-7d-utilization value '12' is 1200%, outside [0, 100]",
        ),
    ],
)
def test_weekly_only_response_with_an_invalid_seven_day_value_raises(
    header: str, value: str, message: str
) -> None:
    """The 7d pair must be valid, not only present, for a reading with no
    5-hour window."""
    with pytest.raises(UsageApiHeaderMissing) as exc_info:
        _headers_to_reading({**_WEEKLY_ONLY_HEADERS, header: value}, _CAPTURED_AT)
    assert str(exc_info.value) == message


def test_weekly_only_response_with_an_unparseable_reset_raises() -> None:
    h = {**_WEEKLY_ONLY_HEADERS, "anthropic-ratelimit-unified-7d-reset": "next week"}
    with pytest.raises(UsageApiHeaderMissing) as exc_info:
        _headers_to_reading(h, _CAPTURED_AT)
    assert str(exc_info.value).startswith(
        "unparseable anthropic-ratelimit-unified-7d-reset header value 'next week': "
    )


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


def test_api_source_read_weekly_only_response(tmp_path: Path) -> None:
    config_dir = _make_creds(tmp_path)
    src = ApiUsageSource(FakeClock(start=_CAPTURED_AT), config_dir=config_dir)
    with patch("urllib.request.urlopen", return_value=_MockResponse(headers=_WEEKLY_ONLY_HEADERS)):
        reading = src.read()
    assert reading.five_hour is None
    assert reading.seven_day.utilization_pct == 12
    assert reading.captured_at == _CAPTURED_AT


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


def test_composite_returns_a_weekly_only_api_reading_without_the_tty() -> None:
    """A reading with no 5-hour window is a clean reading, not a failure
    to fall through on."""
    api_reading = _headers_to_reading(dict(_WEEKLY_ONLY_HEADERS), _CAPTURED_AT)
    tty = MagicMock()
    out = ApiThenTtyUsageSource(api=FakeUsageSource([api_reading]), tty=tty).read()
    assert out.five_hour is None
    assert out.seven_day.utilization_pct == 12
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


# ---------------------------------------------------------------------------
# An unrepresentable header value, end to end through a real ApiUsageSource.
# These values used to raise OverflowError, which the composite does not
# fall through on and safe_poll does not catch, so the supervisor's tick
# loop ended on its first poll.
# ---------------------------------------------------------------------------


_UNREPRESENTABLE_HEADER_VALUES = [
    ("anthropic-ratelimit-unified-5h-utilization", "inf"),
    ("anthropic-ratelimit-unified-7d-reset", str(10**20)),
]


@pytest.mark.parametrize(("header", "value"), _UNREPRESENTABLE_HEADER_VALUES)
def test_composite_falls_through_on_unrepresentable_header_value(
    tmp_path: Path, header: str, value: str
) -> None:
    h = dict(_VALID_HEADERS)
    h[header] = value
    api = ApiUsageSource(FakeClock(start=datetime.now(UTC)), config_dir=_make_creds(tmp_path))
    tty = FakeUsageSource([_reading(util_5h=42, util_7d=55)])
    composite = ApiThenTtyUsageSource(api=api, tty=tty)
    with patch("urllib.request.urlopen", return_value=_MockResponse(headers=h)):
        out = composite.read()
    assert out.five_hour.utilization_pct == 42
    assert out.seven_day.utilization_pct == 55


@pytest.mark.parametrize(("header", "value"), _UNREPRESENTABLE_HEADER_VALUES)
def test_safe_poll_returns_drift_for_unrepresentable_header_value(
    tmp_path: Path, header: str, value: str
) -> None:
    """In ``api`` mode nothing sits between the source and safe_poll, so
    the value must arrive as the drift the state machine routes to
    ERROR_DRIFT."""
    h = dict(_VALID_HEADERS)
    h[header] = value
    api = ApiUsageSource(FakeClock(start=datetime.now(UTC)), config_dir=_make_creds(tmp_path))
    with patch("urllib.request.urlopen", return_value=_MockResponse(headers=h)):
        result = safe_poll(api)
    assert isinstance(result, UsageApiHeaderMissing)
    assert header in str(result)


# ---------------------------------------------------------------------------
# How a recorded failure reads: a login to renew (auth) or a changed usage
# page (format). ``account list`` and /runner-status tell the operator which
# to fix from the recorded message alone.
# ---------------------------------------------------------------------------


def _raised(call: Callable[[], object]) -> Exception:
    try:
        call()
    except Exception as exc:
        return exc
    raise AssertionError("expected an exception")


def _rejected(tmp_path: Path, code: int) -> Exception:
    src = ApiUsageSource(FakeClock(start=datetime.now(UTC)), config_dir=_make_creds(tmp_path))
    err = urllib.error.HTTPError(
        ANTHROPIC_API_URL,
        code,
        "Unauthorized",
        hdrs=None,
        fp=None,  # type: ignore[arg-type]
    )
    with patch("urllib.request.urlopen", side_effect=err):
        return _raised(src.read)


def _no_credentials(tmp_path: Path) -> Exception:
    src = ApiUsageSource(FakeClock(start=datetime.now(UTC)), config_dir=str(tmp_path / "none"))
    return _raised(src.read)


def _credentials(tmp_path: Path, text: str) -> Exception:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / ".credentials.json").write_text(text, encoding="utf-8")
    return _raised(functools.partial(_read_oauth_token, str(config_dir)))


LOGIN_FAILURES: dict[str, Callable[[Path], Exception]] = {
    "rejected-401": functools.partial(_rejected, code=401),
    "rejected-403": functools.partial(_rejected, code=403),
    "no-credentials-file": _no_credentials,
    "credentials-not-json": functools.partial(_credentials, text="not json {"),
    "no-token-in-credentials": functools.partial(_credentials, text='{"other": {}}'),
}
"""Each way the API source reports a login failure, as it raises it."""


@pytest.mark.parametrize("make", LOGIN_FAILURES.values(), ids=list(LOGIN_FAILURES))
def test_each_login_failure_is_recorded_whole_as_auth(
    make: Callable[[Path], Exception], tmp_path: Path
) -> None:
    exc = make(tmp_path)
    assert isinstance(exc, UsageApiAuthExpired)
    assert auth_failure_message(exc) == str(exc)
    assert usage_error_kind(str(exc)) == "auth"


def test_a_login_failure_without_a_marker_gets_one() -> None:
    assert auth_failure_message(UsageApiAuthExpired("HTTP 401")) == "OAuth login failed: HTTP 401"


def test_an_unexpandable_config_dir_is_recorded_with_a_marker() -> None:
    """The one API source login failure that names no marker itself: the
    account's config_dir names a user who is gone."""
    src = ApiUsageSource(FakeClock(start=datetime.now(UTC)), config_dir="~nosuchuser-rs/x")
    exc = _raised(src.read)
    assert isinstance(exc, CredentialsNotFound)
    recorded = auth_failure_message(exc)
    assert recorded == f"OAuth login failed: {exc}"
    assert str(exc).startswith("config_dir '~nosuchuser-rs/x' cannot be expanded: ")
    assert usage_error_kind(recorded) == "auth"


@pytest.mark.parametrize(
    "recorded",
    [
        "OAuth bearer rejected by https://api.anthropic.com/v1/messages (HTTP 401)",
        "OAuth bearer rejected: OAuth bearer rejected by https://api.anthropic.com/v1/messages "
        "(HTTP 401)",
        "could not read /home/u/.claude/.credentials.json: [Errno 13] Permission denied",
    ],
    ids=["unprefixed", "earlier-prefix", "credentials-file"],
)
def test_messages_an_earlier_supervisor_recorded_read_as_auth(recorded: str) -> None:
    assert usage_error_kind(recorded) == "auth"


def _header_drift(headers: dict[str, str]) -> Exception:
    return _raised(
        functools.partial(_headers_to_reading, headers, datetime(2026, 5, 21, tzinfo=UTC))
    )


@pytest.mark.parametrize(
    "change",
    [
        {"anthropic-ratelimit-unified-5h-utilization": None},
        {"anthropic-ratelimit-unified-7d-reset": None},
        {"anthropic-ratelimit-unified-5h-utilization": "high"},
        {"anthropic-ratelimit-unified-7d-utilization": "nan"},
        {"anthropic-ratelimit-unified-5h-utilization": "42.5"},
        {"anthropic-ratelimit-unified-5h-reset": "soon"},
        {"anthropic-ratelimit-unified-7d-reset": "1e400"},
    ],
    ids=[
        "missing",
        "missing-reset",
        "unparseable",
        "non-finite",
        "out-of-range",
        "bad-reset",
        "unrepresentable-reset",
    ],
)
def test_each_header_drift_reads_as_format(change: dict[str, str | None]) -> None:
    headers = {k: v for k, v in {**_VALID_HEADERS, **change}.items() if v is not None}
    exc = _header_drift(headers)
    assert isinstance(exc, UsageFormatDrift)
    assert usage_error_kind(str(exc)) == "format"


USAGE_SOURCES = Path(__file__).parents[2] / "src" / "claude_task_runner" / "usage"
NOT_LOGIN_FAILURES = {
    "UsageFormatDrift",
    "UsageApiHeaderMissing",
    "UsageCaptureTimeout",
    "UsageApiNetworkError",
    "UsageCaptureSpawnError",
}
"""The usage errors that are not a login failure."""


def _literal_text(node: ast.expr) -> str:
    """The literal parts of a message argument: constants and f-string text."""
    return "".join(
        part.value
        for part in ast.walk(node)
        if isinstance(part, ast.Constant) and isinstance(part.value, str)
    )


def test_no_other_usage_error_names_a_marker() -> None:
    """A drift or network message that named a marker would read as a login
    to renew. Scans every ``raise`` of those errors under usage/."""
    raised: dict[str, str] = {}
    for path in sorted(USAGE_SOURCES.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
                continue
            func = node.exc.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in NOT_LOGIN_FAILURES:
                text = " ".join(_literal_text(arg) for arg in node.exc.args)
                raised[f"{path.name}:{node.lineno}"] = text
    assert len(raised) >= 10, raised
    named = {where: text for where, text in raised.items() if usage_error_kind(text) == "auth"}
    assert named == {}, f"markers {AUTH_FAILURE_MARKERS} in {named}"
