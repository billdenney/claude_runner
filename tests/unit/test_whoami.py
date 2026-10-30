"""Tests for usage.whoami — account identity from credentials + TUI welcome."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_task_runner.usage.whoami import (
    IdentitySnapshot,
    credentials_path,
    extract_welcome_label,
    from_capture,
    from_credentials_only,
    read_credentials,
)

from ._digit_limit import PAST_DIGIT_LIMIT, digit_limit_error


def _write_creds(
    config_dir: Path,
    *,
    subscription: str,
    rate_limit: str,
    scopes: list[str] | None = None,
) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "claudeAiOauth": {
            "accessToken": "opaque-token",
            "subscriptionType": subscription,
            "rateLimitTier": rate_limit,
            "scopes": scopes or ["user:profile"],
        }
    }
    (config_dir / ".credentials.json").write_text(json.dumps(payload))


class TestCredentialsPath:
    def test_explicit_dir(self) -> None:
        assert credentials_path("/tmp/foo") == Path("/tmp/foo/.credentials.json")

    def test_default(self) -> None:
        assert credentials_path("") == Path.home() / ".claude" / ".credentials.json"

    def test_expanduser(self) -> None:
        assert credentials_path("~/.foo") == Path.home() / ".foo" / ".credentials.json"


_LOGGER = "claude_task_runner.usage.whoami"


class TestReadCredentials:
    def test_missing_file_returns_empty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING", logger=_LOGGER):
            assert read_credentials(str(tmp_path)) == {}
        assert caplog.records == []

    def test_full_payload(self, tmp_path: Path) -> None:
        _write_creds(
            tmp_path,
            subscription="team",
            rate_limit="default_claude_max_5x",
            scopes=["user:profile", "user:inference"],
        )
        out = read_credentials(str(tmp_path))
        assert out["subscription_type"] == "team"
        assert out["rate_limit_tier"] == "default_claude_max_5x"
        assert out["scopes"] == ("user:profile", "user:inference")

    def test_invalid_json_returns_empty_with_a_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / ".credentials.json"
        path.write_text("{not json")
        with caplog.at_level("WARNING", logger=_LOGGER):
            assert read_credentials(str(tmp_path)) == {}
        assert [r.getMessage() for r in caplog.records] == [
            f"could not read {path}: Expecting property name enclosed in double quotes: "
            "line 1 column 2 (char 1); reporting no credentials"
        ]

    @pytest.mark.parametrize(
        ("content", "error"),
        [
            pytest.param(
                f'{{"claudeAiOauth": {{"n": {PAST_DIGIT_LIMIT}}}}}'.encode(),
                digit_limit_error(),
                id="int-past-digit-limit",
            ),
            pytest.param(
                b"\xff{}",
                "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte",
                id="not-utf8",
            ),
        ],
    )
    def test_json_that_raises_a_bare_value_error_returns_empty(
        self, tmp_path: Path, content: bytes, error: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """json raises these as bare ValueErrors, not JSONDecodeError."""
        path = tmp_path / ".credentials.json"
        path.write_bytes(content)
        with caplog.at_level("WARNING", logger=_LOGGER):
            assert read_credentials(str(tmp_path)) == {}
        assert [r.getMessage() for r in caplog.records] == [
            f"could not read {path}: {error}; reporting no credentials"
        ]

    @pytest.mark.parametrize("content", ["[]", "null", '"text"'])
    def test_json_that_is_not_an_object_returns_empty(
        self, tmp_path: Path, content: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / ".credentials.json"
        path.write_text(content)
        with caplog.at_level("WARNING", logger=_LOGGER):
            assert read_credentials(str(tmp_path)) == {}
        assert [r.getMessage() for r in caplog.records] == [
            f"{path} is not a JSON object; reporting no credentials"
        ]

    def test_missing_oauth_section_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / ".credentials.json").write_text('{"otherKey": {}}')
        assert read_credentials(str(tmp_path)) == {}


class TestExtractWelcomeLabel:
    def test_single_line(self) -> None:
        out = extract_welcome_label("Opus 4.7 (1M context) · Claude Team · Human Predictions")
        assert out == "Claude Team · Human Predictions"

    def test_personal_account_label(self) -> None:
        # Personal Pro/Max plans show no org suffix.
        out = extract_welcome_label("Opus 4.7 (1M context) · Claude Pro")
        assert out == "Claude Pro"

    def test_wrap_continuation(self) -> None:
        # Real-world layout: org name wraps to next line in same column.
        text = (
            "│  Opus 4.7 (1M context) · Claude Team · Human   │ Tips     │\n"
            "│  Predictions                                   │ Run /…   │\n"
        )
        out = extract_welcome_label(text)
        assert out == "Claude Team · Human Predictions"

    def test_sidebar_does_not_bleed(self) -> None:
        text = (
            "│  Opus 4.7 (1M context) · Claude Team           │ Tips for │\n"
            "│                                                │ Added X  │\n"
        )
        out = extract_welcome_label(text)
        assert out == "Claude Team"
        assert "Tips" not in out
        assert "Added" not in out

    def test_no_match_returns_empty(self) -> None:
        assert extract_welcome_label("just some text") == ""
        assert extract_welcome_label("") == ""

    def test_continuation_rejects_punctuation_blob(self) -> None:
        text = (
            "│  Opus 4.7 (1M context) · Claude Team · Human   │\n"
            "│  ============================                  │\n"
        )
        out = extract_welcome_label(text)
        # The "===..." line should NOT be appended.
        assert out == "Claude Team · Human"


class TestSnapshotPredicates:
    def test_team_is_team(self) -> None:
        snap = IdentitySnapshot(config_dir="", subscription_type="team")
        assert snap.is_team() is True
        assert snap.is_personal() is False

    def test_max_is_personal(self) -> None:
        snap = IdentitySnapshot(config_dir="", subscription_type="max20")
        assert snap.is_personal() is True
        assert snap.is_team() is False

    def test_pro_is_personal(self) -> None:
        snap = IdentitySnapshot(config_dir="", subscription_type="pro")
        assert snap.is_personal() is True

    def test_unknown_is_neither(self) -> None:
        snap = IdentitySnapshot(config_dir="", subscription_type="")
        assert snap.is_team() is False
        assert snap.is_personal() is False


class TestFromCapture:
    def test_combines_creds_and_welcome(self, tmp_path: Path) -> None:
        _write_creds(tmp_path, subscription="team", rate_limit="default_claude_max_5x")
        raw = (
            b"\x1b[?1049h"
            b"\xe2\x95\xad Welcome \xe2\x95\xae\r\n"
            b"\xe2\x94\x82 Opus 4.7 (1M context) \xc2\xb7 Claude Team \xc2\xb7 "
            b"Human Predictions \xe2\x94\x82\r\n"
        )
        snap = from_capture(raw, str(tmp_path))
        assert snap.subscription_type == "team"
        assert snap.config_dir == str(tmp_path)
        # Welcome label may or may not extract depending on box-drawing
        # rendering — but the credentials half is reliable.
        assert snap.is_team()

    def test_missing_creds_yields_empty_subscription(self, tmp_path: Path) -> None:
        snap = from_capture(b"", str(tmp_path))
        assert snap.subscription_type == ""
        assert snap.is_team() is False
        assert snap.is_personal() is False


class TestFromCredentialsOnly:
    def test_skips_capture(self, tmp_path: Path) -> None:
        _write_creds(tmp_path, subscription="max20", rate_limit="max_20x")
        snap = from_credentials_only(str(tmp_path))
        assert snap.welcome_label == ""
        assert snap.subscription_type == "max20"
        assert snap.is_personal() is True
