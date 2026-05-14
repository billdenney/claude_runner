"""``--user`` systemd unit installer (alternative to cron watchdog).

Per ADR-0002 and the Plan agent's review, systemd is strictly better
than cron when available: native restart policies, journald log
integration, and single-instance enforcement via the unit's own state
machine.

The supervisor runs as a long-lived ``simple`` service with
``Restart=on-failure``. Systemd handles backoff, exit-code tracking,
and signals — we don't need our :mod:`cron.backoff` module under
systemd, but it's still consulted by the watchdog timer for
parity-of-policy if operators have BOTH installed.

When operators have neither systemd-as-PID-1 nor ``systemctl --user``
working (e.g. inside Docker without a tmpfiles.d setup), we fall back
to cron. Detection is in :func:`is_systemd_user_available`.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

UNIT_NAME = "claude-task-runner"
"""systemd unit name; appended to ``~/.config/systemd/user/`` as
``<UNIT_NAME>.service``."""


class SystemdError(RuntimeError):
    """Failure to interact with ``systemctl --user``."""


@dataclass(frozen=True)
class SystemdInstallPlan:
    """Proposed systemd unit installation.

    Attributes
    ----------
    unit_path
        Where the unit file will be written.
    unit_text
        Full ``[Unit]/[Service]/[Install]`` content.
    enable_command
        The exact ``systemctl --user`` command sequence the operator
        will run after confirming.
    block_existed
        Whether a unit with this name already exists at ``unit_path``.
    """

    unit_path: Path
    unit_text: str
    enable_command: list[str]
    block_existed: bool


def is_systemd_user_available(systemctl_executable: str = "systemctl") -> bool:
    """Heuristic: can we use ``systemctl --user``?

    Checks both that ``systemctl`` is on PATH AND that ``--user`` mode
    actually responds. Returns False on any failure (missing binary,
    no D-Bus session, container without systemd, etc.).
    """
    if shutil.which(systemctl_executable) is None:
        return False
    try:
        proc = subprocess.run(
            [systemctl_executable, "--user", "is-system-running"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    # ``is-system-running`` prints status text and exits 0/non-0 based
    # on whether the user manager is healthy. Even ``degraded`` (exit
    # 1) means user units are usable. We just need NOT-EXIT-127 and not
    # the "Failed to connect to user scope bus" stderr.
    if proc.returncode in (0, 1):
        return "running" in proc.stdout or "degraded" in proc.stdout
    return False


def systemd_unit_dir() -> Path:
    """Resolve ``~/.config/systemd/user/``."""
    return Path.home() / ".config" / "systemd" / "user"


def systemd_unit_path() -> Path:
    """Resolve ``<systemd_unit_dir>/<UNIT_NAME>.service``."""
    return systemd_unit_dir() / f"{UNIT_NAME}.service"


def build_unit_text(
    *,
    supervisor_command: str,
    queue_dir: Path,
    description: str = "Claude Code task-runner supervisor",
    restart_sec_s: int = 30,
    start_limit_burst: int = 5,
    start_limit_interval_s: int = 600,
) -> str:
    """Build the ``[Unit]/[Service]/[Install]`` text.

    ``supervisor_command`` is the full command line to invoke (e.g.
    ``/home/bill/.venv/bin/claude-task-runner supervisor start --queue
    /home/bill/queue``). systemd's restart policy gives us crash-loop
    protection for free; we additionally cap with ``StartLimitBurst``
    so a hopelessly broken supervisor doesn't churn forever.
    """
    return (
        "[Unit]\n"
        f"Description={description}\n"
        "After=default.target\n"
        f"StartLimitIntervalSec={start_limit_interval_s}\n"
        f"StartLimitBurst={start_limit_burst}\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        # systemd-user units start with a near-empty environment. The
        # supervisor's `usage capture` spawns `claude` via pexpect, which
        # needs (a) a working TERM for the TUI to render at all, and (b)
        # PATH to include `~/.local/bin` so pipx-installed Claude binaries
        # resolve under `shutil.which("claude")`. Without these,
        # safe_poll() returns UsageCaptureSpawnError every tick and the
        # supervisor sits in IDLE forever even though the queue has work.
        # Operators can override with `systemctl --user edit
        # claude-task-runner.service` if their setup differs.
        "Environment=TERM=xterm-256color\n"
        "Environment=PATH=%h/.local/bin:/usr/local/sbin:"
        "/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
        f"ExecStart={supervisor_command}\n"
        f"WorkingDirectory={queue_dir}\n"
        "Restart=on-failure\n"
        f"RestartSec={restart_sec_s}\n"
        # Don't restart when supervisor exits cleanly (e.g., STOPPED state).
        "RestartPreventExitStatus=0\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def build_install_plan(
    *,
    supervisor_command: str,
    queue_dir: Path,
    unit_path: Path | None = None,
    restart_sec_s: int = 30,
    start_limit_burst: int = 5,
) -> SystemdInstallPlan:
    """Compute what installing the systemd unit will do."""
    target = unit_path if unit_path is not None else systemd_unit_path()
    unit_text = build_unit_text(
        supervisor_command=supervisor_command,
        queue_dir=queue_dir,
        restart_sec_s=restart_sec_s,
        start_limit_burst=start_limit_burst,
    )
    enable_command = [
        "systemctl",
        "--user",
        "enable",
        "--now",
        f"{UNIT_NAME}.service",
    ]
    return SystemdInstallPlan(
        unit_path=target,
        unit_text=unit_text,
        enable_command=enable_command,
        block_existed=target.exists(),
    )


def apply_plan(
    plan: SystemdInstallPlan,
    *,
    systemctl_executable: str = "systemctl",
    daemon_reload: bool = True,
) -> None:
    """Write the unit file and run ``systemctl --user enable --now``.

    ``daemon_reload=True`` is the safe default — required when an
    existing unit text was changed. Tests can disable it to avoid the
    side effect.
    """
    plan.unit_path.parent.mkdir(parents=True, exist_ok=True)
    plan.unit_path.write_text(plan.unit_text)

    if shutil.which(systemctl_executable) is None:
        raise SystemdError(f"{systemctl_executable!r} not found on PATH")

    if daemon_reload:
        proc = subprocess.run(
            [systemctl_executable, "--user", "daemon-reload"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise SystemdError(f"daemon-reload failed: {proc.stderr.strip()}")

    # Substitute systemctl_executable for the leading "systemctl" in the
    # enable command so tests can pass a fake binary.
    enable_argv = [systemctl_executable, *plan.enable_command[1:]]
    proc = subprocess.run(
        enable_argv,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemdError(f"{' '.join(enable_argv)} failed: {proc.stderr.strip()}")


def uninstall(
    *,
    unit_path: Path | None = None,
    systemctl_executable: str = "systemctl",
) -> bool:
    """Disable + stop the unit and remove its file. Returns True if the
    unit existed and was removed; False if there was nothing to do.

    Errors during ``disable``/``stop`` are tolerated (the unit may
    already be gone), but failure to remove an existing unit file
    raises.
    """
    target = unit_path if unit_path is not None else systemd_unit_path()
    existed = target.exists()
    if shutil.which(systemctl_executable):
        for argv in (
            [systemctl_executable, "--user", "disable", "--now", f"{UNIT_NAME}.service"],
            [systemctl_executable, "--user", "daemon-reload"],
        ):
            subprocess.run(argv, capture_output=True, text=True, check=False)
    if existed:
        target.unlink()
    return existed
