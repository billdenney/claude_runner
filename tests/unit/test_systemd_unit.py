"""Tests for cron.systemd_unit — --user systemd unit installer."""

from __future__ import annotations

import re
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from claude_task_runner.config.loader import load_settings
from claude_task_runner.config.schema import WatchdogSettings
from claude_task_runner.cron.systemd_unit import (
    _SYSTEMD_MAX_UNSIGNED,
    _SYSTEMD_MAX_WHOLE_SECONDS,
    RELOADED_DIRECTIVES,
    START_DIRECTIVES,
    UNIT_NAME,
    SystemdError,
    UnitSettingError,
    apply_plan,
    build_install_plan,
    build_unit_text,
    changed_start_directives,
    is_systemd_user_available,
    is_unit_active,
    uninstall,
    unit_queue,
)

_START = (
    "/usr/local/bin/claude-task-runner supervisor start --queue /q --config /q/claude_runner.toml"
)

_DEFAULTS = load_settings(None).watchdog
"""The package's ``[watchdog]`` defaults: 30 s, 600 s and 5."""

_UNIT_BEFORE_WATCHDOG = """\
[Unit]
Description=Claude Code task-runner supervisor
After=default.target
StartLimitIntervalSec=600
StartLimitBurst=5

[Service]
Type=simple
Environment=TERM=xterm-256color
Environment=PATH=%h/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/usr/local/bin/claude-task-runner supervisor start --queue /q --config /q/claude_runner.toml
ExecStop=-/usr/local/bin/claude-task-runner supervisor stop --queue /q --config /q/claude_runner.toml
WorkingDirectory=/q
KillMode=process
TimeoutStopSec=30
Restart=on-failure
RestartSec=30
RestartPreventExitStatus=0
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""
"""The unit ``build_unit_text`` wrote for ``_START`` before it read
``[watchdog]``, when RestartSec, StartLimitBurst and
StartLimitIntervalSec were hardcoded. Captured from that code."""


def _only_line(text: str, key: str) -> str:
    """Return the unit's single ``<key>=`` line."""
    lines = [ln for ln in text.splitlines() if ln.startswith(f"{key}=")]
    assert len(lines) == 1, f"expected exactly one {key}= line, got {lines!r}"
    return lines[0]


def _watchdog(**overrides: float) -> WatchdogSettings:
    """The package defaults with ``overrides``, validated like a TOML."""
    return WatchdogSettings.model_validate({**_DEFAULTS.model_dump(), **overrides})


class TestBuildUnitText:
    def test_includes_required_sections(self) -> None:
        text = build_unit_text(
            supervisor_command="/usr/bin/claude-task-runner supervisor start",
            queue_dir=Path("/queue"),
            watchdog=_DEFAULTS,
        )
        assert "[Unit]" in text
        assert "[Service]" in text
        assert "[Install]" in text
        assert "ExecStart=/usr/bin/claude-task-runner supervisor start" in text
        assert "WorkingDirectory=/queue" in text
        assert "Restart=on-failure" in text
        assert "WantedBy=default.target" in text

    def test_clean_exit_does_not_restart(self) -> None:
        text = build_unit_text(
            supervisor_command="/usr/bin/claude-task-runner supervisor start",
            queue_dir=Path("/queue"),
            watchdog=_DEFAULTS,
        )
        # Exit-status 0 means STOPPED state; we don't want a relaunch loop.
        assert "RestartPreventExitStatus=0" in text

    def test_includes_drain_execstop_when_adoption_off(self) -> None:
        """With adoption OFF, ExecStop runs ``supervisor drain --no-wait``
        so systemctl stop/restart goes through the graceful-drain path
        (the historical PR-11 wiring, plus the ``-`` prefix)."""
        text = build_unit_text(
            supervisor_command=_START, queue_dir=Path("/q"), watchdog=_DEFAULTS, adopt_workers=False
        )
        # Same binary path as ExecStart so the operator's pipx install is
        # honoured, the same --queue / --config so drain targets the right
        # state file, and the `-` prefix so systemd ignores its exit status.
        assert _only_line(text, "ExecStop") == (
            "ExecStop=-/usr/local/bin/claude-task-runner supervisor drain "
            "--queue /q --config /q/claude_runner.toml --no-wait"
        )

    def test_includes_fast_stop_execstop_when_adoption_on(self) -> None:
        """ADR-0025: with adoption ON (the default), ExecStop runs
        ``supervisor stop`` (a SIGTERM) so the daemon's fast stop trips —
        the supervisor exits promptly and file-backed workers survive."""
        text = build_unit_text(supervisor_command=_START, queue_dir=Path("/q"), watchdog=_DEFAULTS)
        assert _only_line(text, "ExecStop") == (
            "ExecStop=-/usr/local/bin/claude-task-runner supervisor stop "
            "--queue /q --config /q/claude_runner.toml"
        )
        # Fast stop does NOT drain.
        assert "supervisor drain" not in text
        assert "--no-wait" not in text

    def test_only_execstop_ignores_its_exit_status(self) -> None:
        """ExecStop carries systemd's ``-`` prefix in both modes: after a
        clean exit the supervisor has removed its PID file, so ExecStop
        exits 1, and without the prefix the unit would end
        ``failed (Result: exit-code)``. ExecStart stays unprefixed so a
        supervisor that fails still counts as failed for
        ``Restart=on-failure``."""
        for adopt in (True, False):
            text = build_unit_text(
                supervisor_command=_START,
                queue_dir=Path("/q"),
                watchdog=_DEFAULTS,
                adopt_workers=adopt,
            )
            assert _only_line(text, "ExecStart") == f"ExecStart={_START}"
            assert _only_line(text, "ExecStop").startswith(
                "ExecStop=-/usr/local/bin/claude-task-runner supervisor "
            )

    def test_kill_mode_process(self) -> None:
        """KillMode=process so dispatched claude subprocesses survive
        systemd's SIGKILL escalation on the main PID — in BOTH modes."""
        for adopt in (True, False):
            text = build_unit_text(
                supervisor_command="x",
                queue_dir=Path("/q"),
                watchdog=_DEFAULTS,
                adopt_workers=adopt,
            )
            assert "KillMode=process" in text

    def test_timeout_stop_sec_default_short_when_adoption_on(self) -> None:
        """ADR-0025: with adoption ON (default) the supervisor fast-stops,
        so TimeoutStopSec drops to a short 30s bound instead of the 4h
        drain ceiling — a `systemctl restart` is near-instant."""
        text = build_unit_text(supervisor_command="x", queue_dir=Path("/q"), watchdog=_DEFAULTS)
        assert "TimeoutStopSec=30" in text

    def test_timeout_stop_sec_default_matches_max_task_duration_when_adoption_off(self) -> None:
        """With adoption OFF, TimeoutStopSec=14400 (4h) matches the default
        [task_caps].max_duration_s_per_task so drain has time to finish
        the longest plausibly-allowed task."""
        text = build_unit_text(
            supervisor_command="x", queue_dir=Path("/q"), watchdog=_DEFAULTS, adopt_workers=False
        )
        assert "TimeoutStopSec=14400" in text

    def test_timeout_stop_sec_customizable(self) -> None:
        """An explicit timeout_stop_sec overrides the per-mode default in
        both modes."""
        for adopt in (True, False):
            text = build_unit_text(
                supervisor_command="x",
                queue_dir=Path("/q"),
                watchdog=_DEFAULTS,
                timeout_stop_sec=1800,
                adopt_workers=adopt,
            )
            assert "TimeoutStopSec=1800" in text

    def test_clean_stop_exit_does_not_restart(self) -> None:
        """RestartPreventExitStatus=0 — a clean stop/drain-exit means the
        operator asked for stop/restart, NOT a crash. systemd's stop
        sequence handles the eventual fresh-start when needed
        (``systemctl restart`` runs stop then start; ``stop`` alone
        leaves it stopped). Restart=on-failure only fires for crashes."""
        text = build_unit_text(supervisor_command="x", queue_dir=Path("/q"), watchdog=_DEFAULTS)
        assert "Restart=on-failure" in text
        assert "RestartPreventExitStatus=0" in text


_UNIT_LINE_OF = {
    "restart_cooldown_s": "RestartSec",
    "crash_loop_threshold": "StartLimitBurst",
    "restart_backoff_max_s": "StartLimitIntervalSec",
}
"""The unit line each ``[watchdog]`` key sets."""


def _unit(watchdog: WatchdogSettings) -> str:
    return build_unit_text(supervisor_command=_START, queue_dir=Path("/q"), watchdog=watchdog)


class TestRestartPolicyFromWatchdog:
    """The unit's restart policy comes from the queue's ``[watchdog]``."""

    def test_package_defaults_reproduce_the_hardcoded_unit(self) -> None:
        """A queue that sets no ``[watchdog]`` gets the same unit as before."""
        assert _unit(_DEFAULTS) == _UNIT_BEFORE_WATCHDOG

    def test_each_key_sets_its_line(self) -> None:
        text = _unit(
            _watchdog(restart_cooldown_s=120, restart_backoff_max_s=1800, crash_loop_threshold=9)
        )
        assert _only_line(text, "RestartSec") == "RestartSec=120"
        assert _only_line(text, "StartLimitBurst") == "StartLimitBurst=9"
        assert _only_line(text, "StartLimitIntervalSec") == "StartLimitIntervalSec=1800"

    def test_every_watchdog_key_sets_exactly_its_line(self) -> None:
        """Each ``[watchdog]`` key changes its own unit line and nothing else.

        Walks the schema, so a key added to ``[watchdog]`` later fails
        here until it has a unit line in ``_UNIT_LINE_OF``."""
        assert set(_UNIT_LINE_OF) == set(WatchdogSettings.model_fields)
        before = _unit(_DEFAULTS).splitlines()
        for key, unit_key in _UNIT_LINE_OF.items():
            default = getattr(_DEFAULTS, key)
            after = _unit(_watchdog(**{key: default * 2})).splitlines()
            changed = [(old, new) for old, new in zip(before, after, strict=True) if old != new]
            assert changed == [(f"{unit_key}={default:g}", f"{unit_key}={default * 2:g}")], key

    @pytest.mark.parametrize(
        ("seconds", "written"),
        [
            (30, "30"),
            (0.25, "0.25"),
            (30.5, "30.5"),
            # Rounded to systemd's resolution of one microsecond.
            (30.1234567, "30.123457"),
            # Python writes these two as 1e-05 and 1e-07, which systemd rejects.
            (1e-5, "0.00001"),
            (1e-7, "0"),
            (_SYSTEMD_MAX_WHOLE_SECONDS + 0.5, "18446744073708.5"),
        ],
    )
    def test_seconds_are_written_as_systemd_reads_them(self, seconds: float, written: str) -> None:
        text = _unit(_watchdog(restart_cooldown_s=seconds, restart_backoff_max_s=seconds))
        assert _only_line(text, "RestartSec") == f"RestartSec={written}"
        assert _only_line(text, "StartLimitIntervalSec") == f"StartLimitIntervalSec={written}"

    @pytest.mark.parametrize("key", ["restart_cooldown_s", "restart_backoff_max_s"])
    def test_a_span_longer_than_systemd_accepts_is_refused(self, key: str) -> None:
        """systemd would ignore the line and fall back to its own default."""
        with pytest.raises(UnitSettingError) as excinfo:
            _unit(_watchdog(**{key: float(_SYSTEMD_MAX_WHOLE_SECONDS + 1)}))
        assert str(excinfo.value) == (
            f"[watchdog].{key} = 18446744073709.0 is longer than systemd accepts (18446744073708 s)"
        )

    @pytest.mark.parametrize("key", ["restart_cooldown_s", "restart_backoff_max_s"])
    def test_an_infinite_span_is_refused(self, key: str) -> None:
        """The schema's ``gt=0`` lets ``inf`` through, and TOML can spell it."""
        with pytest.raises(UnitSettingError) as excinfo:
            _unit(_watchdog(**{key: float("inf")}))
        assert str(excinfo.value) == f"[watchdog].{key} = inf is not a finite number of seconds"

    def test_the_largest_burst_systemd_accepts_is_written(self) -> None:
        text = _unit(_watchdog(crash_loop_threshold=_SYSTEMD_MAX_UNSIGNED))
        assert _only_line(text, "StartLimitBurst") == "StartLimitBurst=4294967295"

    def test_a_burst_larger_than_systemd_accepts_is_refused(self) -> None:
        with pytest.raises(UnitSettingError) as excinfo:
            _unit(_watchdog(crash_loop_threshold=_SYSTEMD_MAX_UNSIGNED + 1))
        assert str(excinfo.value) == (
            "[watchdog].crash_loop_threshold = 4294967296 is more than systemd accepts (4294967295)"
        )


_SYSTEMD_ANALYZE = shutil.which("systemd-analyze")


def _verify(unit_dir: Path, text: str) -> str:
    """What ``systemd-analyze verify`` prints about ``text`` as a unit.

    Its exit status says nothing here: a line systemd cannot parse gets
    a warning, and ``verify`` still exits 0."""
    assert _SYSTEMD_ANALYZE is not None
    unit = unit_dir / "ctr-verify.service"
    unit.write_text(text, encoding="utf-8")
    proc = subprocess.run(
        [_SYSTEMD_ANALYZE, "verify", "--man=no", str(unit)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return proc.stdout + proc.stderr


@pytest.fixture
def systemd_analyze() -> str:
    if _SYSTEMD_ANALYZE is None:
        pytest.skip("systemd-analyze is not installed")
    return _SYSTEMD_ANALYZE


@pytest.fixture
def verify_dir(systemd_analyze: str, tmp_path: Path) -> Path:
    """A directory to verify units in; skips where ``verify`` has warnings of its own."""
    baseline = _verify(tmp_path, "[Unit]\nDescription=baseline\n\n[Service]\nExecStart=/bin/true\n")
    if baseline:
        pytest.skip(f"systemd-analyze verify is not usable here: {baseline.strip()}")
    return tmp_path


class TestSystemdParsesTheUnit:
    """The unit text against systemd's own parser, where one is installed."""

    def test_the_check_sees_a_line_systemd_ignores(self, verify_dir: Path) -> None:
        """Guards the checks below: they would pass on no warnings at all."""
        out = _verify(
            verify_dir,
            "[Unit]\nDescription=t\nStartLimitBurst=4294967296\n\n"
            "[Service]\nExecStart=/bin/true\nRestartSec=1e-05\n",
        )
        assert "Failed to parse unsigned value, ignoring: 4294967296" in out
        assert "Failed to parse sec value, ignoring: 1e-05" in out

    @pytest.mark.parametrize(
        "overrides",
        [
            {},
            {
                "restart_cooldown_s": 0.25,
                "restart_backoff_max_s": 30.1234567,
                "crash_loop_threshold": _SYSTEMD_MAX_UNSIGNED,
            },
            {"restart_cooldown_s": 1e-5, "restart_backoff_max_s": 1e-7, "crash_loop_threshold": 1},
            {
                "restart_cooldown_s": _SYSTEMD_MAX_WHOLE_SECONDS + 0.5,
                "restart_backoff_max_s": float(_SYSTEMD_MAX_WHOLE_SECONDS),
            },
        ],
        ids=["defaults", "fractional", "tiny", "longest"],
    )
    def test_systemd_parses_every_line(self, verify_dir: Path, overrides: dict[str, float]) -> None:
        queue = verify_dir / "q"
        queue.mkdir()
        text = build_unit_text(
            supervisor_command=f"/bin/true supervisor start --queue {queue}",
            queue_dir=queue,
            watchdog=_watchdog(**overrides),
        )
        assert _verify(verify_dir, text) == ""

    @pytest.mark.parametrize(
        "seconds", [30, 0.25, 30.1234567, 1e-5, 1e-7, _SYSTEMD_MAX_WHOLE_SECONDS + 0.5]
    )
    def test_systemd_reads_what_watchdog_says(self, systemd_analyze: str, seconds: float) -> None:
        """To within half a microsecond, systemd's resolution."""
        line = _only_line(_unit(_watchdog(restart_cooldown_s=seconds)), "RestartSec")
        written = line.removeprefix("RestartSec=")
        proc = subprocess.run(
            [systemd_analyze, "timespan", written],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        match = re.search(r"(?:μs|us): (\d+)", proc.stdout)
        assert match is not None, proc.stdout
        microseconds = Decimal(match.group(1))
        assert microseconds == Decimal(written) * 1_000_000
        assert abs(microseconds - Decimal(repr(seconds)) * 1_000_000) <= Decimal("0.5")

    def test_the_longest_span_is_the_one_systemd_accepts(self, systemd_analyze: str) -> None:
        def _timespan(text: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [systemd_analyze, "timespan", text],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )

        assert _timespan(str(_SYSTEMD_MAX_WHOLE_SECONDS)).returncode == 0
        too_long = _timespan(str(_SYSTEMD_MAX_WHOLE_SECONDS + 1))
        assert too_long.returncode != 0
        assert "out of range" in too_long.stderr


class TestBuildInstallPlan:
    def test_default_path(self, tmp_path: Path) -> None:
        plan = build_install_plan(
            supervisor_command="/usr/bin/claude-task-runner supervisor start",
            queue_dir=Path("/queue"),
            watchdog=_DEFAULTS,
            unit_path=tmp_path / f"{UNIT_NAME}.service",
        )
        assert plan.unit_path.name == f"{UNIT_NAME}.service"
        assert plan.block_existed is False
        assert plan.enable_command[0] == "systemctl"
        assert "--user" in plan.enable_command

    def test_block_existed_when_file_present(self, tmp_path: Path) -> None:
        path = tmp_path / f"{UNIT_NAME}.service"
        path.write_text("[Unit]\nDescription=Old\n")
        plan = build_install_plan(
            supervisor_command="/usr/bin/x",
            queue_dir=Path("/q"),
            watchdog=_DEFAULTS,
            unit_path=path,
        )
        assert plan.block_existed is True

    def test_existing_text_is_none_without_a_unit(self, tmp_path: Path) -> None:
        plan = build_install_plan(
            supervisor_command=_START,
            queue_dir=Path("/q"),
            watchdog=_DEFAULTS,
            unit_path=tmp_path / f"{UNIT_NAME}.service",
        )
        assert plan.existing_text is None

    def test_existing_text_is_the_units_text(self, tmp_path: Path) -> None:
        path = tmp_path / f"{UNIT_NAME}.service"
        path.write_text(_UNIT_BEFORE_WATCHDOG, encoding="utf-8")
        plan = build_install_plan(
            supervisor_command=_START,
            queue_dir=Path("/q"),
            watchdog=_DEFAULTS,
            unit_path=path,
        )
        assert plan.existing_text == _UNIT_BEFORE_WATCHDOG


class TestApplyPlan:
    def _make_fake_systemctl(self, tmp_path: Path, *, fail: bool = False) -> Path:
        if fail:
            body = '#!/usr/bin/env bash\necho "permission denied" 1>&2\nexit 1\n'
        else:
            body = "#!/usr/bin/env bash\nexit 0\n"
        p = tmp_path / "systemctl"
        p.write_text(body)
        p.chmod(0o755)
        return p

    def test_writes_unit_file(self, tmp_path: Path) -> None:
        unit_path = tmp_path / "claude-task-runner.service"
        plan = build_install_plan(
            supervisor_command="/usr/bin/x",
            queue_dir=tmp_path,
            watchdog=_DEFAULTS,
            unit_path=unit_path,
        )
        binary = self._make_fake_systemctl(tmp_path)
        apply_plan(plan, systemctl_executable=str(binary))
        assert unit_path.exists()
        assert "ExecStart=/usr/bin/x" in unit_path.read_text()

    def test_apply_propagates_systemctl_failure(self, tmp_path: Path) -> None:
        unit_path = tmp_path / "claude-task-runner.service"
        plan = build_install_plan(
            supervisor_command="/usr/bin/x",
            queue_dir=tmp_path,
            watchdog=_DEFAULTS,
            unit_path=unit_path,
        )
        binary = self._make_fake_systemctl(tmp_path, fail=True)
        with pytest.raises(SystemdError):
            apply_plan(plan, systemctl_executable=str(binary))


class TestUninstall:
    def test_removes_existing_unit(self, tmp_path: Path) -> None:
        unit_path = tmp_path / f"{UNIT_NAME}.service"
        unit_path.write_text("[Unit]\nDescription=Test\n")
        existed = uninstall(
            unit_path=unit_path,
            systemctl_executable="/usr/bin/false",  # tolerated failure
        )
        assert existed is True
        assert not unit_path.exists()

    def test_returns_false_when_absent(self, tmp_path: Path) -> None:
        unit_path = tmp_path / f"{UNIT_NAME}.service"
        existed = uninstall(
            unit_path=unit_path,
            systemctl_executable="/usr/bin/false",
        )
        assert existed is False

    def _make_failing_systemctl(self, tmp_path: Path) -> Path:
        """A fake systemctl that fails (rc=1) and writes to stderr."""
        p = tmp_path / "systemctl"
        p.write_text('#!/usr/bin/env bash\necho "Failed to disable unit" 1>&2\nexit 1\n')
        p.chmod(0o755)
        return p

    def test_disable_failure_is_logged_not_silent(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A non-zero rc from ``systemctl --user disable`` must be logged at
        WARNING — a silent failure would leave the unit enabled/active even
        though the operator asked to uninstall it (audit finding)."""
        unit_path = tmp_path / f"{UNIT_NAME}.service"
        unit_path.write_text("[Unit]\nDescription=Test\n")
        binary = self._make_failing_systemctl(tmp_path)
        with caplog.at_level("WARNING", logger="claude_task_runner.cron.systemd_unit"):
            existed = uninstall(unit_path=unit_path, systemctl_executable=str(binary))
        # Tolerant behaviour preserved: the file is still removed.
        assert existed is True
        assert not unit_path.exists()
        # But the failure is now visible.
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("disable" in r.getMessage() for r in warnings)
        assert any("Failed to disable unit" in r.getMessage() for r in warnings)

    def test_successful_uninstall_logs_nothing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """rc=0 from every systemctl step → no WARNING noise."""
        unit_path = tmp_path / f"{UNIT_NAME}.service"
        unit_path.write_text("[Unit]\nDescription=Test\n")
        good = tmp_path / "systemctl"
        good.write_text("#!/usr/bin/env bash\nexit 0\n")
        good.chmod(0o755)
        with caplog.at_level("WARNING", logger="claude_task_runner.cron.systemd_unit"):
            uninstall(unit_path=unit_path, systemctl_executable=str(good))
        assert [r for r in caplog.records if r.levelname == "WARNING"] == []


class TestIsSystemdUserAvailable:
    def test_missing_systemctl_returns_false(self) -> None:
        # An obviously-missing executable name returns False without raising.
        assert is_systemd_user_available(systemctl_executable="this-doesnt-exist-12345") is False

    def test_failing_systemctl_returns_false(self, tmp_path: Path) -> None:
        body = "#!/usr/bin/env bash\nexit 127\n"
        p = tmp_path / "systemctl"
        p.write_text(body)
        p.chmod(0o755)
        assert is_systemd_user_available(systemctl_executable=str(p)) is False


def test_unit_text_includes_term_and_path_environment() -> None:
    """Regression: systemd-user units start with a near-empty environment.
    Without TERM the pexpect-driven `claude /usage` TUI can't render; without
    `~/.local/bin` on PATH `shutil.which("claude")` returns None for pipx
    installs and the supervisor's safe_poll() raises UsageCaptureSpawnError
    forever. The generated unit must inject both.
    """
    text = build_unit_text(
        supervisor_command="/usr/bin/claude-task-runner supervisor start",
        queue_dir=Path("/home/bill/queue"),
        watchdog=_DEFAULTS,
    )
    assert "Environment=TERM=" in text
    assert "Environment=PATH=" in text
    # PATH must include the user's local bin so pipx-installed Claude resolves.
    assert "%h/.local/bin" in text or "/.local/bin" in text


def _unit_for(
    queue: str,
    *,
    watchdog: WatchdogSettings,
    exe: str = "/usr/local/bin/claude-task-runner",
    adopt_workers: bool = True,
) -> str:
    return build_unit_text(
        supervisor_command=f"{exe} supervisor start --queue {queue}",
        queue_dir=Path(queue),
        watchdog=watchdog,
        adopt_workers=adopt_workers,
    )


class TestDirectiveClassification:
    """Whether a running supervisor sees a changed line depends on the directive."""

    def test_the_sets_do_not_overlap(self) -> None:
        assert not START_DIRECTIVES & RELOADED_DIRECTIVES

    @pytest.mark.parametrize("adopt_workers", [True, False])
    def test_every_directive_the_unit_writes_is_classified(self, adopt_workers: bool) -> None:
        """A directive added to build_unit_text must be placed in one set."""
        text = _unit_for("/q", watchdog=_DEFAULTS, adopt_workers=adopt_workers)
        keys = {
            line.partition("=")[0]
            for line in text.splitlines()
            if "=" in line and not line.startswith("[")
        }
        assert keys == START_DIRECTIVES | RELOADED_DIRECTIVES


class TestChangedStartDirectives:
    def test_no_old_unit(self) -> None:
        assert changed_start_directives(None, _unit_for("/q", watchdog=_DEFAULTS)) == []

    def test_same_unit(self) -> None:
        text = _unit_for("/q", watchdog=_DEFAULTS)
        assert changed_start_directives(text, text) == []

    def test_another_queue(self) -> None:
        old = _unit_for("/a", watchdog=_DEFAULTS)
        new = _unit_for("/b", watchdog=_DEFAULTS)
        assert changed_start_directives(old, new) == ["ExecStart", "WorkingDirectory"]

    def test_another_executable(self) -> None:
        old = _unit_for("/q", exe="/old/venv/bin/claude-task-runner", watchdog=_DEFAULTS)
        new = _unit_for("/q", watchdog=_DEFAULTS)
        assert changed_start_directives(old, new) == ["ExecStart"]

    def test_restart_policy_applies_at_reload(self) -> None:
        """Verified on systemd 255: RestartSec and StartLimit* apply without a restart."""
        old = _unit_for("/q", watchdog=_DEFAULTS)
        new = _unit_for(
            "/q",
            watchdog=_watchdog(
                restart_cooldown_s=7, restart_backoff_max_s=700, crash_loop_threshold=9
            ),
        )
        assert old != new
        assert changed_start_directives(old, new) == []

    def test_stop_wiring_applies_at_the_next_stop(self) -> None:
        """Verified on systemd 255: a changed ExecStop runs at the next stop."""
        old = _unit_for("/q", watchdog=_DEFAULTS, adopt_workers=True)
        new = _unit_for("/q", watchdog=_DEFAULTS, adopt_workers=False)
        assert old != new
        assert changed_start_directives(old, new) == []

    def test_a_hand_edited_environment(self) -> None:
        new = _unit_for("/q", watchdog=_DEFAULTS)
        old = new.replace("Environment=TERM=xterm-256color", "Environment=TERM=dumb")
        assert changed_start_directives(old, new) == ["Environment"]

    def test_a_directive_the_old_unit_lacked(self) -> None:
        new = _unit_for("/q", watchdog=_DEFAULTS)
        old = "\n".join(ln for ln in new.splitlines() if not ln.startswith("StandardError="))
        assert changed_start_directives(old, new) == ["StandardError"]

    def test_comments_and_sections_are_not_directives(self) -> None:
        new = _unit_for("/q", watchdog=_DEFAULTS)
        old = "# ExecStart=/elsewhere\n; WorkingDirectory=/elsewhere\n" + new
        assert changed_start_directives(old, new) == []


class TestUnitQueue:
    def test_the_working_directory(self) -> None:
        assert unit_queue(_unit_for("/some/queue", watchdog=_DEFAULTS)) == Path("/some/queue")

    def test_a_unit_without_one(self) -> None:
        assert unit_queue("[Unit]\nDescription=Old\n") is None


def _fake_systemctl(tmp_path: Path, output: str, code: int) -> str:
    """A systemctl that answers ``--user is-active`` for this unit, and fails otherwise."""
    path = tmp_path / "systemctl"
    path.write_text(
        "#!/usr/bin/env bash\n"
        f'[ "$*" = "--user is-active {UNIT_NAME}.service" ] || exit 99\n'
        f"echo {output}\n"
        f"exit {code}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return str(path)


class TestIsUnitActive:
    def test_active(self, tmp_path: Path) -> None:
        assert is_unit_active(_fake_systemctl(tmp_path, "active", 0)) is True

    @pytest.mark.parametrize(
        ("output", "code"),
        [("inactive", 3), ("failed", 3), ("activating", 3), ("active", 1)],
    )
    def test_anything_else(self, tmp_path: Path, output: str, code: int) -> None:
        """A unit about to restart after a crash starts its next process afresh."""
        assert is_unit_active(_fake_systemctl(tmp_path, output, code)) is False

    def test_missing_systemctl(self) -> None:
        assert is_unit_active("this-doesnt-exist-12345") is False
