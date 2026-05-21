"""Tests for doctor/checks.py — one battery of pure-ish self-diagnostics.

Every check is exercised across its PASS / WARN / FAIL branches. The
checks that touch the filesystem (queue layout, state YAMLs,
supervisor state, EMA) are tested with real tmp_path scaffolding; the
two that touch external state (``check_claude_binary`` PATH lookup,
``check_global_lock`` PID liveness) are mocked.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_task_runner.config.loader import load_settings
from claude_task_runner.config.schema import AccountSettings, Settings
from claude_task_runner.doctor.checks import (
    CheckStatus,
    _extract_paths,
    all_checks,
    check_account_sudo,
    check_accounts,
    check_claude_binary,
    check_ema,
    check_global_lock,
    check_legacy_claude_config_dir,
    check_queue_layout,
    check_queue_perms_for_linux_users,
    check_skills_installed,
    check_state_yamls,
    check_supervisor_state,
    check_task_paths,
    check_task_yamls,
    check_watchdog_installed,
)
from claude_task_runner.queue.schema import Task
from claude_task_runner.queue.store import (
    task_path_for,
    todo_dir,
    write_task_atomic,
)
from claude_task_runner.supervisor.persistence import (
    supervisor_state_path,
)
from claude_task_runner.supervisor.persistence import (
    write_atomic as supervisor_write_atomic,
)
from claude_task_runner.supervisor.states import SupervisorSnapshot, SupervisorState


@pytest.fixture
def settings() -> Settings:
    return load_settings(None)


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    todo_dir(qd)
    return qd


# ---------------------------------------------------------------------------
# check_claude_binary
# ---------------------------------------------------------------------------


def test_check_claude_binary_pass(settings: Settings) -> None:
    with patch("claude_task_runner.doctor.checks.shutil.which", return_value="/usr/bin/claude"):
        result = check_claude_binary(settings)
    assert result.status == CheckStatus.PASS
    assert "/usr/bin/claude" in result.detail


def test_check_claude_binary_fail(settings: Settings) -> None:
    with patch("claude_task_runner.doctor.checks.shutil.which", return_value=None):
        result = check_claude_binary(settings)
    assert result.status == CheckStatus.FAIL
    assert "not found" in result.detail
    assert result.remediation != ""


# ---------------------------------------------------------------------------
# check_accounts (replaces the old check_claude_config_dir)
# ---------------------------------------------------------------------------


def _set_accounts(settings: Settings, accounts: list[AccountSettings]) -> Settings:
    """Helper: return a Settings copy with the given [[accounts]] list."""
    return settings.model_copy(update={"accounts": accounts})


def test_check_accounts_default_account_no_config_dir_passes(settings: Settings) -> None:
    """Synthesised default account with empty config_dir → PASS."""
    # The loader's back-compat shim already populated a single
    # 'default' account from the empty legacy field.
    assert len(settings.accounts) == 1
    assert settings.accounts[0].name == "default"
    assert settings.accounts[0].config_dir == ""
    result = check_accounts(settings)
    assert result.status == CheckStatus.PASS
    assert "default" in result.detail


def test_check_accounts_explicit_present_with_creds(settings: Settings, tmp_path: Path) -> None:
    cfg_dir = tmp_path / "claude_personal"
    cfg_dir.mkdir()
    (cfg_dir / ".credentials.json").write_text("{}", encoding="utf-8")
    s = _set_accounts(
        settings,
        [AccountSettings(name="personal", config_dir=str(cfg_dir))],
    )
    result = check_accounts(s)
    assert result.status == CheckStatus.PASS
    assert "personal" in result.detail


def test_check_accounts_explicit_missing_dir_fails(settings: Settings, tmp_path: Path) -> None:
    cfg_dir = tmp_path / "does_not_exist"
    s = _set_accounts(
        settings,
        [AccountSettings(name="personal", config_dir=str(cfg_dir))],
    )
    result = check_accounts(s)
    assert result.status == CheckStatus.FAIL
    assert "does not exist" in result.remediation


def test_check_accounts_no_credentials_warns(settings: Settings, tmp_path: Path) -> None:
    cfg_dir = tmp_path / "claude_unloggedin"
    cfg_dir.mkdir()
    s = _set_accounts(
        settings,
        [AccountSettings(name="personal", config_dir=str(cfg_dir))],
    )
    result = check_accounts(s)
    assert result.status == CheckStatus.WARN
    assert "no .credentials.json" in result.remediation


def test_check_accounts_multi_mixed_states(settings: Settings, tmp_path: Path) -> None:
    """One PASS + one WARN + one FAIL → FAIL (highest severity wins)."""
    good = tmp_path / "good"
    good.mkdir()
    (good / ".credentials.json").write_text("{}", encoding="utf-8")
    nocreds = tmp_path / "nocreds"
    nocreds.mkdir()
    s = _set_accounts(
        settings,
        [
            AccountSettings(name="a1", config_dir=str(good)),
            AccountSettings(name="a2", config_dir=str(nocreds)),
            AccountSettings(name="a3", config_dir=str(tmp_path / "missing")),
        ],
    )
    result = check_accounts(s)
    assert result.status == CheckStatus.FAIL
    assert "a3" in result.remediation


# ---------------------------------------------------------------------------
# check_legacy_claude_config_dir
# ---------------------------------------------------------------------------


def test_check_legacy_config_dir_unset(settings: Settings) -> None:
    """No legacy field set → PASS."""
    assert settings.claude.config_dir == ""
    result = check_legacy_claude_config_dir(settings)
    assert result.status == CheckStatus.PASS
    assert "no legacy" in result.detail


def test_check_legacy_config_dir_synthesised(settings: Settings) -> None:
    """[claude].config_dir set, no explicit [[accounts]] → PASS (supported)."""
    legacy = "/home/x/.claude_personal"
    s = settings.model_copy(
        update={
            "claude": settings.claude.model_copy(update={"config_dir": legacy}),
            "accounts": [AccountSettings(name="default", config_dir=legacy)],
        }
    )
    result = check_legacy_claude_config_dir(s)
    assert result.status == CheckStatus.PASS
    assert "synthesised" in result.detail


def test_check_legacy_config_dir_conflicts_with_explicit(settings: Settings) -> None:
    """Both legacy field AND explicit [[accounts]] set → WARN."""
    s = settings.model_copy(
        update={
            "claude": settings.claude.model_copy(update={"config_dir": "/legacy/path"}),
            "accounts": [AccountSettings(name="personal", config_dir="/different/path")],
        }
    )
    result = check_legacy_claude_config_dir(s)
    assert result.status == CheckStatus.WARN
    assert "ignored" in result.detail


# ---------------------------------------------------------------------------
# check_account_policies
# ---------------------------------------------------------------------------


def test_check_account_policies_defaults_pass(settings: Settings) -> None:
    """Default settings (single 'default' account, empty config_dir) → PASS.

    Empty config_dir → policy defaults apply; report should show
    max_concurrency=1 and the default bands.
    """
    from claude_task_runner.doctor.checks import check_account_policies

    result = check_account_policies(settings)
    assert result.status == CheckStatus.PASS
    assert "max_concurrency=1" in result.detail
    assert "daytime=40/60" in result.detail
    assert "nighttime=70/90" in result.detail


def test_check_account_policies_present_file_reported(settings: Settings, tmp_path: Path) -> None:
    """An on-disk per-account file is parsed and reported."""
    from claude_task_runner.doctor.checks import check_account_policies

    cfg_dir = tmp_path / "personal"
    cfg_dir.mkdir()
    (cfg_dir / "runner-account.toml").write_text(
        "[concurrency]\nmax_concurrency = 5\n", encoding="utf-8"
    )
    s = _set_accounts(settings, [AccountSettings(name="personal", config_dir=str(cfg_dir))])
    result = check_account_policies(s)
    assert result.status == CheckStatus.PASS
    assert "max_concurrency=5" in result.detail


def test_check_account_policies_invalid_file_fails(settings: Settings, tmp_path: Path) -> None:
    from claude_task_runner.doctor.checks import check_account_policies

    cfg_dir = tmp_path / "broken"
    cfg_dir.mkdir()
    (cfg_dir / "runner-account.toml").write_text("] not toml [", encoding="utf-8")
    s = _set_accounts(settings, [AccountSettings(name="broken", config_dir=str(cfg_dir))])
    result = check_account_policies(s)
    assert result.status == CheckStatus.FAIL
    assert "broken" in result.remediation


# ---------------------------------------------------------------------------
# check_account_sudo
# ---------------------------------------------------------------------------


def test_check_account_sudo_no_linux_user_passes(settings: Settings) -> None:
    """Default settings have no linux_user → PASS skip."""
    result = check_account_sudo(settings)
    assert result.status == CheckStatus.PASS
    assert "no accounts" in result.detail


def test_check_account_sudo_self_user_is_noop(settings: Settings) -> None:
    """linux_user matching the supervisor's user is treated as no-op."""
    import getpass

    self_user = getpass.getuser()
    s = _set_accounts(
        settings,
        [AccountSettings(name="self", config_dir="", linux_user=self_user)],
    )
    # Patch the helper to avoid pwd lookup variance and force a known
    # username — the check should return PASS without calling sudo.
    with patch("claude_task_runner.doctor.checks._current_username", return_value=self_user):
        result = check_account_sudo(s)
    assert result.status == CheckStatus.PASS
    assert "no-op" in result.detail


def test_check_account_sudo_fail_on_nonzero(settings: Settings) -> None:
    """sudo returns non-zero → FAIL with remediation listing the snippet."""
    s = _set_accounts(
        settings,
        [
            AccountSettings(
                name="work",
                config_dir="",
                linux_user="some-other-user",
            )
        ],
    )

    class _R:
        returncode = 1
        stderr = "sudo: a password is required\n"

    with (
        patch("claude_task_runner.doctor.checks._current_username", return_value="me"),
        patch("claude_task_runner.doctor.checks.subprocess.run", return_value=_R()),
        patch("claude_task_runner.doctor.checks.shutil.which", return_value="/usr/bin/sudo"),
    ):
        result = check_account_sudo(s)
    assert result.status == CheckStatus.FAIL
    assert "some-other-user" in result.remediation
    assert "NOPASSWD" in result.remediation


def test_check_account_sudo_pass_when_sudo_succeeds(settings: Settings) -> None:
    """sudo returns 0 → PASS."""
    s = _set_accounts(
        settings,
        [
            AccountSettings(
                name="work",
                config_dir="",
                linux_user="bill-work",
            )
        ],
    )

    class _R:
        returncode = 0
        stderr = ""

    with (
        patch("claude_task_runner.doctor.checks._current_username", return_value="bill"),
        patch("claude_task_runner.doctor.checks.subprocess.run", return_value=_R()),
        patch("claude_task_runner.doctor.checks.shutil.which", return_value="/usr/bin/sudo"),
    ):
        result = check_account_sudo(s)
    assert result.status == CheckStatus.PASS
    assert "work" in result.detail


def test_check_account_sudo_missing_sudo_binary(settings: Settings) -> None:
    """sudo not on PATH but linux_user requested → FAIL."""
    s = _set_accounts(
        settings,
        [AccountSettings(name="work", config_dir="", linux_user="bill-work")],
    )
    with patch("claude_task_runner.doctor.checks.shutil.which", return_value=None):
        result = check_account_sudo(s)
    assert result.status == CheckStatus.FAIL
    assert "sudo" in result.detail


# ---------------------------------------------------------------------------
# check_queue_perms_for_linux_users
# ---------------------------------------------------------------------------


def test_check_queue_perms_no_linux_user_skipped(settings: Settings, queue_dir: Path) -> None:
    result = check_queue_perms_for_linux_users(settings, queue_dir)
    assert result.status == CheckStatus.PASS
    assert "skipped" in result.detail


def test_check_queue_perms_missing_queue_dir(settings: Settings, tmp_path: Path) -> None:
    s = _set_accounts(
        settings,
        [AccountSettings(name="work", config_dir="", linux_user="bill-work")],
    )
    result = check_queue_perms_for_linux_users(s, tmp_path / "nope")
    assert result.status == CheckStatus.FAIL
    assert "queue dir not found" in result.detail


def test_check_queue_perms_unknown_linux_user(settings: Settings, queue_dir: Path) -> None:
    """Account linux_user that doesn't exist on this host → FAIL."""
    s = _set_accounts(
        settings,
        [
            AccountSettings(
                name="work",
                config_dir="",
                linux_user="user-that-does-not-exist-on-any-host",
            )
        ],
    )
    result = check_queue_perms_for_linux_users(s, queue_dir)
    assert result.status == CheckStatus.FAIL
    assert "does not exist" in result.remediation


# ---------------------------------------------------------------------------
# check_global_lock
# ---------------------------------------------------------------------------


def test_check_global_lock_no_file(settings: Settings, tmp_path: Path) -> None:
    with patch(
        "claude_task_runner.doctor.checks.pidfile_mod.global_lock_path",
        return_value=tmp_path / "nonexistent.lock",
    ):
        result = check_global_lock(settings)
    assert result.status == CheckStatus.PASS
    assert "no lock file" in result.detail


def test_check_global_lock_unreadable_pid(settings: Settings, tmp_path: Path) -> None:
    lock = tmp_path / "global.lock"
    lock.write_text("not a number", encoding="utf-8")
    with (
        patch(
            "claude_task_runner.doctor.checks.pidfile_mod.global_lock_path",
            return_value=lock,
        ),
        patch(
            "claude_task_runner.doctor.checks.pidfile_mod.read_existing_pid",
            return_value=None,
        ),
    ):
        result = check_global_lock(settings)
    assert result.status == CheckStatus.WARN
    assert "PID is unreadable" in result.detail


def test_check_global_lock_stale_pid(settings: Settings, tmp_path: Path) -> None:
    lock = tmp_path / "global.lock"
    lock.write_text("12345", encoding="utf-8")
    with (
        patch(
            "claude_task_runner.doctor.checks.pidfile_mod.global_lock_path",
            return_value=lock,
        ),
        patch(
            "claude_task_runner.doctor.checks.pidfile_mod.read_existing_pid",
            return_value=12345,
        ),
        patch(
            "claude_task_runner.doctor.checks.pidfile_mod.is_pid_alive",
            return_value=False,
        ),
    ):
        result = check_global_lock(settings)
    assert result.status == CheckStatus.WARN
    assert "not alive" in result.detail


def test_check_global_lock_live(settings: Settings, tmp_path: Path) -> None:
    lock = tmp_path / "global.lock"
    lock.write_text("12345", encoding="utf-8")
    with (
        patch(
            "claude_task_runner.doctor.checks.pidfile_mod.global_lock_path",
            return_value=lock,
        ),
        patch(
            "claude_task_runner.doctor.checks.pidfile_mod.read_existing_pid",
            return_value=12345,
        ),
        patch(
            "claude_task_runner.doctor.checks.pidfile_mod.is_pid_alive",
            return_value=True,
        ),
    ):
        result = check_global_lock(settings)
    assert result.status == CheckStatus.PASS
    assert "12345" in result.detail


# ---------------------------------------------------------------------------
# check_queue_layout
# ---------------------------------------------------------------------------


def test_check_queue_layout_missing_queue_dir(settings: Settings, tmp_path: Path) -> None:
    result = check_queue_layout(settings, tmp_path / "nope")
    assert result.status == CheckStatus.FAIL
    assert "not found" in result.detail


def test_check_queue_layout_happy_path(settings: Settings, queue_dir: Path) -> None:
    # queue_dir fixture already created todo/ via todo_dir().
    result = check_queue_layout(settings, queue_dir)
    assert result.status == CheckStatus.PASS


def test_check_queue_layout_missing_todo_warns(settings: Settings, tmp_path: Path) -> None:
    qd = tmp_path / "q"
    qd.mkdir()
    # No todo/ subdir.
    result = check_queue_layout(settings, qd)
    assert result.status == CheckStatus.WARN
    assert "todo/" in result.detail


# ---------------------------------------------------------------------------
# check_task_yamls
# ---------------------------------------------------------------------------


def _make_task(qd: Path, task_id: str) -> Task:
    task = Task.model_validate({"id": task_id, "title": f"Task {task_id}", "prompt": "do thing"})
    write_task_atomic(task, task_path_for(qd, task_id))
    return task


def test_check_task_yamls_empty_passes(settings: Settings, queue_dir: Path) -> None:
    result = check_task_yamls(settings, queue_dir)
    assert result.status == CheckStatus.PASS
    assert "0 valid task YAMLs" in result.detail


def test_check_task_yamls_all_valid(settings: Settings, queue_dir: Path) -> None:
    _make_task(queue_dir, "t1")
    _make_task(queue_dir, "t2")
    result = check_task_yamls(settings, queue_dir)
    assert result.status == CheckStatus.PASS
    assert "2 valid" in result.detail


def test_check_task_yamls_one_invalid(settings: Settings, queue_dir: Path) -> None:
    _make_task(queue_dir, "t1")
    # Write a malformed YAML into todo/.
    bad = queue_dir / "todo" / "bad.yaml"
    bad.write_text("not yaml: ][", encoding="utf-8")
    result = check_task_yamls(settings, queue_dir)
    assert result.status == CheckStatus.FAIL
    assert "1 of 2" in result.detail


# ---------------------------------------------------------------------------
# _extract_paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt, expected",
    [
        ("see /home/user/file.txt for details", {Path("/home/user/file.txt")}),
        (
            "papers at /home/user/a.pdf and /home/user/b.pdf",
            {Path("/home/user/a.pdf"), Path("/home/user/b.pdf")},
        ),
        ("template ${task_id}/output.r is built", set()),  # env-var placeholder
        ("URL https://example.com/path skipped", set()),  # the (?<!://) guard
        ("relative/path/only.txt is fine", set()),  # not absolute
        ("trailing punct /home/x/y.txt,", {Path("/home/x/y.txt")}),
        # Trailing _ and - are stripped from the path, leaving the
        # remainder. /home/x/y_ → /home/x/y.
        ("trailing underscore /home/x/y_", {Path("/home/x/y")}),
    ],
)
def test_extract_paths(prompt: str, expected: set[Path]) -> None:
    assert _extract_paths(prompt) == expected


# ---------------------------------------------------------------------------
# check_task_paths
# ---------------------------------------------------------------------------


def test_check_task_paths_disabled_passes(settings: Settings, queue_dir: Path) -> None:
    result = check_task_paths(settings, queue_dir, enabled=False)
    assert result.status == CheckStatus.PASS
    assert "skipped" in result.detail


def test_check_task_paths_no_paths_referenced(settings: Settings, queue_dir: Path) -> None:
    _make_task(queue_dir, "t1")  # default prompt has no absolute paths
    result = check_task_paths(settings, queue_dir)
    assert result.status == CheckStatus.PASS


def test_check_task_paths_all_present(settings: Settings, queue_dir: Path, tmp_path: Path) -> None:
    existing = tmp_path / "real_input.txt"
    existing.write_text("hello", encoding="utf-8")
    task = Task.model_validate({"id": "t1", "title": "t", "prompt": f"read {existing}"})
    write_task_atomic(task, task_path_for(queue_dir, "t1"))
    result = check_task_paths(settings, queue_dir)
    assert result.status == CheckStatus.PASS


def test_check_task_paths_warns_on_missing(
    settings: Settings, queue_dir: Path, tmp_path: Path
) -> None:
    missing = tmp_path / "no_such_file.txt"
    task = Task.model_validate({"id": "t1", "title": "t", "prompt": f"read {missing}"})
    write_task_atomic(task, task_path_for(queue_dir, "t1"))
    result = check_task_paths(settings, queue_dir)
    assert result.status == CheckStatus.WARN


def test_check_task_paths_skips_unparseable(settings: Settings, queue_dir: Path) -> None:
    """Unparseable task YAMLs are skipped here (check_task_yamls handles them)."""
    bad = queue_dir / "todo" / "bad.yaml"
    bad.write_text("not yaml: ][", encoding="utf-8")
    result = check_task_paths(settings, queue_dir)
    assert result.status == CheckStatus.PASS  # no countable referenced paths


# ---------------------------------------------------------------------------
# check_state_yamls
# ---------------------------------------------------------------------------


def test_check_state_yamls_empty(settings: Settings, queue_dir: Path) -> None:
    result = check_state_yamls(settings, queue_dir)
    assert result.status == CheckStatus.PASS


def test_check_state_yamls_invalid_file(settings: Settings, queue_dir: Path) -> None:
    state_dir = queue_dir / ".claude_task_runner" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "bad.yaml").write_text("not yaml: ][", encoding="utf-8")
    result = check_state_yamls(settings, queue_dir)
    assert result.status == CheckStatus.FAIL


# ---------------------------------------------------------------------------
# check_supervisor_state
# ---------------------------------------------------------------------------


def test_check_supervisor_state_no_file(settings: Settings, queue_dir: Path) -> None:
    result = check_supervisor_state(settings, queue_dir)
    assert result.status == CheckStatus.PASS
    assert "never started" in result.detail


def test_check_supervisor_state_valid(settings: Settings, queue_dir: Path) -> None:
    snap = SupervisorSnapshot.model_validate(
        {
            "state": SupervisorState.IDLE,
            "since": datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC),
        }
    )
    state_path = supervisor_state_path(queue_dir, settings.supervisor.state_file)
    supervisor_write_atomic(snap, state_path)
    result = check_supervisor_state(settings, queue_dir)
    assert result.status == CheckStatus.PASS
    assert "idle" in result.detail


def test_check_supervisor_state_corrupt(settings: Settings, queue_dir: Path) -> None:
    state_path = supervisor_state_path(queue_dir, settings.supervisor.state_file)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("not json", encoding="utf-8")
    result = check_supervisor_state(settings, queue_dir)
    assert result.status == CheckStatus.FAIL


# ---------------------------------------------------------------------------
# check_ema
# ---------------------------------------------------------------------------


def test_check_ema_no_file(settings: Settings, queue_dir: Path) -> None:
    result = check_ema(settings, queue_dir)
    assert result.status == CheckStatus.PASS
    assert "cold start" in result.detail


def test_check_ema_valid_file(settings: Settings, queue_dir: Path) -> None:
    from claude_task_runner.runner.ema import EMA_FILE_NAME

    ema_path = queue_dir / ".claude_task_runner" / EMA_FILE_NAME
    ema_path.parent.mkdir(parents=True, exist_ok=True)
    ema_path.write_text(json.dumps({"schema_version": 2, "buckets": {}}), encoding="utf-8")
    result = check_ema(settings, queue_dir)
    assert result.status == CheckStatus.PASS


def test_check_ema_corrupt(settings: Settings, queue_dir: Path) -> None:
    from claude_task_runner.runner.ema import EMA_FILE_NAME

    ema_path = queue_dir / ".claude_task_runner" / EMA_FILE_NAME
    ema_path.parent.mkdir(parents=True, exist_ok=True)
    ema_path.write_text("not json", encoding="utf-8")
    result = check_ema(settings, queue_dir)
    assert result.status == CheckStatus.FAIL


# ---------------------------------------------------------------------------
# check_skills_installed
# ---------------------------------------------------------------------------


def test_check_skills_installed_all_present(settings: Settings, tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    from claude_task_runner.cli.install_skills_cmd import SKILL_NAMES

    for name in SKILL_NAMES:
        (skills_dir / name).mkdir()
    import contextlib

    with patch.object(Path, "home", return_value=tmp_path.parent):
        # Make .claude/skills resolve to our tmp dir.
        (tmp_path.parent / ".claude").mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileExistsError):
            (tmp_path.parent / ".claude" / "skills").symlink_to(skills_dir)
        result = check_skills_installed(settings)
    assert result.status == CheckStatus.PASS


def test_check_skills_installed_some_missing(settings: Settings, tmp_path: Path) -> None:
    """Pointing at an empty skills dir → WARN."""
    home = tmp_path / "homedir"
    home.mkdir()
    (home / ".claude" / "skills").mkdir(parents=True)
    with patch.object(Path, "home", return_value=home):
        result = check_skills_installed(settings)
    assert result.status == CheckStatus.WARN


# ---------------------------------------------------------------------------
# check_watchdog_installed
# ---------------------------------------------------------------------------


def test_check_watchdog_systemd_present(settings: Settings, tmp_path: Path) -> None:
    fake_unit = tmp_path / "claude-task-runner.service"
    fake_unit.write_text("[Unit]\n", encoding="utf-8")
    with patch(
        "claude_task_runner.doctor.checks.systemd_mod.systemd_unit_path",
        return_value=fake_unit,
    ):
        result = check_watchdog_installed(settings)
    assert result.status == CheckStatus.PASS
    assert "systemd" in result.detail


def test_check_watchdog_none(settings: Settings, tmp_path: Path) -> None:
    with (
        patch(
            "claude_task_runner.doctor.checks.systemd_mod.systemd_unit_path",
            return_value=tmp_path / "nonexistent.service",
        ),
        patch(
            "claude_task_runner.cron.install.crontab_l",
            side_effect=Exception("crontab not available"),
        ),
    ):
        result = check_watchdog_installed(settings)
    assert result.status == CheckStatus.WARN


def test_check_watchdog_cron_present(settings: Settings, tmp_path: Path) -> None:
    """systemd absent; cron has the managed block → PASS (cron).

    The markers use underscores per BEGIN_MARKER / END_MARKER in
    cron/install.py — # BEGIN claude_task_runner / # END claude_task_runner.
    """
    crontab_content = "# BEGIN claude_task_runner\n* * * * * /bin/true\n# END claude_task_runner\n"
    with (
        patch(
            "claude_task_runner.doctor.checks.systemd_mod.systemd_unit_path",
            return_value=tmp_path / "nonexistent.service",
        ),
        patch(
            "claude_task_runner.cron.install.crontab_l",
            return_value=crontab_content,
        ),
    ):
        result = check_watchdog_installed(settings)
    assert result.status == CheckStatus.PASS
    assert "cron" in result.detail


# ---------------------------------------------------------------------------
# all_checks
# ---------------------------------------------------------------------------


def test_all_checks_returns_runnable_callables(settings: Settings, queue_dir: Path) -> None:
    checks = list(all_checks(settings, queue_dir))
    # 15 checks as of multi-account PR1 (added accounts, legacy,
    # account_policies, sudo, perms).
    assert len(checks) >= 15
    # Each is callable and produces a CheckResult.
    for fn in checks:
        result = fn()
        assert hasattr(result, "status")
        assert hasattr(result, "name")


def test_all_checks_can_disable_paths_check(settings: Settings, queue_dir: Path) -> None:
    """The check_paths=False param toggles the task_paths check off."""
    checks_off = list(all_checks(settings, queue_dir, check_paths=False))
    results_off = [fn() for fn in checks_off]
    task_paths_result = next(r for r in results_off if r.name == "task_paths")
    assert "skipped" in task_paths_result.detail


def test_all_checks_api_usage_off_by_default(settings: Settings, queue_dir: Path) -> None:
    """The API usage probe is opt-in so doctor doesn't spend tokens unprompted."""
    checks = list(all_checks(settings, queue_dir))
    results = [fn() for fn in checks]
    assert not any(r.name == "api_usage_source" for r in results)


def test_all_checks_api_usage_on_when_enabled(
    settings: Settings,
    queue_dir: Path,
    tmp_path: Path,
) -> None:
    """--check-api-usage adds the probe; with a missing creds file it
    surfaces as FAIL with a remediation pointing at `claude /login`."""
    from claude_task_runner.config.schema import AccountSettings

    s = settings.model_copy(
        update={
            "accounts": [AccountSettings(name="probe", config_dir=str(tmp_path / "nope"))],
        }
    )
    checks = list(all_checks(s, queue_dir, check_api_usage=True))
    results = [fn() for fn in checks]
    api_result = next(r for r in results if r.name == "api_usage_source")
    assert api_result.status == CheckStatus.FAIL
    assert "claude /login" in api_result.remediation
