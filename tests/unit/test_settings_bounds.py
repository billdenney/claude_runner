"""No duration setting is longer than ten years.

A finite value can be too large to use. ``[watchdog] restart_cooldown_s =
1e308`` loaded, and the watchdog's ``decide()`` then raised ``OverflowError``
from ``timedelta(seconds=1e308)``. An ``eow_time_switch`` with a 400-digit day
count made ``load_settings`` itself raise ``OverflowError``. Every duration
setting now has :data:`MAX_DURATION_S` (ten years) as its upper bound, and a
millisecond setting :data:`MAX_DURATION_MS`.

A duration setting is one whose name has a unit word: ``s`` or ``seconds``
(``poll_interval_s``, ``max_duration_s_per_task``) or ``ms``
(``capture_post_ready_pad_ms``). The gate loads each one set to its
ceiling, which must load unchanged, and to one more, which must fail with a
:class:`ConfigError` naming the key. A setting that another setting bounds
more tightly is listed, with the reason, in ``BOUNDED_BY_ANOTHER_SETTING``
and tested on its own. It covers both files an operator writes,
and a duration setting added later is covered without being listed. Every
float setting must be a duration by that rule, so a float added under another
name fails here until it is classified. The one duration string,
``eow_time_switch``, is tested directly. The last tests check that the
ceiling is safe for the code that reads the settings.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from functools import reduce
from pathlib import Path

import pytest
from pydantic import ValidationError

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.loader import ConfigError, load_settings
from claude_task_runner.config.schema import (
    MAX_DURATION_MS,
    MAX_DURATION_S,
    AccountPolicy,
    Settings,
    WatchdogSettings,
)
from claude_task_runner.cron.backoff import WatchdogState, WatchdogVerdict
from claude_task_runner.cron.backoff import decide as watchdog_decide
from claude_task_runner.cron.systemd_unit import _SYSTEMD_MAX_WHOLE_SECONDS
from claude_task_runner.supervisor.states import SupervisorState
from claude_task_runner.throttle import policy as policy_mod
from claude_task_runner.throttle.decision import FIVE_HOUR_LENGTH_S
from claude_task_runner.throttle.decision import decide as throttle_decide
from claude_task_runner.usage.models import UsageReading, WindowReading

from ._settings_walk import LOADERS, ROOTS, float_paths, toml_setting, walk_settings

CEILINGS = {"s": MAX_DURATION_S, "seconds": MAX_DURATION_S, "ms": MAX_DURATION_MS}
"""A unit word in a duration setting's name, and the largest value it may hold."""


def _ceiling(name: str) -> int | None:
    """The largest value the setting ``name`` may hold, or None if it is no duration.

    A duration setting names its unit as one of its words, last or not:
    ``poll_interval_s``, ``max_duration_s_per_task``, ``capture_post_ready_pad_ms``.
    """
    for word in reversed(name.split("_")):
        if word in CEILINGS:
            return CEILINGS[word]
    return None


def _value_at(loaded: object, path: str) -> object:
    """The value at the dotted ``path`` of a loaded settings model."""
    return reduce(getattr, path.split("."), loaded)


DURATIONS = [
    (file, field.path, ceiling)
    for file, root in ROOTS.items()
    for field in walk_settings(root)
    if (ceiling := _ceiling(field.name)) is not None
]

CASES = [
    pytest.param(file, path, ceiling, id=f"{file}:{path}") for file, path, ceiling in DURATIONS
]

BOUNDED_BY_ANOTHER_SETTING = {
    ("claude_runner.toml", "usage.poll_interval_s"): (
        "[usage].max_reading_age_s must span two capture cycles of "
        "len(accounts) x poll_interval_s and is itself at most ten years, so a "
        "poll interval over five years is rejected by that rule, not by its own "
        "bound. One more than the ceiling still fails on the field's own bound."
    ),
}
"""Duration settings whose ceiling alone cannot load, and why."""

CEILING_CASES = [
    pytest.param(file, path, ceiling, id=f"{file}:{path}")
    for file, path, ceiling in DURATIONS
    if (file, path) not in BOUNDED_BY_ANOTHER_SETTING
]


class TestEveryDurationIsAtMostTenYears:
    @pytest.mark.parametrize(("file", "path", "ceiling"), CEILING_CASES)
    def test_the_ceiling_loads(self, tmp_path: Path, file: str, path: str, ceiling: int) -> None:
        (tmp_path / file).write_text(toml_setting(path, str(ceiling)), encoding="utf-8")
        assert _value_at(LOADERS[file](tmp_path), path) == ceiling

    @pytest.mark.parametrize(("file", "path", "ceiling"), CASES)
    def test_one_more_fails_to_load(
        self, tmp_path: Path, file: str, path: str, ceiling: int
    ) -> None:
        (tmp_path / file).write_text(toml_setting(path, str(ceiling + 1)), encoding="utf-8")
        with pytest.raises(ConfigError) as excinfo:
            LOADERS[file](tmp_path)
        assert path in str(excinfo.value)
        cause = excinfo.value.__cause__
        assert isinstance(cause, ValidationError)
        assert [(e["type"], e["loc"], e["ctx"]) for e in cause.errors()] == [
            ("less_than_equal", tuple(path.split(".")), {"le": ceiling})
        ]

    def test_each_setting_bounded_by_another_is_a_duration(self) -> None:
        """An entry that no longer names a duration setting fails, so the list
        cannot outlive the rule that needs it."""
        durations = {(file, path) for file, path, _ in DURATIONS}
        assert set(BOUNDED_BY_ANOTHER_SETTING) <= durations

    def test_poll_interval_is_bounded_by_the_reading_age(self, tmp_path: Path) -> None:
        """Ten years alone fails the two-cycle rule; five years, with the
        reading age at its own ceiling, is the largest that loads."""
        toml = tmp_path / "claude_runner.toml"
        toml.write_text(toml_setting("usage.poll_interval_s", str(MAX_DURATION_S)))
        with pytest.raises(ConfigError, match="under two capture cycles"):
            load_settings(toml)
        toml.write_text(
            f"[usage]\npoll_interval_s = {MAX_DURATION_S // 2}\n"
            f"max_reading_age_s = {MAX_DURATION_S}\n"
        )
        assert load_settings(toml).usage.poll_interval_s == MAX_DURATION_S // 2

    def test_every_float_setting_is_a_duration(self) -> None:
        """A float setting named otherwise would escape the ceiling."""
        unnamed = [
            f"{file}: {path}"
            for file, root in ROOTS.items()
            for path in float_paths(root)
            if _ceiling(path.rpartition(".")[2]) is None
        ]
        assert unnamed == []

    def test_a_negative_affinity_ttl_fails_to_load(self, tmp_path: Path) -> None:
        """``0`` already means the affinity never expires; less means nothing."""
        toml = tmp_path / "claude_runner.toml"
        toml.write_text(toml_setting("dispatch.affinity_ttl_seconds", "-1"), encoding="utf-8")
        with pytest.raises(ConfigError) as excinfo:
            load_settings(toml)
        cause = excinfo.value.__cause__
        assert isinstance(cause, ValidationError)
        assert [(e["type"], e["loc"]) for e in cause.errors()] == [
            ("greater_than_equal", ("dispatch", "affinity_ttl_seconds"))
        ]

    def test_a_zero_affinity_ttl_loads(self, tmp_path: Path) -> None:
        toml = tmp_path / "claude_runner.toml"
        toml.write_text(toml_setting("dispatch.affinity_ttl_seconds", "0"), encoding="utf-8")
        assert load_settings(toml).dispatch.affinity_ttl_seconds == 0


@pytest.mark.parametrize("file", list(ROOTS))
class TestDurationStrings:
    """``[dispatch_pct.week].eow_time_switch``, in both files."""

    PATH = "dispatch_pct.week.eow_time_switch"

    def test_ten_years_loads(self, tmp_path: Path, file: str) -> None:
        (tmp_path / file).write_text(toml_setting(self.PATH, '"3650d"'), encoding="utf-8")
        assert _value_at(LOADERS[file](tmp_path), self.PATH) == "3650d"

    def test_a_second_more_fails_to_load(self, tmp_path: Path, file: str) -> None:
        (tmp_path / file).write_text(toml_setting(self.PATH, '"3650d 1s"'), encoding="utf-8")
        with pytest.raises(ConfigError) as excinfo:
            LOADERS[file](tmp_path)
        assert self.PATH in str(excinfo.value)
        assert "duration '3650d 1s' is longer than ten years (315360000 s)" in str(excinfo.value)

    @pytest.mark.parametrize("digits", [400, 5000])
    def test_a_day_count_too_long_for_a_number_fails_to_load(
        self, tmp_path: Path, file: str, digits: int
    ) -> None:
        """400 digits overflowed ``float()``; 5000 is past ``int()``'s digit limit."""
        days = "1" + "0" * (digits - 1)
        (tmp_path / file).write_text(toml_setting(self.PATH, f'"{days}d"'), encoding="utf-8")
        with pytest.raises(ConfigError) as excinfo:
            LOADERS[file](tmp_path)
        assert self.PATH in str(excinfo.value)
        assert f"duration '{days}d' is too long" in str(excinfo.value)


class TestInstrument:
    """The classification and the walk the gate runs on: a broken one passes everything."""

    @pytest.mark.parametrize(
        ("name", "ceiling"),
        [
            ("poll_interval_s", MAX_DURATION_S),
            # The unit need not be the last word.
            ("max_duration_s_per_task", MAX_DURATION_S),
            ("stuck_sleep_loop_kill_threshold_s", MAX_DURATION_S),
            ("affinity_ttl_seconds", MAX_DURATION_S),
            ("capture_post_ready_pad_ms", MAX_DURATION_MS),
            ("steady_state_reap_interval_ticks", None),
            ("max_tokens_per_task", None),
            ("eow_time_switch", None),
            ("timezone", None),
            ("status", None),
        ],
    )
    def test_classifies_a_setting_by_its_name(self, name: str, ceiling: int | None) -> None:
        assert _ceiling(name) == ceiling

    def test_the_ceilings(self) -> None:
        assert MAX_DURATION_S == 315_360_000
        assert MAX_DURATION_MS == 315_360_000_000

    def test_covers_each_kind_of_duration(self) -> None:
        """A float, an int and a millisecond setting, a unit mid-name, and the affinity TTL."""
        assert {
            ("claude_runner.toml", "watchdog.restart_cooldown_s", MAX_DURATION_S),
            ("claude_runner.toml", "task_caps.max_duration_s_per_task", MAX_DURATION_S),
            ("claude_runner.toml", "task_caps.stuck_sleep_loop_kill_threshold_s", MAX_DURATION_S),
            ("claude_runner.toml", "usage.capture_post_data_pad_ms", MAX_DURATION_MS),
            ("claude_runner.toml", "dispatch.affinity_ttl_seconds", MAX_DURATION_S),
        } <= set(DURATIONS)

    def test_reads_a_value_at_a_path(self) -> None:
        settings = load_settings(None)
        assert _value_at(settings, "watchdog.restart_cooldown_s") == 30
        assert _value_at(AccountPolicy(), "concurrency.max_concurrency") == 1


_NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
_TEN_YEARS = timedelta(seconds=MAX_DURATION_S)


def _reading(*, five_h_pct: int, five_h_resets_at: datetime | None) -> UsageReading:
    return UsageReading(
        captured_at=_NOW,
        five_hour=WindowReading(
            utilization_pct=five_h_pct, resets_at_raw="x", resets_at=five_h_resets_at
        ),
        seven_day=WindowReading(
            utilization_pct=0, resets_at_raw="y", resets_at=_NOW + timedelta(days=3)
        ),
    )


class TestTheCeilingIsSafe:
    """Each reader that overflowed on a huge value accepts the ceiling."""

    def test_it_is_under_the_limits_of_sleeps_waits_and_systemd(self) -> None:
        # time.sleep, Event.wait and Thread.join refuse more than TIMEOUT_MAX.
        assert MAX_DURATION_S < threading.TIMEOUT_MAX
        assert MAX_DURATION_S < _SYSTEMD_MAX_WHOLE_SECONDS

    def test_the_watchdog_cools_down_for_ten_years(self) -> None:
        settings = WatchdogSettings(
            restart_cooldown_s=MAX_DURATION_S,
            restart_backoff_max_s=MAX_DURATION_S,
            crash_loop_threshold=5,
        )
        last = _NOW - timedelta(seconds=5)
        decision = watchdog_decide(
            state=WatchdogState(recent_restarts=[last]),
            supervisor_alive=False,
            settings=settings,
            clock=FakeClock(_NOW),
        )
        assert decision.verdict is WatchdogVerdict.COOLDOWN
        assert decision.next_check_at == last + _TEN_YEARS

    def test_the_watchdog_backs_off_for_ten_years(self) -> None:
        settings = WatchdogSettings(
            restart_cooldown_s=MAX_DURATION_S,
            restart_backoff_max_s=MAX_DURATION_S,
            crash_loop_threshold=5,
        )
        restarts = [_NOW - timedelta(seconds=50 - 10 * i) for i in range(5)]
        decision = watchdog_decide(
            state=WatchdogState(recent_restarts=restarts),
            supervisor_alive=False,
            settings=settings,
            clock=FakeClock(_NOW),
        )
        assert decision.verdict is WatchdogVerdict.BACKOFF
        assert decision.next_check_at == restarts[-1] + _TEN_YEARS

    @pytest.mark.parametrize(
        ("resets_at", "wakeup"),
        [
            (_NOW + timedelta(hours=2), _NOW + timedelta(hours=2) + _TEN_YEARS),
            (None, _NOW + timedelta(seconds=FIVE_HOUR_LENGTH_S) + _TEN_YEARS),
        ],
        ids=["reset-known", "reset-unknown"],
    )
    def test_the_throttle_sleeps_ten_years_past_the_reset(
        self, resets_at: datetime | None, wakeup: datetime
    ) -> None:
        """``[usage].poll_interval_s`` and ``[supervisor].window_start_delay_s``
        at the ceiling, with 5h utilization past any stop band."""
        resolved = policy_mod.resolve(load_settings(None), AccountPolicy(), "default")
        decision = throttle_decide(
            resolved,
            _reading(five_h_pct=100, five_h_resets_at=resets_at),
            FakeClock(_NOW),
            poll_interval_s=MAX_DURATION_S,
            window_start_delay_s=MAX_DURATION_S,
        )
        assert decision.state is SupervisorState.THROTTLED_5H
        assert decision.wakeup_at == wakeup


def test_settings_model_is_the_queue_root() -> None:
    """Guards the gate's scope: the queue file loads into :class:`Settings`."""
    assert ROOTS["claude_runner.toml"] is Settings
