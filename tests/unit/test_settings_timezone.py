"""``[dispatch_pct].timezone`` must name a zone this system's database has.

Neither the queue-wide ``timezone`` nor an account's override was checked.
``load_settings`` accepted ``"Not/AZone"``, and the throttle's ``decide()``
then raised ``ZoneInfoNotFoundError`` from ``throttle.time_of_day.to_local``.
``decide()`` also runs on idle ticks, so the supervisor stopped at its first
clean poll and restarted into the same crash. Both files now check the name
when they load, by building the zone the way ``to_local`` does.
"""

from __future__ import annotations

import zoneinfo
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from claude_task_runner.config.loader import ConfigError, load_account_policy, load_settings
from claude_task_runner.config.schema import AccountPolicy, Settings
from claude_task_runner.throttle.time_of_day import to_local

from ._settings_walk import LOADERS, ROOTS, toml_setting

PATH = "dispatch_pct.timezone"

KNOWN = ["UTC", "America/New_York", "Etc/GMT+5"]
UNKNOWN = [
    "Not/AZone",
    # Zone names are case-sensitive, and spaces are part of the name.
    "utc",
    " UTC",
    # Not a zone key at all: a directory, a path, a data file.
    "America/",
    "/etc/localtime",
    "../etc/passwd",
    "zone.tab",
]


@pytest.mark.parametrize("file", list(ROOTS))
class TestTheTimezoneIsChecked:
    """In ``claude_runner.toml`` and in each account's ``runner-account.toml``."""

    @pytest.mark.parametrize("name", KNOWN)
    def test_a_known_zone_loads(self, tmp_path: Path, file: str, name: str) -> None:
        (tmp_path / file).write_text(toml_setting(PATH, f'"{name}"'), encoding="utf-8")
        loaded = LOADERS[file](tmp_path)
        assert isinstance(loaded, Settings | AccountPolicy)
        assert loaded.dispatch_pct.timezone == name

    def test_empty_loads_as_the_system_local_time(self, tmp_path: Path, file: str) -> None:
        (tmp_path / file).write_text(toml_setting(PATH, '""'), encoding="utf-8")
        loaded = LOADERS[file](tmp_path)
        assert isinstance(loaded, Settings | AccountPolicy)
        assert loaded.dispatch_pct.timezone == ""

    @pytest.mark.parametrize("name", UNKNOWN)
    def test_an_unknown_zone_fails_to_load(self, tmp_path: Path, file: str, name: str) -> None:
        (tmp_path / file).write_text(toml_setting(PATH, f'"{name}"'), encoding="utf-8")
        with pytest.raises(ConfigError) as excinfo:
            LOADERS[file](tmp_path)
        message = str(excinfo.value)
        assert PATH in message
        assert f"[dispatch_pct].timezone = {name!r} is not an IANA time zone name" in message
        assert "Leave it empty for the system's local time." in message
        cause = excinfo.value.__cause__
        assert isinstance(cause, ValidationError)
        assert [(e["type"], e["loc"]) for e in cause.errors()] == [
            ("value_error", ("dispatch_pct", "timezone"))
        ]


def test_an_absent_queue_timezone_is_the_system_local_time(tmp_path: Path) -> None:
    toml = tmp_path / "claude_runner.toml"
    toml.write_text("", encoding="utf-8")
    assert load_settings(toml).dispatch_pct.timezone == ""


def test_an_absent_account_timezone_inherits_the_queue_one(tmp_path: Path) -> None:
    (tmp_path / "runner-account.toml").write_text("", encoding="utf-8")
    assert load_account_policy(str(tmp_path)).dispatch_pct.timezone is None


def test_the_reason_is_in_the_message(tmp_path: Path) -> None:
    """Why the name failed, as zoneinfo put it, for a malformed key."""
    toml = tmp_path / "claude_runner.toml"
    toml.write_text(toml_setting(PATH, '"/etc/localtime"'), encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_settings(toml)
    assert (
        "[dispatch_pct].timezone = '/etc/localtime' is not an IANA time zone name such as "
        "'UTC' or 'America/New_York' (ZoneInfo keys may not be absolute paths, got: "
        "/etc/localtime). Leave it empty for the system's local time."
    ) in str(excinfo.value)


@pytest.mark.parametrize("name", KNOWN + UNKNOWN)
def test_a_name_loads_exactly_when_the_throttle_can_use_it(tmp_path: Path, name: str) -> None:
    """The check and ``to_local`` agree, so a config that loads cannot crash
    the throttle's time-of-day step, and none is refused that would work."""
    toml = tmp_path / "claude_runner.toml"
    toml.write_text(toml_setting(PATH, f'"{name}"'), encoding="utf-8")
    try:
        load_settings(toml)
    except ConfigError:
        loads = False
    else:
        loads = True
    try:
        to_local(datetime(2026, 9, 26, 12, 0, tzinfo=UTC), name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        usable = False
    else:
        usable = True
    assert (loads, usable) == (name in KNOWN, name in KNOWN)
