"""No float setting accepts ``inf``, ``-inf`` or ``nan``.

TOML can write all three, and reads ``1e309`` as ``inf``. pydantic accepts
them in a float field unless the model says otherwise, and ``inf`` passes a
``gt=0`` bound. So ``[watchdog] restart_cooldown_s = inf`` loaded, and the
watchdog's ``decide()`` then raised ``OverflowError`` from
``timedelta(seconds=inf)``. ``[dispatch].affinity_ttl_seconds`` has no bound
and took all three. Every settings model now inherits
``allow_inf_nan=False`` from ``_StrictModel``. A setting that has an
"unlimited" value spells it ``0``.

The gate writes a TOML that sets one float field to one non-finite value,
loads it with the loader for that file, and requires a :class:`ConfigError`
that names the key and rejects the value as non-finite. It does this for
every float field under both files an operator writes. The fields come from
the schema walk, so a float setting added later is covered without being
listed here. A walk that found nothing would generate no cases and pass, so
the known-answer tests check the walk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, Field, PositiveFloat, ValidationError

from claude_task_runner.config.loader import (
    PER_ACCOUNT_TOML_NAME,
    ConfigError,
    load_account_policy,
    load_settings,
)
from claude_task_runner.config.schema import AccountPolicy, Settings

from ._settings_walk import LOADERS, ROOTS, float_paths, holds_float, toml_setting

NON_FINITE = ("inf", "-inf", "nan")
"""The three non-finite values, as TOML writes them."""


def _assert_rejects_non_finite(error: ConfigError, path: str) -> None:
    """``error`` names ``path`` and rejects its value as non-finite, and nothing else.

    A union field reports once per member, a list or dict field per item,
    so an error may sit below ``path``, never beside it.
    """
    assert path in str(error)
    cause = error.__cause__
    assert isinstance(cause, ValidationError), f"not a schema error: {error}"
    loc = tuple(path.split("."))
    problems = [(problem["type"], problem["loc"][: len(loc)]) for problem in cause.errors()]
    assert problems, str(error)
    assert all(problem == ("finite_number", loc) for problem in problems), problems


CASES = [
    pytest.param(file, path, value, id=f"{file}:{path}={value}")
    for file, root in ROOTS.items()
    for path in float_paths(root)
    for value in NON_FINITE
]


class TestEveryFloatSettingIsFinite:
    @pytest.mark.parametrize(("file", "path", "value"), CASES)
    def test_a_non_finite_value_fails_to_load(
        self, tmp_path: Path, file: str, path: str, value: str
    ) -> None:
        (tmp_path / file).write_text(toml_setting(path, value), encoding="utf-8")
        with pytest.raises(ConfigError) as excinfo:
            LOADERS[file](tmp_path)
        _assert_rejects_non_finite(excinfo.value, path)


class _Seconds(float):
    """A float subclass, which a field could be annotated with."""


class _Leaf(BaseModel):
    seconds: float
    count: int


class _Tree(BaseModel):
    top: float
    maybe: float | None = None
    whole: int
    name: str
    branch: _Leaf
    same_model_again: _Leaf | None = None


class _PolicyWithFloat(AccountPolicy):
    """``AccountPolicy`` plus the float field it does not have yet."""

    probe_s: float = 1.0


class TestInstrument:
    """The walk and the checks the gate runs on it: a broken one passes everything."""

    @pytest.mark.parametrize(
        "annotation",
        [
            float,
            float | None,
            int | float,
            list[float],
            dict[str, float],
            Annotated[float, Field(gt=0)],
            PositiveFloat,
            _Seconds,
        ],
    )
    def test_float_annotations_hold_a_float(self, annotation: Any) -> None:
        assert holds_float(annotation)

    @pytest.mark.parametrize(
        "annotation",
        [
            int,
            bool,
            str,
            int | None,
            list[int],
            dict[str, list[str]],
            Literal["tty", "api"],
            _Leaf,
        ],
    )
    def test_other_annotations_do_not(self, annotation: Any) -> None:
        assert not holds_float(annotation)

    def test_finds_exactly_the_float_fields_of_a_known_tree(self) -> None:
        # _Leaf is walked once, under the shorter of its two paths.
        assert float_paths(_Tree) == ["top", "maybe", "branch.seconds"]

    def test_finds_float_settings_in_every_section_that_has_one(self) -> None:
        assert {
            "usage.poll_interval_s",
            "failure_classifier.deferral_recheck_cooldown_s",
            "task_caps.max_duration_s_per_task",
            "watchdog.restart_cooldown_s",
            "supervisor.window_start_delay_s",
            "hooks.pre_dispatch_timeout_s",
            "worktree_reclaim.git_timeout_s",
            "dispatch.affinity_ttl_seconds",
        } <= set(float_paths(Settings))

    def test_leaves_out_integer_settings(self) -> None:
        paths = float_paths(Settings)
        assert "watchdog.crash_loop_threshold" not in paths
        # An int, despite the unit suffix.
        assert "task_caps.stuck_sleep_loop_kill_threshold_s" not in paths

    def test_the_gate_runs_the_reported_case(self) -> None:
        ids = {case.id for case in CASES}
        assert "claude_runner.toml:watchdog.restart_cooldown_s=inf" in ids
        assert "claude_runner.toml:dispatch.affinity_ttl_seconds=nan" in ids

    def test_there_is_a_loader_for_each_operator_file(self) -> None:
        assert LOADERS.keys() == ROOTS.keys()

    def test_writes_a_key_in_nested_tables(self) -> None:
        assert toml_setting("watchdog.restart_cooldown_s", "inf") == (
            "[watchdog]\nrestart_cooldown_s = inf\n"
        )
        assert toml_setting("dispatch_pct.day.x", "nan") == "[dispatch_pct.day]\nx = nan\n"
        assert toml_setting("probe_s", "-inf") == "probe_s = -inf\n"

    @pytest.mark.parametrize("value", ["+inf", "-nan", "1e309"])
    def test_other_spellings_of_non_finite_fail_too(self, tmp_path: Path, value: str) -> None:
        """TOML reads these as the same three values; ``1e309`` overflows to ``inf``."""
        toml = tmp_path / "claude_runner.toml"
        toml.write_text(toml_setting("watchdog.restart_cooldown_s", value), encoding="utf-8")
        with pytest.raises(ConfigError) as excinfo:
            load_settings(toml)
        _assert_rejects_non_finite(excinfo.value, "watchdog.restart_cooldown_s")

    def test_the_per_account_loader_rejects_a_non_finite_float(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No per-account setting is a float yet, so the gate runs no case
        through ``load_account_policy``. This is the case it will run."""
        monkeypatch.setattr("claude_task_runner.config.loader.AccountPolicy", _PolicyWithFloat)
        (tmp_path / PER_ACCOUNT_TOML_NAME).write_text(
            toml_setting("probe_s", "inf"), encoding="utf-8"
        )
        with pytest.raises(ConfigError) as excinfo:
            load_account_policy(str(tmp_path))
        _assert_rejects_non_finite(excinfo.value, "probe_s")

    def test_a_finite_value_still_loads(self, tmp_path: Path) -> None:
        """The check rejects the value, not the key or the TOML around it."""
        (tmp_path / "claude_runner.toml").write_text(
            toml_setting("watchdog.restart_cooldown_s", "45.5"), encoding="utf-8"
        )
        settings = LOADERS["claude_runner.toml"](tmp_path)
        assert isinstance(settings, Settings)
        assert settings.watchdog.restart_cooldown_s == 45.5

    def test_the_assertion_rejects_a_different_error(self, tmp_path: Path) -> None:
        """A load that fails for another reason must not count as the gate passing."""
        toml = tmp_path / "claude_runner.toml"
        toml.write_text(toml_setting("watchdog.restart_cooldown_s", "-1"), encoding="utf-8")
        with pytest.raises(ConfigError) as excinfo:
            load_settings(toml)
        with pytest.raises(AssertionError):
            _assert_rejects_non_finite(excinfo.value, "watchdog.restart_cooldown_s")
