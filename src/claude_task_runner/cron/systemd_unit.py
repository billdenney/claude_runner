"""``--user`` systemd unit installer (alternative to cron watchdog).

Per ADR-0002 and the Plan agent's review, systemd is strictly better
than cron when available: native restart policies, journald log
integration, and single-instance enforcement via the unit's own state
machine.

The supervisor runs as a long-lived ``simple`` service with
``Restart=on-failure``. Systemd handles backoff, exit-code tracking,
and signals, so we don't need our :mod:`cron.backoff` module under
systemd. The unit's restart policy comes from the queue's
``[watchdog]`` table instead (see :func:`build_unit_text`). Nothing on
the systemd path runs ``watchdog tick``, and a systemd ``install``
does not register its queue with the cron watchdog.

When operators have neither systemd-as-PID-1 nor ``systemctl --user``
working (e.g. inside Docker without a tmpfiles.d setup), we fall back
to cron. Detection is in :func:`is_systemd_user_available`.
"""

from __future__ import annotations

import logging
import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from claude_task_runner.config.schema import WatchdogSettings

logger = logging.getLogger(__name__)

UNIT_NAME = "claude-task-runner"
"""systemd unit name; appended to ``~/.config/systemd/user/`` as
``<UNIT_NAME>.service``."""

_SYSTEMD_MAX_WHOLE_SECONDS = 18_446_744_073_708
"""The largest whole part of a time span, in seconds, that systemd
accepts: just under 2**64 microseconds. ``systemd-analyze timespan``
(systemd 255) rejects one more with "Numerical result out of range"."""

_SYSTEMD_MAX_UNSIGNED = 2**32 - 1
"""The largest ``StartLimitBurst=`` systemd accepts; one more is
"Failed to parse unsigned value"."""


class SystemdError(RuntimeError):
    """Failure to interact with ``systemctl --user``."""


class UnitSettingError(ValueError):
    """A ``[watchdog]`` value that systemd cannot parse.

    systemd ignores a unit line it cannot parse, with only a journal
    warning, and falls back to its own default: ``RestartSec=100ms``,
    and the manager's ``DefaultStartLimitBurst=`` (5) and
    ``DefaultStartLimitIntervalSec=`` (10 s) unless configured
    otherwise. So such a value is refused before the unit is written."""


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


def _drain_command_from(supervisor_command: str) -> str:
    """Derive the ``supervisor drain`` invocation from the ``supervisor start`` one.

    Reuses the same binary path, ``--queue``, and ``--config`` so the
    unit's stop command can be generated without the caller having to
    pass it explicitly. Appends ``--no-wait`` so the ExecStop process
    returns immediately after signalling SIGUSR1; systemd's own
    main-PID wait (capped by ``TimeoutStopSec``) governs the drain
    duration. Falls back to a substring substitution that does nothing
    if ``supervisor_command`` doesn't include ``supervisor start`` —
    the operator can still hand-edit the unit.
    """
    return supervisor_command.replace(" supervisor start", " supervisor drain") + " --no-wait"


def _stop_command_from(supervisor_command: str) -> str:
    """Derive the ``supervisor stop`` invocation from the ``supervisor start`` one.

    The fast-stop ExecStop used when ``[supervisor].adopt_workers`` is on
    (ADR-0025): ``supervisor stop`` sends a single SIGTERM and returns,
    which trips the daemon's fast-stop handler (stop dispatching, exit
    promptly without joining worker threads). Reuses the same binary
    path, ``--queue``, and ``--config`` as ExecStart. Falls back to a
    no-op substring substitution if ``supervisor start`` isn't present.
    """
    return supervisor_command.replace(" supervisor start", " supervisor stop")


# Short ExecStop timeout for the adoption fast-stop path (ADR-0025).
# The supervisor exits in well under a second once it stops dispatching;
# 30s is a generous bound that still lets `systemctl restart` be
# near-instant instead of waiting out the 4h drain ceiling.
_ADOPT_TIMEOUT_STOP_SEC = 30


def _timespan(seconds: float, key: str) -> str:
    """``seconds`` as a systemd time span, for the ``[watchdog]`` key ``key``.

    Rounded to systemd's resolution of one microsecond, and never in
    exponent form, which systemd rejects (``1e-07``). A whole number
    renders without a fraction (``30.0`` as ``30``), and ``0.25`` as
    ``0.25``. A span under half a microsecond renders as ``0``, which
    systemd reads as no delay (``RestartSec``) or no rate limit
    (``StartLimitIntervalSec``); the cron watchdog treats such a span
    the same way.

    Raises :class:`UnitSettingError` for a span systemd would reject.
    """
    if not math.isfinite(seconds):
        raise UnitSettingError(f"[watchdog].{key} = {seconds} is not a finite number of seconds")
    text = f"{seconds:.6f}".rstrip("0").rstrip(".")
    if int(text.partition(".")[0]) > _SYSTEMD_MAX_WHOLE_SECONDS:
        raise UnitSettingError(
            f"[watchdog].{key} = {seconds} is longer than systemd accepts "
            f"({_SYSTEMD_MAX_WHOLE_SECONDS} s)"
        )
    return text


def _burst(count: int, key: str) -> str:
    """``count`` as a ``StartLimitBurst=`` value, for the ``[watchdog]`` key ``key``.

    Raises :class:`UnitSettingError` for a count systemd would reject.
    """
    if count > _SYSTEMD_MAX_UNSIGNED:
        raise UnitSettingError(
            f"[watchdog].{key} = {count} is more than systemd accepts ({_SYSTEMD_MAX_UNSIGNED})"
        )
    return str(count)


def build_unit_text(
    *,
    supervisor_command: str,
    queue_dir: Path,
    watchdog: WatchdogSettings,
    description: str = "Claude Code task-runner supervisor",
    timeout_stop_sec: int | None = None,
    adopt_workers: bool = True,
) -> str:
    """Build the ``[Unit]/[Service]/[Install]`` text.

    ``supervisor_command`` is the full command line to invoke (e.g.
    ``/home/bill/.venv/bin/claude-task-runner supervisor start --queue
    /home/bill/queue``).

    The restart policy comes from ``watchdog``, the queue's
    ``[watchdog]`` table (the settings :mod:`cron.backoff` takes for the
    cron watchdog):

    * ``RestartSec`` is ``restart_cooldown_s``: how long systemd waits
      after a crash before it restarts the supervisor.
    * ``StartLimitBurst`` is ``crash_loop_threshold`` and
      ``StartLimitIntervalSec`` is ``restart_backoff_max_s``: once the
      unit has been started more than that many times within that many
      seconds, systemd stops restarting it. It can be started by hand
      once the interval has passed, or at once after ``systemctl --user
      reset-failed``. The cron watchdog backs off and retries instead.

    With the package defaults (30 s, 5 and 600 s) the unit is the one
    this function wrote before it read ``[watchdog]``. Seconds are
    rendered by :func:`_timespan`. A value systemd would reject raises
    :class:`UnitSettingError`, so no unit is written that systemd would
    run with its own defaults instead.

    Stop wiring depends on ``adopt_workers`` (ADR-0025):

    * **Adoption ON (default).** ``ExecStop`` calls ``supervisor stop``
      (a single SIGTERM) so ``systemctl stop`` / ``restart`` trips the
      daemon's *fast stop*: it stops dispatching and exits promptly
      without joining worker threads. The file-backed workers keep
      running as independent processes and the next supervisor adopts
      them — so ``TimeoutStopSec`` drops to a short bound
      (:data:`_ADOPT_TIMEOUT_STOP_SEC`) instead of the 4h drain ceiling.
    * **Adoption OFF.** ``ExecStop`` calls ``supervisor drain --no-wait``
      (SIGUSR1) and ``TimeoutStopSec`` stays generous (default 14400s =
      4h, matching ``[task_caps].max_duration_s_per_task``) so the
      graceful drain can finish the longest in-flight task before exit.
      This is the historical PR-11 wiring, unchanged except for the
      ``-`` prefix described below.

    In both cases ``KillMode=process`` keeps systemd from signalling the
    dispatched ``claude`` subprocesses if it ever escalates to SIGKILL
    on the main PID after ``TimeoutStopSec`` — required for adoption so
    the surviving workers aren't killed on supervisor stop, and harmless
    for the drain path. Operators can override ``timeout_stop_sec``
    explicitly; when left ``None`` it defaults per the mode above.

    Also in both cases, ``ExecStop`` carries systemd's ``-`` prefix, so
    systemd ignores its exit status. systemd runs ExecStop even when the
    supervisor has already exited on its own (``supervisor stop`` or a
    finished drain). By then the supervisor has
    removed its PID file, so ``supervisor stop`` and ``supervisor drain``
    exit 1. Without the prefix, that exit 1 would leave the unit
    ``failed (Result: exit-code)`` after every clean exit. It would also
    make ``Restart=on-failure`` restart a supervisor that died of a
    signal systemd counts as clean (SIGHUP, SIGINT, SIGTERM or SIGPIPE).
    The restart would come from the failed ExecStop, not from how the
    supervisor exited.

    For ``systemctl restart``, systemd runs ExecStop, waits for the main
    PID to exit, then starts the unit again. ``Restart=on-failure``
    covers crashes only — it never fires for an operator-driven
    stop/restart.
    """
    if adopt_workers:
        stop_command = _stop_command_from(supervisor_command)
        effective_timeout = (
            timeout_stop_sec if timeout_stop_sec is not None else _ADOPT_TIMEOUT_STOP_SEC
        )
    else:
        stop_command = _drain_command_from(supervisor_command)
        effective_timeout = timeout_stop_sec if timeout_stop_sec is not None else 14400
    restart_sec = _timespan(watchdog.restart_cooldown_s, "restart_cooldown_s")
    start_limit_burst = _burst(watchdog.crash_loop_threshold, "crash_loop_threshold")
    start_limit_interval = _timespan(watchdog.restart_backoff_max_s, "restart_backoff_max_s")
    return (
        "[Unit]\n"
        f"Description={description}\n"
        "After=default.target\n"
        f"StartLimitIntervalSec={start_limit_interval}\n"
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
        # `-`: systemd ignores ExecStop's exit status (see docstring).
        # After a clean exit the supervisor has removed its PID file, so
        # ExecStop still runs and exits 1 ("No PID file"). Without the
        # prefix that marks the unit failed after every clean exit.
        f"ExecStop=-{stop_command}\n"
        f"WorkingDirectory={queue_dir}\n"
        # Stop wiring (see docstring): KillMode=process so dispatched
        # claude subprocesses are never signalled on supervisor stop —
        # essential for the adoption path where they must survive.
        "KillMode=process\n"
        f"TimeoutStopSec={effective_timeout}\n"
        "Restart=on-failure\n"
        f"RestartSec={restart_sec}\n"
        # Don't restart when supervisor exits cleanly (a successful drain
        # or fast-stop).
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
    watchdog: WatchdogSettings,
    unit_path: Path | None = None,
    adopt_workers: bool = True,
) -> SystemdInstallPlan:
    """Compute what installing the systemd unit will do.

    ``watchdog`` is the queue's ``[watchdog]`` table, which sets the
    unit's restart policy (see :func:`build_unit_text`). It has no
    default, so a caller cannot fall back to a hardcoded policy by
    leaving it out. ``adopt_workers`` selects the stop wiring
    (ADR-0025): True (default) wires the fast-stop ExecStop + short
    ``TimeoutStopSec``; False keeps the graceful-drain ExecStop + 4h
    timeout. The CLI passes the queue's ``[supervisor].adopt_workers``
    so the generated unit matches the runtime behaviour.

    Raises :class:`UnitSettingError` for a ``[watchdog]`` value that
    systemd cannot parse.
    """
    target = unit_path if unit_path is not None else systemd_unit_path()
    unit_text = build_unit_text(
        supervisor_command=supervisor_command,
        queue_dir=queue_dir,
        watchdog=watchdog,
        adopt_workers=adopt_workers,
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

    Errors during ``disable``/``daemon-reload`` are tolerated (the unit
    may already be gone) but no longer silent: a non-zero return code is
    logged at WARNING with the failing command and stderr. A silent
    ``disable`` failure would otherwise leave the unit enabled/active
    even though the operator asked to uninstall it. Failure to remove an
    existing unit file still raises (via ``unlink``).
    """
    target = unit_path if unit_path is not None else systemd_unit_path()
    existed = target.exists()
    if shutil.which(systemctl_executable):
        for argv in (
            [systemctl_executable, "--user", "disable", "--now", f"{UNIT_NAME}.service"],
            [systemctl_executable, "--user", "daemon-reload"],
        ):
            proc = subprocess.run(argv, capture_output=True, text=True, check=False)
            if proc.returncode != 0:
                logger.warning(
                    "systemctl uninstall step %r returned %d: %s",
                    " ".join(argv),
                    proc.returncode,
                    (proc.stderr or "").strip(),
                )
    if existed:
        target.unlink()
    return existed
