"""Each reaper threshold spans two of the write intervals it is measured against.

The silent-orphan reaper compares ``[task_caps]`` thresholds with the age of
timestamps the dispatcher writes at most once an interval: the monitor's
``dispatcher_alive_at`` and the agent's persisted ``last_heartbeat_at``.
:data:`REAPER_THRESHOLD_FLOORS` relates each threshold to its interval, and
the loader rejects a threshold under twice it. Every ``[task_caps]`` field
must be in that table, as a threshold or an interval, or in
``NOT_MEASURED_AGAINST_A_WRITE`` with the reason, so a field added later
fails here until it is classified.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from claude_task_runner.config.loader import ConfigError, load_settings
from claude_task_runner.config.schema import REAPER_THRESHOLD_FLOORS, TaskCapsSettings

THRESHOLDS = [threshold for threshold, _, _ in REAPER_THRESHOLD_FLOORS]
INTERVALS = {interval for _, interval, _ in REAPER_THRESHOLD_FLOORS}

NOT_MEASURED_AGAINST_A_WRITE = {
    "max_tokens_per_task": "a token count, checked against the stream's usage",
    "max_duration_s_per_task": "measured from the attempt's start, written once",
    "zombie_verify_fs_activity_window_s": (
        "compared with the mtimes of files the worker writes, not the dispatcher"
    ),
    "steady_state_reap_interval_ticks": "how often the reaper runs, not what it measures",
    "bash_poll_antipattern_kill": "a switch, not a threshold",
}
"""``[task_caps]`` fields that are no threshold the reaper compares with the
age of a timestamp the dispatcher writes once an interval, and why."""

FLOOR_CASES = [
    pytest.param(threshold, interval, why, id=threshold)
    for threshold, interval, why in REAPER_THRESHOLD_FLOORS
]


def _toml(tmp_path: Path, **task_caps: float) -> Path:
    toml = tmp_path / "claude_runner.toml"
    toml.write_text(
        "[task_caps]\n" + "".join(f"{key} = {value}\n" for key, value in task_caps.items())
    )
    return toml


class TestEveryFieldIsClassified:
    def test_every_task_caps_field_is_a_threshold_an_interval_or_listed(self) -> None:
        classified = [*THRESHOLDS, *INTERVALS, *NOT_MEASURED_AGAINST_A_WRITE]
        assert sorted(classified) == sorted(TaskCapsSettings.model_fields)

    def test_each_threshold_has_one_floor(self) -> None:
        assert len(THRESHOLDS) == len(set(THRESHOLDS))

    def test_each_floor_relates_two_duration_fields(self) -> None:
        """A threshold and its interval are both seconds, so their ratio is the rule."""
        for threshold, interval, _ in REAPER_THRESHOLD_FLOORS:
            assert threshold.endswith("_s")
            assert interval.endswith("_s")


class TestThresholdFloors:
    @pytest.mark.parametrize(("threshold", "interval", "why"), FLOOR_CASES)
    def test_exactly_two_intervals_loads(
        self, tmp_path: Path, threshold: str, interval: str, why: str
    ) -> None:
        settings = load_settings(_toml(tmp_path, **{interval: 40, threshold: 80}))
        assert getattr(settings.task_caps, threshold) == 80
        assert getattr(settings.task_caps, interval) == 40

    @pytest.mark.parametrize(("threshold", "interval", "why"), FLOOR_CASES)
    def test_under_two_intervals_is_rejected(
        self, tmp_path: Path, threshold: str, interval: str, why: str
    ) -> None:
        with pytest.raises(ConfigError) as excinfo:
            load_settings(_toml(tmp_path, **{interval: 40, threshold: 79}))
        assert (
            f"[task_caps].{threshold} = 79 s is under two write intervals: "
            f"[task_caps].{interval} 40 s x 2 = 80 s. {why} Raise {threshold} or "
            f"lower {interval}."
        ) in str(excinfo.value)

    @pytest.mark.parametrize(("threshold", "interval", "why"), FLOOR_CASES)
    def test_the_model_rejects_it_too(self, threshold: str, interval: str, why: str) -> None:
        """Code that builds the settings directly is held to the same rule."""
        loaded = load_settings(None).task_caps.model_dump()
        with pytest.raises(ValidationError, match=f"{threshold} = 79 s is under two"):
            TaskCapsSettings(**{**loaded, interval: 40, threshold: 79})

    def test_the_alert_message(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as excinfo:
            load_settings(_toml(tmp_path, heartbeat_silence_alert_s=59.5))
        assert (
            "[task_caps].heartbeat_silence_alert_s = 59.5 s is under two write intervals: "
            "[task_caps].dispatcher_alive_write_interval_s 30 s x 2 = 60 s. The reaper "
            "counts a task as alive while its monitor's tick, written once an interval, is "
            "within heartbeat_silence_alert_s, so a shorter window reads a running task as "
            "silent when one tick is late. Raise heartbeat_silence_alert_s or lower "
            "dispatcher_alive_write_interval_s."
        ) in str(excinfo.value)

    def test_the_packaged_defaults_meet_every_floor(self) -> None:
        caps = load_settings(None).task_caps
        assert caps.heartbeat_silence_alert_s == 300
        assert caps.dispatcher_alive_write_interval_s == 30
        assert caps.heartbeat_silence_kill_s == 0
        assert caps.stuck_sleep_loop_kill_threshold_s == 600
        assert caps.heartbeat_persist_interval_s == 30

    def test_the_default_alert_rejects_a_slower_monitor(self, tmp_path: Path) -> None:
        """The 300 s default needs a monitor tick of 150 s or less."""
        assert load_settings(_toml(tmp_path, dispatcher_alive_write_interval_s=150))
        with pytest.raises(ConfigError, match=r"heartbeat_silence_alert_s = 300 s is under two"):
            load_settings(_toml(tmp_path, dispatcher_alive_write_interval_s=151))

    def test_the_default_stuck_threshold_rejects_a_slower_persist(self, tmp_path: Path) -> None:
        """The 600 s default needs heartbeat writes of 300 s or less."""
        assert load_settings(_toml(tmp_path, heartbeat_persist_interval_s=300))
        with pytest.raises(
            ConfigError, match=r"stuck_sleep_loop_kill_threshold_s = 600 s is under two"
        ):
            load_settings(_toml(tmp_path, heartbeat_persist_interval_s=301))

    def test_a_kill_threshold_of_zero_is_off(self, tmp_path: Path) -> None:
        settings = load_settings(
            _toml(tmp_path, heartbeat_silence_kill_s=0, heartbeat_persist_interval_s=300)
        )
        assert settings.task_caps.heartbeat_silence_kill_s == 0

    def test_the_first_floor_broken_is_the_one_named(self, tmp_path: Path) -> None:
        """Two broken at once: the error names the first in the table."""
        with pytest.raises(ConfigError) as excinfo:
            load_settings(
                _toml(tmp_path, heartbeat_silence_alert_s=59, heartbeat_silence_kill_s=59)
            )
        assert "heartbeat_silence_alert_s = 59 s is under two" in str(excinfo.value)
        assert "heartbeat_silence_kill_s = 59 s" not in str(excinfo.value)
