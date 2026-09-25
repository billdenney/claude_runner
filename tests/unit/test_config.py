"""Tests for the config loader and schema validation."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from claude_task_runner.config.loader import (
    _RETIRED_KEYS,
    ConfigError,
    _deep_merge,
    _has_path,
    _reject_retired_keys,
    _retired_key_shown,
    load_defaults,
    load_settings,
)
from claude_task_runner.config.schema import Settings


class TestDeepMerge:
    def test_overrides_scalar(self) -> None:
        assert _deep_merge({"a": 1}, {"a": 2}) == {"a": 2}

    def test_recurses_nested_dicts(self) -> None:
        base = {"x": {"a": 1, "b": 2}}
        ovr = {"x": {"b": 99}}
        assert _deep_merge(base, ovr) == {"x": {"a": 1, "b": 99}}

    def test_lists_are_replaced_not_merged(self) -> None:
        base = {"items": [1, 2, 3]}
        ovr = {"items": [4]}
        assert _deep_merge(base, ovr) == {"items": [4]}

    def test_unrelated_keys_kept(self) -> None:
        base = {"a": 1, "b": 2}
        ovr = {"c": 3}
        assert _deep_merge(base, ovr) == {"a": 1, "b": 2, "c": 3}


class TestLoadDefaults:
    def test_loads_without_error(self) -> None:
        defaults = load_defaults()
        assert "usage" in defaults
        assert "dispatch_pct" in defaults
        assert defaults["dispatch_pct"]["day"]["fivehr_slowdown_pct"] == 40


class TestLoadSettings:
    def test_no_override(self) -> None:
        s = load_settings(None)
        assert s.dispatch_pct.day.fivehr_slowdown_pct == 40
        assert s.dispatch_pct.day.fivehr_stop_pct == 60
        assert s.usage.poll_interval_s == 60.0
        assert s.session.max_resume_attempts == 3

    def test_legacy_throttle_block_rejected(self, tmp_path: Path) -> None:
        """ADR-0022 retired ``[throttle.*]``; the loader raises with a
        migration hint so operators don't silently lose safety floors."""
        toml = tmp_path / "claude_runner.toml"
        toml.write_text(
            "[throttle.five_hour]\nband_full_dispatch_max_pct = 75\nband_slowdown_max_pct = 85\n"
        )
        with pytest.raises(ConfigError, match=r"\[throttle\.\*\]"):
            load_settings(toml)

    def test_override_replaces_value(self, tmp_path: Path) -> None:
        toml = tmp_path / "claude_runner.toml"
        toml.write_text("[dispatch_pct.day]\nfivehr_slowdown_pct = 75\nfivehr_stop_pct     = 85\n")
        s = load_settings(toml)
        assert s.dispatch_pct.day.fivehr_slowdown_pct == 75
        assert s.dispatch_pct.day.fivehr_stop_pct == 85
        # Untouched section keeps default
        assert s.session.max_resume_attempts == 3

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_settings(tmp_path / "nope.toml")

    def test_invalid_toml_raises(self, tmp_path: Path) -> None:
        toml = tmp_path / "bad.toml"
        toml.write_text("not = valid = toml = at all\n")
        with pytest.raises(ConfigError, match="Invalid TOML"):
            load_settings(toml)

    def test_unknown_key_rejected(self, tmp_path: Path) -> None:
        toml = tmp_path / "extra.toml"
        toml.write_text("[usage]\nthere_is_no_such_key = 99\n")
        with pytest.raises(ConfigError, match="validation failed"):
            load_settings(toml)

    def test_out_of_range_value_rejected(self, tmp_path: Path) -> None:
        toml = tmp_path / "bad_range.toml"
        toml.write_text(
            "[dispatch_pct.day]\nfivehr_slowdown_pct = 250\n"  # > 100
        )
        with pytest.raises(ConfigError, match="validation failed"):
            load_settings(toml)

    def test_queue_section_defaults_to_empty_template(self) -> None:
        """ADR-0023: ``[queue].working_dir_template`` is empty by default
        so existing queues whose claude_runner.toml predates the section
        keep writing ``working_dir: null`` from ``queue add``."""
        s = load_settings(None)
        assert s.queue.working_dir_template == ""

    def test_queue_section_override(self, tmp_path: Path) -> None:
        toml = tmp_path / "claude_runner.toml"
        toml.write_text('[queue]\nworking_dir_template = "/repo/.worktrees/{task_id}"\n')
        s = load_settings(toml)
        assert s.queue.working_dir_template == "/repo/.worktrees/{task_id}"

    def test_queue_unknown_key_rejected(self, tmp_path: Path) -> None:
        toml = tmp_path / "claude_runner.toml"
        toml.write_text("[queue]\nbogus_key = 1\n")
        with pytest.raises(ConfigError, match="validation failed"):
            load_settings(toml)


def _toml_setting(path: tuple[str, ...]) -> str:
    """The smallest TOML that sets ``path``: a key in its table, or an empty table."""
    if len(path) == 1:
        return f"[{path[0]}]\n"
    return f"[{'.'.join(path[:-1])}]\n{path[-1]} = 1\n"


class TestRetiredKeys:
    """Settings removed because nothing read them fail loudly, with the fix."""

    @pytest.mark.parametrize("path", list(_RETIRED_KEYS), ids=_retired_key_shown)
    def test_each_retired_key_is_rejected(self, path: tuple[str, ...], tmp_path: Path) -> None:
        toml = tmp_path / "claude_runner.toml"
        toml.write_text(_toml_setting(path))
        with pytest.raises(ConfigError) as excinfo:
            load_settings(toml)
        message = str(excinfo.value)
        assert message.startswith(f"{toml}: delete these retired settings.")
        assert f"  {_retired_key_shown(path)}: {_RETIRED_KEYS[path]}" in message

    def test_the_plan_line_first_time_setup_used_to_suggest(self, tmp_path: Path) -> None:
        toml = tmp_path / "claude_runner.toml"
        toml.write_text('[claude]\nplan = "max20x"\n')
        with pytest.raises(ConfigError, match=re.escape("[claude].plan: selected a [plans.*]")):
            load_settings(toml)

    def test_an_empty_value_is_still_rejected(self, tmp_path: Path) -> None:
        # "" was the schema default; the key itself is what is retired.
        toml = tmp_path / "claude_runner.toml"
        toml.write_text('[claude]\nplan = ""\n')
        with pytest.raises(ConfigError, match=re.escape("[claude].plan")):
            load_settings(toml)

    def test_a_plans_subtable_is_rejected(self, tmp_path: Path) -> None:
        toml = tmp_path / "claude_runner.toml"
        toml.write_text("[plans.max20x]\nfive_hour_tokens = 1\nweekly_tokens = 2\n")
        with pytest.raises(ConfigError, match=re.escape("[plans.*]: token budgets")):
            load_settings(toml)

    def test_every_retired_key_is_named_in_one_error(self) -> None:
        payload: dict[str, Any] = {}
        for path in _RETIRED_KEYS:
            node = payload
            for key in path[:-1]:
                node = node.setdefault(key, {})
            node[path[-1]] = {} if len(path) == 1 else 1
        with pytest.raises(ConfigError) as excinfo:
            _reject_retired_keys(payload, "q.toml")
        lines = str(excinfo.value).splitlines()[1:]
        assert lines == [
            f"  {_retired_key_shown(path)}: {reason}" for path, reason in _RETIRED_KEYS.items()
        ]

    def test_live_keys_in_the_same_table_still_load(self, tmp_path: Path) -> None:
        # The guard matches exact paths: [claude] itself is live.
        toml = tmp_path / "claude_runner.toml"
        toml.write_text('[claude]\nexecutable = "claude-dev"\n')
        assert load_settings(toml).claude.executable == "claude-dev"

    def test_a_scalar_where_a_table_belongs_is_left_to_validation(self, tmp_path: Path) -> None:
        toml = tmp_path / "claude_runner.toml"
        toml.write_text('claude = "max20x"\n')
        with pytest.raises(ConfigError, match="validation failed"):
            load_settings(toml)

    def test_has_path(self) -> None:
        assert _has_path({"claude": {"plan": ""}}, ("claude", "plan"))
        assert _has_path({"plans": {}}, ("plans",))
        assert not _has_path({"claude": {}}, ("claude", "plan"))
        assert not _has_path({"claude": "max20x"}, ("claude", "plan"))
        assert not _has_path({}, ("plans",))

    def test_shown_forms(self) -> None:
        assert _retired_key_shown(("claude", "plan")) == "[claude].plan"
        assert _retired_key_shown(("plans",)) == "[plans.*]"
        assert _retired_key_shown(("ema", "priors", "tokens")) == "[ema.priors].tokens"

    def test_no_retired_key_is_still_in_the_schema(self) -> None:
        # Otherwise the guard would reject a key the runtime still honours.
        for path in _RETIRED_KEYS:
            model: Any = Settings
            for key in path[:-1]:
                model = model.model_fields[key].annotation
            assert path[-1] not in model.model_fields, _retired_key_shown(path)

    def test_the_defaults_set_no_retired_key(self) -> None:
        defaults = load_defaults()
        assert [path for path in _RETIRED_KEYS if _has_path(defaults, path)] == []
