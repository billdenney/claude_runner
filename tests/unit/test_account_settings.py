"""Tests for AccountSettings + per-account policy loading.

Covers:
* AccountSettings field validators (name shape, linux_user truthiness).
* Settings._synthesize_legacy_account: legacy [claude].config_dir gets
  folded into a single 'default' [[accounts]] entry when none is
  declared explicitly.
* Settings._validate_account_names_unique: duplicate names rejected.
* Settings accepts an explicit multi-account list and preserves order.
* AccountPolicy / load_account_policy / resolve_accounts: per-account
  dispatch policy lives at <config_dir>/runner-account.toml; missing
  file → defaults, present-and-partial → defaults fill in.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from claude_task_runner.config.loader import (
    ConfigError,
    load_account_policy,
    load_settings,
    per_account_toml_path,
    resolve_accounts,
)
from claude_task_runner.config.schema import (
    AccountPolicy,
    AccountSettings,
    ResolvedAccount,
    Settings,
)


class TestAccountSettings:
    def test_minimal_valid(self) -> None:
        acct = AccountSettings(name="personal", config_dir="/tmp/c")
        assert acct.name == "personal"
        assert acct.config_dir == "/tmp/c"
        assert acct.linux_user is None

    def test_full_fields(self) -> None:
        acct = AccountSettings(
            name="work",
            config_dir="/home/bw/.claude",
            linux_user="bill-work",
        )
        assert acct.linux_user == "bill-work"

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "-leading-hyphen",
            ".leading-dot",
            "has space",
            "slash/in/name",
            "x" * 65,  # too long
        ],
    )
    def test_invalid_name_rejected(self, name: str) -> None:
        with pytest.raises(ValidationError):
            AccountSettings(name=name, config_dir="")

    @pytest.mark.parametrize("name", ["a", "A1", "p_e_r", "name.with.dot", "x-y", "default"])
    def test_valid_name_accepted(self, name: str) -> None:
        assert AccountSettings(name=name, config_dir="").name == name

    def test_blank_linux_user_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AccountSettings(name="a", config_dir="", linux_user="   ")

    def test_inline_concurrency_field_rejected(self) -> None:
        """Per-account `max_concurrency` no longer lives on AccountSettings.

        It moved to the per-account `runner-account.toml`; an inline
        field in the queue's [[accounts]] block is a schema typo and
        must be rejected (extra='forbid' on _StrictModel).
        """
        with pytest.raises(ValidationError):
            AccountSettings.model_validate({"name": "p", "config_dir": "/x", "max_concurrency": 5})

    def test_inline_priority_field_rejected(self) -> None:
        """`priority` was removed entirely; all accounts are equal."""
        with pytest.raises(ValidationError):
            AccountSettings.model_validate({"name": "p", "config_dir": "/x", "priority": 1})


class TestSettingsLegacySynthesis:
    def test_defaults_only_synthesises_single_default_account(self) -> None:
        """`load_settings(None)` (just package defaults) yields one 'default'."""
        s = load_settings(None)
        assert len(s.accounts) == 1
        assert s.accounts[0].name == "default"
        assert s.accounts[0].config_dir == ""

    def test_legacy_config_dir_folded_into_default(self, tmp_path: Path) -> None:
        toml = tmp_path / "claude_runner.toml"
        toml.write_text('[claude]\nconfig_dir = "/somewhere"\n', encoding="utf-8")
        s = load_settings(toml)
        assert s.claude.config_dir == "/somewhere"
        assert len(s.accounts) == 1
        assert s.accounts[0].name == "default"
        assert s.accounts[0].config_dir == "/somewhere"

    def test_explicit_accounts_wins_over_legacy(self, tmp_path: Path) -> None:
        toml = tmp_path / "claude_runner.toml"
        toml.write_text(
            "\n".join(
                [
                    "[claude]",
                    'config_dir = "/legacy/should-be-ignored"',
                    "",
                    "[[accounts]]",
                    'name = "personal"',
                    'config_dir = "/p/dir"',
                    "",
                    "[[accounts]]",
                    'name = "work"',
                    'config_dir = "/w/dir"',
                    'linux_user = "bw"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        s = load_settings(toml)
        assert [a.name for a in s.accounts] == ["personal", "work"]
        assert s.accounts[1].linux_user == "bw"
        # Legacy field is preserved verbatim (doctor warns on conflict).
        assert s.claude.config_dir == "/legacy/should-be-ignored"

    def test_explicit_accounts_with_duplicate_name_rejected(self, tmp_path: Path) -> None:
        toml = tmp_path / "claude_runner.toml"
        toml.write_text(
            "\n".join(
                [
                    "[[accounts]]",
                    'name = "dup"',
                    'config_dir = "/a"',
                    "",
                    "[[accounts]]",
                    'name = "dup"',
                    'config_dir = "/b"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        with pytest.raises(ConfigError):
            load_settings(toml)

    def test_empty_accounts_list_with_no_legacy_works(self) -> None:
        """Synthesised account uses empty config_dir, which is valid."""
        s = load_settings(None)
        assert s.accounts[0].config_dir == ""

    def test_model_dump_round_trip(self) -> None:
        s = load_settings(None)
        dumped = s.model_dump()
        reloaded = Settings.model_validate(dumped)
        assert reloaded.accounts == s.accounts


class TestAccountPolicyDefaults:
    def test_default_policy_uses_documented_values(self) -> None:
        """Empty policy → max_concurrency=1, 40/60 daytime, 70/90 nighttime."""
        policy = AccountPolicy()
        assert policy.concurrency.max_concurrency == 1
        f = policy.throttle.five_hour
        assert f.daytime_band_full_dispatch_max_pct == 40
        assert f.daytime_band_slowdown_max_pct == 60
        assert f.nighttime_band_full_dispatch_max_pct == 70
        assert f.nighttime_band_slowdown_max_pct == 90
        assert policy.throttle.time_of_day.day_end == "21:00"


class TestPerAccountTomlPath:
    def test_empty_config_dir_returns_none(self) -> None:
        assert per_account_toml_path("") is None

    def test_set_config_dir_resolves_path(self, tmp_path: Path) -> None:
        path = per_account_toml_path(str(tmp_path))
        assert path == tmp_path / "runner-account.toml"


class TestLoadAccountPolicy:
    def test_empty_config_dir_returns_defaults(self) -> None:
        assert load_account_policy("") == AccountPolicy()

    def test_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        assert load_account_policy(str(tmp_path)) == AccountPolicy()

    def test_full_policy_loads(self, tmp_path: Path) -> None:
        (tmp_path / "runner-account.toml").write_text(
            "\n".join(
                [
                    "[concurrency]",
                    "max_concurrency = 5",
                    "",
                    "[throttle.five_hour]",
                    "daytime_band_full_dispatch_max_pct   = 30",
                    "daytime_band_slowdown_max_pct        = 50",
                    "nighttime_band_full_dispatch_max_pct = 65",
                    "nighttime_band_slowdown_max_pct      = 85",
                    "",
                    "[throttle.time_of_day]",
                    'day_end = "22:00"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        policy = load_account_policy(str(tmp_path))
        assert policy.concurrency.max_concurrency == 5
        assert policy.throttle.five_hour.daytime_band_full_dispatch_max_pct == 30
        assert policy.throttle.five_hour.nighttime_band_slowdown_max_pct == 85
        assert policy.throttle.time_of_day.day_end == "22:00"

    def test_partial_policy_fills_defaults(self, tmp_path: Path) -> None:
        """Setting only max_concurrency leaves throttle defaults intact."""
        (tmp_path / "runner-account.toml").write_text(
            "[concurrency]\nmax_concurrency = 3\n", encoding="utf-8"
        )
        policy = load_account_policy(str(tmp_path))
        assert policy.concurrency.max_concurrency == 3
        # Defaults preserved.
        assert policy.throttle.five_hour.daytime_band_full_dispatch_max_pct == 40
        assert policy.throttle.time_of_day.day_end == "21:00"

    def test_invalid_toml_raises_config_error(self, tmp_path: Path) -> None:
        (tmp_path / "runner-account.toml").write_text("not = valid = toml", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_account_policy(str(tmp_path))

    def test_unknown_field_rejected(self, tmp_path: Path) -> None:
        """extra='forbid' on AccountPolicy catches typos in per-account files."""
        (tmp_path / "runner-account.toml").write_text(
            "[concurrency]\nmax_concurrency = 2\nbogus_field = 1\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError):
            load_account_policy(str(tmp_path))


class TestResolveAccounts:
    def test_resolves_single_default_with_empty_policy(self) -> None:
        """Loader's synthesised legacy default → ResolvedAccount with default policy."""
        s = load_settings(None)
        resolved = resolve_accounts(s)
        assert len(resolved) == 1
        assert isinstance(resolved[0], ResolvedAccount)
        assert resolved[0].name == "default"
        assert resolved[0].policy == AccountPolicy()

    def test_resolves_two_accounts_with_per_account_files(self, tmp_path: Path) -> None:
        personal = tmp_path / "personal"
        personal.mkdir()
        (personal / "runner-account.toml").write_text(
            "[concurrency]\nmax_concurrency = 5\n", encoding="utf-8"
        )
        work = tmp_path / "work"
        work.mkdir()
        (work / "runner-account.toml").write_text(
            "[concurrency]\nmax_concurrency = 1\n", encoding="utf-8"
        )

        toml = tmp_path / "claude_runner.toml"
        toml.write_text(
            "\n".join(
                [
                    "[[accounts]]",
                    'name = "personal"',
                    f'config_dir = "{personal}"',
                    "",
                    "[[accounts]]",
                    'name = "work"',
                    f'config_dir = "{work}"',
                    'linux_user = "bw"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        s = load_settings(toml)
        resolved = resolve_accounts(s)
        assert [r.name for r in resolved] == ["personal", "work"]
        assert resolved[0].policy.concurrency.max_concurrency == 5
        assert resolved[1].policy.concurrency.max_concurrency == 1
        assert resolved[1].linux_user == "bw"

    def test_resolve_propagates_per_account_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad"
        bad.mkdir()
        (bad / "runner-account.toml").write_text("] not toml [", encoding="utf-8")
        toml = tmp_path / "claude_runner.toml"
        toml.write_text(
            "\n".join(["[[accounts]]", 'name = "bad"', f'config_dir = "{bad}"', ""]),
            encoding="utf-8",
        )
        s = load_settings(toml)
        with pytest.raises(ConfigError):
            resolve_accounts(s)
