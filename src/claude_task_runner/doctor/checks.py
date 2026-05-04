"""Self-diagnostic battery — operator's first stop when something feels off.

Each check is a small, pure-ish function that returns a
:class:`CheckResult`. The CLI runs them in order and prints PASS /
FAIL / WARN per check. Exit code is non-zero on any FAIL.

Checks are deliberately isolated so they survive partial failures:
a corrupt ``ema.json`` shouldn't prevent the binary-existence check
from running.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from claude_task_runner.config.schema import Settings
from claude_task_runner.cron import systemd_unit as systemd_mod
from claude_task_runner.queue.store import (
    QueueIOError,
    QueueSchemaError,
    list_pending_tasks,
    list_state_files,
    load_state,
    load_task,
    queue_runtime_dir,
)
from claude_task_runner.runner import ema as ema_mod
from claude_task_runner.runner.ema import EMAFileError
from claude_task_runner.supervisor import persistence as persist_mod
from claude_task_runner.supervisor import pidfile as pidfile_mod


class CheckStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one check.

    Attributes
    ----------
    name
        Short label for telemetry / display.
    status
        PASS / WARN / FAIL.
    detail
        Human-readable one-liner.
    remediation
        Optional next step the operator can take. Empty when the
        check passed.
    """

    name: str
    status: CheckStatus
    detail: str
    remediation: str = ""


def check_claude_binary(settings: Settings) -> CheckResult:
    """The ``claude`` binary must be on PATH."""
    exe = shutil.which(settings.claude.executable)
    if exe is None:
        return CheckResult(
            name="claude_binary",
            status=CheckStatus.FAIL,
            detail=f"{settings.claude.executable!r} not found on PATH",
            remediation="Install Claude Code, or set [claude].executable.",
        )
    return CheckResult(
        name="claude_binary",
        status=CheckStatus.PASS,
        detail=f"found at {exe}",
    )


def check_claude_config_dir(settings: Settings) -> CheckResult:
    """If ``[claude].config_dir`` is set, it must exist."""
    if not settings.claude.config_dir:
        return CheckResult(
            name="claude_config_dir",
            status=CheckStatus.PASS,
            detail="using default ~/.claude",
        )
    path = Path(settings.claude.config_dir).expanduser()
    if not path.exists():
        return CheckResult(
            name="claude_config_dir",
            status=CheckStatus.FAIL,
            detail=f"configured CLAUDE_CONFIG_DIR does not exist: {path}",
            remediation=(
                f"Create the directory and run `CLAUDE_CONFIG_DIR={path} claude /login` once."
            ),
        )
    creds = path / ".credentials.json"
    if not creds.exists():
        return CheckResult(
            name="claude_config_dir",
            status=CheckStatus.WARN,
            detail=f"{path} exists but has no .credentials.json",
            remediation=(f"Run `CLAUDE_CONFIG_DIR={path} claude /login` to authenticate."),
        )
    return CheckResult(
        name="claude_config_dir",
        status=CheckStatus.PASS,
        detail=f"{path}",
    )


def check_global_lock(_settings: Settings) -> CheckResult:
    """No stale or orphan global lock file."""
    path = pidfile_mod.global_lock_path()
    if not path.exists():
        return CheckResult(
            name="global_lock",
            status=CheckStatus.PASS,
            detail="no lock file (no supervisor running)",
        )
    pid = pidfile_mod.read_existing_pid(path)
    if pid is None:
        return CheckResult(
            name="global_lock",
            status=CheckStatus.WARN,
            detail=f"lock file exists at {path} but PID is unreadable",
            remediation=f"Inspect or remove: {path}",
        )
    if not pidfile_mod.is_pid_alive(pid):
        return CheckResult(
            name="global_lock",
            status=CheckStatus.WARN,
            detail=f"lock file holds PID {pid} which is not alive (stale)",
            remediation=(
                f"Remove the stale lock: rm {path}\n"
                "  Then re-run `claude-task-runner supervisor start`."
            ),
        )
    return CheckResult(
        name="global_lock",
        status=CheckStatus.PASS,
        detail=f"held by live PID {pid}",
    )


def check_queue_layout(_settings: Settings, queue_dir: Path) -> CheckResult:
    """The queue's ``todo/`` and ``.claude_task_runner/`` are valid."""
    todo = queue_dir / "todo"
    runtime = queue_dir / ".claude_task_runner"
    missing: list[str] = []
    if not queue_dir.exists():
        return CheckResult(
            name="queue_layout",
            status=CheckStatus.FAIL,
            detail=f"queue dir not found: {queue_dir}",
            remediation=f"mkdir -p {queue_dir}/todo",
        )
    if not todo.exists():
        missing.append("todo/")
    if not runtime.exists():
        missing.append(".claude_task_runner/")
    if missing:
        # Auto-create runtime; flag missing todo only.
        if ".claude_task_runner/" in missing:
            queue_runtime_dir(queue_dir)
            missing.remove(".claude_task_runner/")
        if missing:
            return CheckResult(
                name="queue_layout",
                status=CheckStatus.WARN,
                detail=f"missing dirs in {queue_dir}: {missing}",
                remediation=f"mkdir -p {queue_dir}/{missing[0]}",
            )
    return CheckResult(
        name="queue_layout",
        status=CheckStatus.PASS,
        detail=str(queue_dir),
    )


def check_task_yamls(_settings: Settings, queue_dir: Path) -> CheckResult:
    """Every YAML in ``todo/`` validates against the schema."""
    bad: list[str] = []
    count = 0
    for path in list_pending_tasks(queue_dir):
        count += 1
        try:
            load_task(path)
        except (QueueIOError, QueueSchemaError) as exc:
            bad.append(f"{path.name}: {exc}")
    if bad:
        return CheckResult(
            name="task_yamls",
            status=CheckStatus.FAIL,
            detail=f"{len(bad)} of {count} task YAMLs invalid",
            remediation="\n".join(bad),
        )
    return CheckResult(
        name="task_yamls",
        status=CheckStatus.PASS,
        detail=f"{count} valid task YAMLs in todo/",
    )


def check_state_yamls(_settings: Settings, queue_dir: Path) -> CheckResult:
    """Every YAML in ``state/`` validates."""
    bad: list[str] = []
    count = 0
    for path in list_state_files(queue_dir):
        count += 1
        try:
            load_state(path)
        except (QueueIOError, QueueSchemaError) as exc:
            bad.append(f"{path.name}: {exc}")
    if bad:
        return CheckResult(
            name="state_yamls",
            status=CheckStatus.FAIL,
            detail=f"{len(bad)} of {count} state YAMLs invalid",
            remediation="\n".join(bad),
        )
    return CheckResult(
        name="state_yamls",
        status=CheckStatus.PASS,
        detail=f"{count} valid state YAMLs",
    )


def check_supervisor_state(settings: Settings, queue_dir: Path) -> CheckResult:
    """The supervisor's ``supervisor.json`` (if present) parses cleanly."""
    path = persist_mod.supervisor_state_path(queue_dir, settings.supervisor.state_file)
    if not path.exists():
        return CheckResult(
            name="supervisor_state",
            status=CheckStatus.PASS,
            detail="no supervisor.json (never started here)",
        )
    try:
        snap = persist_mod.load(path)
    except persist_mod.SupervisorPersistenceError as exc:
        return CheckResult(
            name="supervisor_state",
            status=CheckStatus.FAIL,
            detail=str(exc),
            remediation=f"Inspect / remove the corrupt file: {path}",
        )
    if snap is None:
        # Shouldn't happen since path.exists() — defensive.
        return CheckResult(
            name="supervisor_state",
            status=CheckStatus.WARN,
            detail=f"{path} loaded as None unexpectedly",
        )
    return CheckResult(
        name="supervisor_state",
        status=CheckStatus.PASS,
        detail=f"state={snap.state.value}, since={snap.since}",
    )


def check_ema(_settings: Settings, queue_dir: Path) -> CheckResult:
    """``ema.json`` parses cleanly (or doesn't exist yet)."""
    path = queue_dir / ".claude_task_runner" / ema_mod.EMA_FILE_NAME
    if not path.exists():
        return CheckResult(
            name="ema",
            status=CheckStatus.PASS,
            detail="no ema.json yet (cold start)",
        )
    try:
        ema = ema_mod.load(path)
    except EMAFileError as exc:
        return CheckResult(
            name="ema",
            status=CheckStatus.FAIL,
            detail=str(exc),
            remediation=f"Remove or fix {path}",
        )
    return CheckResult(
        name="ema",
        status=CheckStatus.PASS,
        detail=f"{len(ema.buckets)} task-type buckets",
    )


def check_skills_installed(_settings: Settings) -> CheckResult:
    """Skills should be present in ``~/.claude/skills/``."""
    from claude_task_runner.cli.install_skills_cmd import SKILL_NAMES

    target = Path.home() / ".claude" / "skills"
    missing = [n for n in SKILL_NAMES if not (target / n).exists()]
    if missing:
        return CheckResult(
            name="skills_installed",
            status=CheckStatus.WARN,
            detail=f"{len(missing)} of {len(SKILL_NAMES)} skills not installed",
            remediation=(f"Run `claude-task-runner install-skills --yes`. Missing: {missing}"),
        )
    return CheckResult(
        name="skills_installed",
        status=CheckStatus.PASS,
        detail=f"all {len(SKILL_NAMES)} task-runner skills present at {target}",
    )


def check_watchdog_installed(settings: Settings) -> CheckResult:
    """Either a systemd unit or a cron managed-block should exist."""
    systemd_present = systemd_mod.systemd_unit_path().exists()

    # Try to read the crontab non-destructively. If `crontab(1)` is
    # missing, that's an environment limitation rather than a runner
    # failure, so WARN rather than FAIL.
    cron_present = False
    try:
        from claude_task_runner.cron.install import BLOCK_RE, crontab_l

        existing = crontab_l()
        cron_present = BLOCK_RE.search(existing) is not None
    except Exception:
        pass

    if systemd_present or cron_present:
        kind = "systemd" if systemd_present else "cron"
        return CheckResult(
            name="watchdog_installed",
            status=CheckStatus.PASS,
            detail=f"{kind} watchdog detected",
        )

    preferred = settings.supervisor.preferred_init_system
    return CheckResult(
        name="watchdog_installed",
        status=CheckStatus.WARN,
        detail="no watchdog (systemd or cron) detected",
        remediation=(
            f"Run `claude-task-runner install --queue <PATH>` (preferred init: {preferred})."
        ),
    )


def all_checks(settings: Settings, queue_dir: Path) -> Iterable[Callable[[], CheckResult]]:
    """Return zero-arg callables, in the order to run them."""
    return [
        lambda: check_claude_binary(settings),
        lambda: check_claude_config_dir(settings),
        lambda: check_global_lock(settings),
        lambda: check_queue_layout(settings, queue_dir),
        lambda: check_task_yamls(settings, queue_dir),
        lambda: check_state_yamls(settings, queue_dir),
        lambda: check_supervisor_state(settings, queue_dir),
        lambda: check_ema(settings, queue_dir),
        lambda: check_skills_installed(settings),
        lambda: check_watchdog_installed(settings),
    ]
