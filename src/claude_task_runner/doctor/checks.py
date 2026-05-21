"""Self-diagnostic battery — operator's first stop when something feels off.

Each check is a small, pure-ish function that returns a
:class:`CheckResult`. The CLI runs them in order and prints PASS /
FAIL / WARN per check. Exit code is non-zero on any FAIL.

Checks are deliberately isolated so they survive partial failures:
a corrupt ``ema.json`` shouldn't prevent the binary-existence check
from running.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from claude_task_runner.config.loader import (
    ConfigError,
    load_account_policy,
    per_account_toml_path,
)
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


def check_accounts(settings: Settings) -> CheckResult:
    """Every configured account's ``config_dir`` exists and is authenticated.

    Iterates ``settings.accounts`` (which the loader always populates —
    legacy ``[claude].config_dir`` is folded into a single
    ``"default"`` entry). FAILs on any missing directory; WARNs on a
    directory without ``.credentials.json``; PASSes when all accounts
    are wired up.
    """
    if not settings.accounts:
        # The model validator forbids this; defensive guard for type
        # checkers and any future loader change.
        return CheckResult(
            name="accounts",
            status=CheckStatus.FAIL,
            detail="no [[accounts]] configured",
            remediation="Add at least one [[accounts]] block to claude_runner.toml.",
        )

    fails: list[str] = []
    warns: list[str] = []
    oks: list[str] = []
    for acct in settings.accounts:
        if not acct.config_dir:
            oks.append(f"{acct.name} (default ~/.claude)")
            continue
        path = Path(acct.config_dir).expanduser()
        if not path.exists():
            fails.append(
                f"{acct.name}: CLAUDE_CONFIG_DIR does not exist: {path}\n"
                f"      Fix: mkdir -p {path} && "
                f"CLAUDE_CONFIG_DIR={path} claude /login"
            )
            continue
        creds = path / ".credentials.json"
        if not creds.exists():
            warns.append(
                f"{acct.name}: {path} exists but has no .credentials.json\n"
                f"      Fix: CLAUDE_CONFIG_DIR={path} claude /login"
            )
            continue
        oks.append(f"{acct.name} -> {path}")

    if fails:
        return CheckResult(
            name="accounts",
            status=CheckStatus.FAIL,
            detail=f"{len(fails)} of {len(settings.accounts)} accounts have missing dirs",
            remediation="\n".join(fails + warns),
        )
    if warns:
        return CheckResult(
            name="accounts",
            status=CheckStatus.WARN,
            detail=f"{len(warns)} of {len(settings.accounts)} accounts not authenticated",
            remediation="\n".join(warns),
        )
    return CheckResult(
        name="accounts",
        status=CheckStatus.PASS,
        detail=f"{len(oks)} accounts: " + ", ".join(oks),
    )


def check_legacy_claude_config_dir(settings: Settings) -> CheckResult:
    """WARN when ``[claude].config_dir`` is set alongside explicit ``[[accounts]]``.

    The legacy field is ignored when ``[[accounts]]`` is declared
    explicitly. Surfacing the conflict here lets operators clean up
    their TOMLs rather than wondering why the legacy value has no
    effect.
    """
    legacy = settings.claude.config_dir
    explicit_accounts = [a for a in settings.accounts if not _looks_like_legacy_default(a, legacy)]
    if not legacy:
        return CheckResult(
            name="legacy_claude_config_dir",
            status=CheckStatus.PASS,
            detail="no legacy [claude].config_dir set",
        )
    if not explicit_accounts:
        # Single-account back-compat: the legacy field WAS the source
        # of the synthesised account. That's the supported path; not a
        # warning.
        return CheckResult(
            name="legacy_claude_config_dir",
            status=CheckStatus.PASS,
            detail=f"[claude].config_dir={legacy!r} -> synthesised account 'default'",
        )
    return CheckResult(
        name="legacy_claude_config_dir",
        status=CheckStatus.WARN,
        detail=(
            f"[claude].config_dir={legacy!r} is ignored because explicit [[accounts]] are declared"
        ),
        remediation=(
            "Remove [claude].config_dir from claude_runner.toml — the "
            "explicit [[accounts]] list supersedes it."
        ),
    )


def _looks_like_legacy_default(acct: object, legacy_config_dir: str) -> bool:
    """Heuristic: is this account the loader's synthesised default?

    Returns True for an account with name=='default' and the same
    config_dir as the legacy field. Lets ``check_legacy_claude_config_dir``
    distinguish "legacy was folded in" from "operator wrote both".
    """
    name = getattr(acct, "name", None)
    config_dir = getattr(acct, "config_dir", None)
    return name == "default" and config_dir == legacy_config_dir


def _current_username() -> str:
    """Return the supervisor's own Linux username; empty if unknown."""
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        return ""


def check_account_policies(settings: Settings) -> CheckResult:
    """Each account's ``<config_dir>/runner-account.toml`` parses; report resolved policy.

    Walks ``settings.accounts``; for each one tries to load the per-
    account policy. A missing file is informational (defaults apply);
    a present-but-unparseable file is FAIL. The detail string is a
    one-line-per-account summary of resolved ``max_concurrency`` and
    band thresholds so the operator can verify what the supervisor
    will use.
    """
    if not settings.accounts:
        return CheckResult(
            name="account_policies",
            status=CheckStatus.PASS,
            detail="no [[accounts]] configured (defaults apply)",
        )

    fails: list[str] = []
    rows: list[str] = []
    for acct in settings.accounts:
        path = per_account_toml_path(acct.config_dir)
        try:
            policy = load_account_policy(acct.config_dir)
        except ConfigError as exc:
            fails.append(f"{acct.name}: {exc}")
            continue
        source = "defaults" if path is None or not path.exists() else str(path)
        rows.append(
            f"{acct.name}: max_concurrency={policy.concurrency.max_concurrency}, "
            f"daytime={policy.throttle.five_hour.daytime_band_full_dispatch_max_pct}/"
            f"{policy.throttle.five_hour.daytime_band_slowdown_max_pct}, "
            f"nighttime={policy.throttle.five_hour.nighttime_band_full_dispatch_max_pct}/"
            f"{policy.throttle.five_hour.nighttime_band_slowdown_max_pct}, "
            f"day_end={policy.throttle.time_of_day.day_end} "
            f"({source})"
        )

    if fails:
        return CheckResult(
            name="account_policies",
            status=CheckStatus.FAIL,
            detail=f"{len(fails)} of {len(settings.accounts)} per-account policies invalid",
            remediation="\n".join(fails),
        )
    return CheckResult(
        name="account_policies",
        status=CheckStatus.PASS,
        detail=" | ".join(rows),
    )


def check_account_sudo(settings: Settings) -> CheckResult:
    """Each account with ``linux_user`` set has passwordless sudo wired up.

    Runs ``sudo -n -u <linux_user> /bin/true`` for every account whose
    ``linux_user`` differs from the supervisor's own user. ``-n`` makes
    the sudo call non-interactive: if a password would be prompted,
    sudo exits non-zero immediately rather than hanging.

    PASSes silently (with the count) when no account uses
    ``linux_user``. FAILs on the first sudo failure, listing every
    account that needs fixing.
    """
    targets = [a for a in settings.accounts if a.linux_user]
    if not targets:
        return CheckResult(
            name="account_sudo",
            status=CheckStatus.PASS,
            detail="no accounts use linux_user (single-user supervisor)",
        )

    self_user = _current_username()
    sudo_path = shutil.which("sudo")
    if sudo_path is None:
        return CheckResult(
            name="account_sudo",
            status=CheckStatus.FAIL,
            detail="sudo binary not on PATH but accounts request linux_user",
            remediation="Install sudo or remove linux_user from [[accounts]].",
        )

    failures: list[str] = []
    same_user: list[str] = []
    ok: list[str] = []
    for acct in targets:
        target = acct.linux_user or ""
        if target == self_user:
            same_user.append(f"{acct.name} (linux_user={target!r} == supervisor user)")
            continue
        try:
            completed = subprocess.run(
                [sudo_path, "-n", "-u", target, "/bin/true"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            failures.append(f"{acct.name}: sudo invocation raised {type(exc).__name__}: {exc}")
            continue
        if completed.returncode == 0:
            ok.append(f"{acct.name} -> {target}")
        else:
            stderr_tail = (completed.stderr or "").strip().splitlines()[-1:] or [""]
            failures.append(
                f"{acct.name}: sudo -n -u {target} failed (rc={completed.returncode}): "
                f"{stderr_tail[0]}"
            )

    if failures:
        suid = self_user or "<unknown>"
        remediation = (
            "Configure passwordless sudo for the supervisor user "
            f"({suid}) to each target user. Recommended snippet in "
            "/etc/sudoers.d/claude-task-runner:\n"
            + "\n".join(
                f"  {suid} ALL=({t}) NOPASSWD: ALL"
                for t in sorted({a.linux_user for a in targets if a.linux_user})
            )
            + "\n\nFailures:\n  "
            + "\n  ".join(failures)
        )
        return CheckResult(
            name="account_sudo",
            status=CheckStatus.FAIL,
            detail=f"sudo unreachable for {len(failures)} of {len(targets)} multi-user accounts",
            remediation=remediation,
        )
    detail_parts = []
    if ok:
        detail_parts.append(f"{len(ok)} verified: " + ", ".join(ok))
    if same_user:
        detail_parts.append(
            f"{len(same_user)} no-op (same as supervisor user): " + ", ".join(same_user)
        )
    return CheckResult(
        name="account_sudo",
        status=CheckStatus.PASS,
        detail="; ".join(detail_parts) or "all targets resolved",
    )


def check_queue_perms_for_linux_users(settings: Settings, queue_dir: Path) -> CheckResult:
    """Queue dir is reachable by every configured ``linux_user``.

    When at least one account has ``linux_user`` set, the queue
    directory and key subdirectories (``todo/``,
    ``.claude_task_runner/state/``, ``.claude_task_runner/sidecar/``)
    must be readable+writable by every target uid. We check by
    verifying the queue dir's group is one each linux_user belongs to,
    and that the dir is group-writable. The setgid bit is checked so
    new files inherit the group.

    Returns PASS (skipped) when no account uses ``linux_user``. The
    check is informational rather than authoritative — final perms
    enforcement lives at the kernel level when files are written. The
    suggested fix command always names absolute paths so the operator
    can copy/paste.
    """
    targets = sorted({a.linux_user for a in settings.accounts if a.linux_user})
    if not targets:
        return CheckResult(
            name="queue_perms_multi_user",
            status=CheckStatus.PASS,
            detail="no accounts use linux_user (perms check skipped)",
        )

    if not queue_dir.exists():
        return CheckResult(
            name="queue_perms_multi_user",
            status=CheckStatus.FAIL,
            detail=f"queue dir not found: {queue_dir}",
            remediation=f"mkdir -p {queue_dir}/todo",
        )

    issues: list[str] = []
    fix_lines: list[str] = []

    try:
        import grp
        import pwd
    except ImportError:  # pragma: no cover — non-POSIX platforms
        return CheckResult(
            name="queue_perms_multi_user",
            status=CheckStatus.WARN,
            detail="grp/pwd modules unavailable; cannot verify multi-user perms",
        )

    # Each linux_user's group memberships (gid set).
    user_gids: dict[str, set[int]] = {}
    for user in targets:
        try:
            pw = pwd.getpwnam(user)
        except KeyError:
            issues.append(f"linux_user {user!r} does not exist on this host")
            continue
        gids = {pw.pw_gid}
        # Supplementary groups.
        for g in grp.getgrall():
            if user in g.gr_mem:
                gids.add(g.gr_gid)
        user_gids[user] = gids

    # For each key path, verify the group is in every configured user's
    # gid set AND the group-write bit is on.
    paths_to_check: list[Path] = [
        queue_dir,
        queue_dir / "todo",
        queue_dir / ".claude_task_runner",
        queue_dir / ".claude_task_runner" / "state",
        queue_dir / ".claude_task_runner" / "sidecar",
    ]
    target_group = None
    for path in paths_to_check:
        if not path.exists():
            # The runner auto-creates state/ and sidecar/ at startup,
            # so a missing path here is a fresh queue; defer to
            # check_queue_layout's WARN rather than double-counting.
            continue
        st = path.stat()
        if target_group is None:
            target_group = st.st_gid
        mode = st.st_mode
        if not (mode & 0o060):
            issues.append(
                f"{path}: not group-writable (mode={oct(mode & 0o777)}). "
                "Newly-created files will not inherit write access for other "
                "accounts running under linux_user."
            )
        # Reject paths whose group differs from the queue root's group.
        if st.st_gid != target_group:
            try:
                gname = grp.getgrgid(st.st_gid).gr_name
            except KeyError:
                gname = str(st.st_gid)
            issues.append(
                f"{path}: group {gname!r} differs from queue root group; "
                "linux_user accounts may lose access on file creation."
            )
        if path.is_dir() and not (mode & 0o2000):
            issues.append(
                f"{path}: missing setgid bit; new files will not inherit the queue group."
            )

    if target_group is not None:
        try:
            queue_group_name = grp.getgrgid(target_group).gr_name
        except KeyError:
            queue_group_name = str(target_group)
        for user, gids in user_gids.items():
            if target_group not in gids:
                issues.append(
                    f"linux_user {user!r} is not a member of the queue's group {queue_group_name!r}."
                )

    if issues:
        if target_group is not None:
            try:
                queue_group_name = grp.getgrgid(target_group).gr_name
            except KeyError:
                queue_group_name = str(target_group)
        else:
            queue_group_name = "<shared-group>"
        fix_lines = [
            f"sudo groupadd -f {queue_group_name}",
            *[f"sudo usermod -aG {queue_group_name} {u}" for u in targets],
            f"sudo chgrp -R {queue_group_name} {queue_dir}",
            f"sudo chmod -R g+rwX {queue_dir}",
            f"sudo find {queue_dir} -type d -exec chmod g+s {{}} +",
        ]
        return CheckResult(
            name="queue_perms_multi_user",
            status=CheckStatus.FAIL,
            detail=f"{len(issues)} perms issues for multi-user dispatch",
            remediation="Issues:\n  "
            + "\n  ".join(issues)
            + "\n\nSuggested fix:\n  "
            + "\n  ".join(fix_lines),
        )
    return CheckResult(
        name="queue_perms_multi_user",
        status=CheckStatus.PASS,
        detail=f"queue dir group + setgid OK for {len(targets)} multi-user accounts",
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


# Absolute paths embedded in task prompts. Restricted to filesystem-rooted
# directories we actually want to validate; skips URLs, env-var placeholders,
# and template fragments like `${task_id}`.
_PROMPT_PATH_RE = re.compile(
    r"(?<!://)"
    r"/(?:home|gitlab|github|var|tmp|opt|usr|etc|mnt|srv|run|data)"
    r"/[A-Za-z0-9_.][\w./+\-]*"
)
# Trailing punctuation that prose puts after a path but isn't part of it.
# ``_`` and ``-`` are stripped only when trailing — they almost always signal
# a documentation fragment like ``_supplement_{1..N}.docx`` where the regex
# stopped at the brace-expansion boundary.
_PROMPT_PATH_TRIM = ".,:;)`\"'_-"


def _extract_paths(prompt: str) -> set[Path]:
    """Pull absolute-looking filesystem paths out of a task prompt string."""
    found: set[Path] = set()
    for raw in _PROMPT_PATH_RE.findall(prompt):
        stripped = raw.rstrip(_PROMPT_PATH_TRIM)
        # Skip env-var / shell-expansion placeholders, and anything that
        # ended up with no extension AND no path component after the last
        # ``/`` — those are usually directory-like sentinel matches against
        # an opening brace that the regex didn't include.
        if not stripped or "${" in stripped or "$(" in stripped:
            continue
        found.add(Path(stripped))
    return found


def check_task_paths(
    _settings: Settings,
    queue_dir: Path,
    *,
    enabled: bool = True,
) -> CheckResult:
    """Each absolute path referenced inside a Task prompt exists on disk.

    Default-on; disable with ``claude-task-runner doctor --no-check-paths``.
    A missing path is **WARN**, not FAIL: prompts also reference
    yet-to-be-created output paths (reports, model files), and there's no
    cheap way to tell input from output. The remediation lists the
    offenders so the operator can triage.
    """
    if not enabled:
        return CheckResult(
            name="task_paths",
            status=CheckStatus.PASS,
            detail="skipped (--no-check-paths)",
        )

    missing_by_task: list[tuple[str, list[Path]]] = []
    total_paths = 0
    total_missing = 0
    n_tasks = 0
    for path in list_pending_tasks(queue_dir):
        try:
            task = load_task(path)
        except (QueueIOError, QueueSchemaError):
            # check_task_yamls reports this; don't double-count here.
            continue
        n_tasks += 1
        referenced = _extract_paths(task.prompt or "")
        if not referenced:
            continue
        total_paths += len(referenced)
        missing = sorted(p for p in referenced if not p.exists())
        if missing:
            total_missing += len(missing)
            missing_by_task.append((path.name, missing))

    if not missing_by_task:
        return CheckResult(
            name="task_paths",
            status=CheckStatus.PASS,
            detail=f"{total_paths} referenced paths across {n_tasks} tasks all exist",
        )

    sample = []
    for name, paths in missing_by_task[:20]:
        sample.append(f"{name}:")
        for p in paths[:5]:
            sample.append(f"  - {p}")
        if len(paths) > 5:
            sample.append(f"  ... +{len(paths) - 5} more")
    if len(missing_by_task) > 20:
        sample.append(f"... +{len(missing_by_task) - 20} more tasks with missing paths")
    return CheckResult(
        name="task_paths",
        status=CheckStatus.WARN,
        detail=(
            f"{total_missing} missing of {total_paths} referenced paths "
            f"across {len(missing_by_task)} of {n_tasks} tasks"
        ),
        remediation="\n".join(sample),
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


def check_api_usage_source(settings: Settings) -> CheckResult:
    """Probe the API usage source against each configured account.

    Off by default (the doctor CLI exposes ``--check-api-usage`` to
    opt in) because it sends one real ``/v1/messages`` call per account
    — a few tokens each, but not free. When enabled, surfaces:

    * Missing / unparseable ``<config_dir>/.credentials.json``.
    * 401/403 from the API (token expired; operator should run
      ``claude /login`` against that config_dir to refresh).
    * Missing rate-limit headers (Anthropic side may have changed
      them; the runner will fall back to TTY in ``api_then_tty`` mode
      but ``api``-only configurations would stop working).

    Reports the resolved 5h / weekly utilization per account so the
    operator can compare against ``claude /usage`` and confirm the
    API path is trustworthy before flipping ``[usage].source`` to
    ``"api_then_tty"``.
    """
    # Local imports to keep the doctor module's top-level import graph
    # narrow — the API source pulls in urllib, json, etc. but isn't
    # used by most check functions.
    from claude_task_runner.clock import RealClock
    from claude_task_runner.usage.api_source import ApiUsageSource
    from claude_task_runner.usage.drift import (
        UsageApiAuthExpired,
        UsageApiHeaderMissing,
        UsageApiNetworkError,
    )

    if not settings.accounts:
        return CheckResult(
            name="api_usage_source",
            status=CheckStatus.PASS,
            detail="no accounts configured (skipped)",
        )

    rows: list[str] = []
    failures: list[str] = []
    for acct in settings.accounts:
        src = ApiUsageSource(
            RealClock(),
            config_dir=acct.config_dir,
            probe_model=settings.usage.api_probe_model,
            timeout_s=settings.usage.api_timeout_s,
        )
        try:
            reading = src.read()
        except UsageApiAuthExpired as exc:
            failures.append(
                f"{acct.name}: auth expired ({exc}). "
                f"Fix: CLAUDE_CONFIG_DIR={acct.config_dir or '~/.claude'} claude /login"
            )
            continue
        except UsageApiHeaderMissing as exc:
            failures.append(
                f"{acct.name}: headers missing ({exc}). "
                'API source unusable; stay on `[usage].source = "tty"`.'
            )
            continue
        except UsageApiNetworkError as exc:
            failures.append(f"{acct.name}: network ({exc})")
            continue
        rows.append(
            f"{acct.name}: 5h={reading.five_hour.utilization_pct}% "
            f"weekly={reading.seven_day.utilization_pct}% "
            f"(5h resets {reading.five_hour.resets_at}, "
            f"weekly resets {reading.seven_day.resets_at})"
        )

    if failures:
        return CheckResult(
            name="api_usage_source",
            status=CheckStatus.FAIL,
            detail=f"{len(failures)} of {len(settings.accounts)} accounts failed API probe",
            remediation="\n".join(failures + rows),
        )
    return CheckResult(
        name="api_usage_source",
        status=CheckStatus.PASS,
        detail="API readings: " + "; ".join(rows),
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


def all_checks(
    settings: Settings,
    queue_dir: Path,
    *,
    check_paths: bool = True,
    check_api_usage: bool = False,
) -> Iterable[Callable[[], CheckResult]]:
    """Return zero-arg callables, in the order to run them.

    ``check_paths`` toggles :func:`check_task_paths`. Defaults to True;
    the doctor CLI exposes ``--no-check-paths`` to skip it on large
    queues where the existence sweep is unwanted.

    ``check_api_usage`` toggles :func:`check_api_usage_source`. Defaults
    to False because the probe sends a real (cheap) ``/v1/messages``
    call per account; the doctor CLI exposes ``--check-api-usage`` to
    opt in. Useful before flipping ``[usage].source`` to
    ``"api_then_tty"``.
    """
    checks: list[Callable[[], CheckResult]] = [
        lambda: check_claude_binary(settings),
        lambda: check_accounts(settings),
        lambda: check_legacy_claude_config_dir(settings),
        lambda: check_account_policies(settings),
        lambda: check_account_sudo(settings),
        lambda: check_queue_perms_for_linux_users(settings, queue_dir),
        lambda: check_global_lock(settings),
        lambda: check_queue_layout(settings, queue_dir),
        lambda: check_task_yamls(settings, queue_dir),
        lambda: check_task_paths(settings, queue_dir, enabled=check_paths),
        lambda: check_state_yamls(settings, queue_dir),
        lambda: check_supervisor_state(settings, queue_dir),
        lambda: check_ema(settings, queue_dir),
        lambda: check_skills_installed(settings),
        lambda: check_watchdog_installed(settings),
    ]
    if check_api_usage:
        checks.append(lambda: check_api_usage_source(settings))
    return checks
