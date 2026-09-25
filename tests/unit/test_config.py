"""Tests for the config loader and schema validation."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from claude_task_runner.config.loader import (
    ConfigError,
    _deep_merge,
    load_defaults,
    load_settings,
)
from claude_task_runner.config.schema import WorktreeReclaimSettings


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


class TestWorktreeReclaimSettings:
    """ADR-0034 ``[worktree_reclaim]``: defaults, overrides, and the validators
    that keep an unsafe value from ever reaching a ``git`` argv."""

    def test_package_toml_mirrors_the_model_defaults(self) -> None:
        # The defaults live in two places (model + package TOML) so a queue
        # TOML without the section parses; this gate stops them drifting.
        assert load_settings(None).worktree_reclaim == WorktreeReclaimSettings()

    def test_defaults(self) -> None:
        s = load_settings(None).worktree_reclaim
        assert s.periodic is False
        assert s.interval_s == 3600.0
        assert s.max_per_pass == 20
        assert (s.remote, s.parent_branch) == ("origin", "main")
        assert s.branch_template == "claude/{task_id}"
        assert s.discardable_untracked == ["tests/testthat/_problems/"]
        assert s.lock_file == ""
        assert s.lock_timeout_s == 60.0
        assert s.git_timeout_s == 300.0

    def test_override(self, tmp_path: Path) -> None:
        toml = tmp_path / "claude_runner.toml"
        toml.write_text(
            "[worktree_reclaim]\n"
            "periodic = true\n"
            'lock_file = ".run/setup_worktree.lock"\n'
            "discardable_untracked = []\n"
            'parent_branch = "release/1.x"\n'
        )
        s = load_settings(toml).worktree_reclaim
        assert s.periodic is True
        assert s.lock_file == ".run/setup_worktree.lock"
        assert s.discardable_untracked == []
        assert s.parent_branch == "release/1.x"
        assert s.remote == "origin"  # untouched keys keep their default

    def test_unknown_key_rejected(self, tmp_path: Path) -> None:
        toml = tmp_path / "claude_runner.toml"
        toml.write_text("[worktree_reclaim]\nenabled = true\n")
        with pytest.raises(ConfigError, match="validation failed"):
            load_settings(toml)

    @pytest.mark.parametrize("template", ["claude/{task_id}", "{task_id}", "wt/{worktree_name}"])
    def test_branch_template_accepted(self, template: str) -> None:
        assert WorktreeReclaimSettings(branch_template=template).branch_template == template

    @pytest.mark.parametrize(
        ("template", "message"),
        [
            ("main", "must contain {task_id} or {worktree_name}"),
            ("claude/{task}", "unknown placeholder 'task'"),
            ("claude/{task_id", "not a valid template"),
            ("-{task_id}", "not a safe git ref/remote name"),
            ("claude/{task_id}.lock", "not a safe git ref/remote name"),
            ("claude/{task_id} x", "not a safe git ref/remote name"),
        ],
    )
    def test_branch_template_rejected(self, template: str, message: str) -> None:
        with pytest.raises(ValidationError, match=re.escape(message)):
            WorktreeReclaimSettings(branch_template=template)

    def test_render_branch(self) -> None:
        s = WorktreeReclaimSettings(branch_template="{worktree_name}/{task_id}")
        assert s.render_branch(task_id="t-1", worktree_name="wt") == "wt/t-1"

    @pytest.mark.parametrize("field", ["remote", "parent_branch"])
    @pytest.mark.parametrize(
        "value", ["", "-upload-pack=x", "a b", "a..b", "a//b", "main/", "x.lock"]
    )
    def test_git_names_rejected(self, field: str, value: str) -> None:
        with pytest.raises(ValidationError, match="not a safe git ref/remote name"):
            WorktreeReclaimSettings(**{field: value})

    @pytest.mark.parametrize(
        "entry", ["tests/testthat/_problems/", "Rplots.pdf", "build", ".Rcheck/"]
    )
    def test_discardable_entries_accepted(self, entry: str) -> None:
        assert WorktreeReclaimSettings(discardable_untracked=[entry]).discardable_untracked == [
            entry
        ]

    @pytest.mark.parametrize(
        "entry", ["", "/", ".", "./", "/abs/path", "../up", "a/../b", " padded", "a\\b"]
    )
    def test_discardable_entries_rejected(self, entry: str) -> None:
        # Each of these would widen "discard these paths" to "discard
        # anything" or point outside the worktree.
        with pytest.raises(ValidationError, match="must be a non-empty path"):
            WorktreeReclaimSettings(discardable_untracked=[entry])

    @pytest.mark.parametrize(
        ("field", "value"),
        [("interval_s", 0), ("max_per_pass", 0), ("lock_timeout_s", 0), ("git_timeout_s", -1)],
    )
    def test_non_positive_limits_rejected(self, field: str, value: float) -> None:
        with pytest.raises(ValidationError):
            WorktreeReclaimSettings(**{field: value})
