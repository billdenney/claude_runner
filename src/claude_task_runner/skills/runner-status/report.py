"""The runner-status report: a plain-language summary, then the details.

``snapshot.sh`` gathers the facts, runs ``python3 report.py``, and prints
the open sidecars itself. Every fact about what the runner would do comes
from the runner: ``account list --json`` (each account's state, caps,
pause, settings-file error, weekly resume time and usage error, and the
queue's concurrency ceiling), ``queue holds --json`` (the supervisor's own
candidate selection, read-only: what can start and what holds the rest,
and the poll interval) and ``sidecar list --json`` (the open questions).
This file words those facts; it does not re-derive them.

It runs under the ``python3`` on ``PATH``, so it imports only the standard
library, and needs Python 3.11 or later, as the runner does.

Environment, from ``snapshot.sh`` (paths are files it wrote; a missing
variable reads as empty):

``RS_QUEUE``          the queue directory
``RS_NOW``            the report's time, ISO 8601 UTC
``RS_PID_STATE``      ``alive``, ``stale`` (the PID file names no live
                      process) or ``missing`` (no PID file)
``RS_PID``            the PID file's PID; ``RS_PSLINE`` its ``ps`` line
``RS_PID_ETIME``      ``ps -o etime=`` of the live supervisor
``RS_<CMD>_RC``       exit code of a CLI call, ``missing`` when the CLI is
                      not on PATH, for ``<CMD>`` ``ACCOUNTS``, ``HOLDS``
                      and ``SIDECARS``; ``RS_<CMD>_OUT`` / ``_ERR`` its output
``RS_JOURNAL_STATE``  ``read`` (the journal has entries for the live
                      supervisor's PID), ``empty`` or ``unavailable``
``RS_JOURNAL_LINE``   the newest line about eligible tasks among them
``RS_CONFIG_UNKNOWN`` the supervisor's relative ``--config``, set when its
                      working directory could not be read, so the CLI calls
                      went without one
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
import traceback
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

STALE_AFTER_POLLS = 3
"""supervisor.json older than this many poll intervals means a stuck
supervisor. It is rewritten every tick, and a tick can run past its interval
while it captures usage, so one or two intervals are normal."""

START_TOLERANCE = timedelta(seconds=2)
"""Slack when comparing a log line's time with the supervisor's start, which
comes from ``ps`` etime and the report time, both in whole seconds."""

LOG_TAIL_BYTES = 64 * 1024 * 1024
"""How far back from its end supervisor.log is searched for the newest idle
line, so a large log costs at most this much reading."""

STATUSES = (
    "pending",
    "running",
    "awaiting_sidecar",
    "deferred",
    "possibly_hung",
    "completed",
    "failed",
    "failed_circuit_breaker",
    "weekly_paused",
)
"""``TaskStatus`` in queue/schema.py, in its order. This file cannot import
the package, so tests/unit/test_snapshot_script.py seeds a state file for
every TaskStatus and fails until each has its row."""

_STATUS_RE = re.compile(r"^status:\s*(\S+)\s*$", re.M)

CONFIG_ERROR_LEAD = "configuration error: "
"""How the CLI's ``"error"`` starts when the queue settings file does not
load."""

# Account states in which an account takes tasks (account_dispatch's
# _DISPATCHABLE_STATES).
_TAKING_STATES = frozenset({"dispatching", "slowing_down", "idle"})

# The supervisor's log line about eligible tasks, as the text renderer
# prints it: an ISO timestamp, the level and the message, then the logger.
_IDLE_TEXT_RE = re.compile(
    r"(?P<ts>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)\s+\[\s*info\s*\]\s+"
    r"(?P<msg>(?:no eligible tasks: .*?|eligible tasks available again: \d+))"
    r"(?:\s+\[[\w.]+\])?\s*$"
)
_IDLE_MSG_RE = re.compile(r"^(?:no eligible tasks: .*|eligible tasks available again: \d+)$")
_IDLE_MARKERS = (b"no eligible tasks:", b"eligible tasks available again:")

# Plain words for the categories ``queue holds`` reports, as (singular,
# plural) phrases after a count. A status the selector never dispatches from
# is its own category; a readiness hold is readiness_ plus its requirement
# categories joined by "+".
_BLOCK_LIST = "held by the block list (papers or files still needed, or deliberate holds)"
_HELD_WORDS: dict[str, tuple[str, str]] = {
    "in_flight": ("running", "running"),
    "block_dispatch": (_BLOCK_LIST, _BLOCK_LIST),
    "unanswered_sidecar": ("waits for your answer", "wait for your answer"),
    "depends_on": ("waits for another task to finish", "wait for another task to finish"),
    "deferred_cooldown": ("waits for a retry time", "wait for a retry time"),
    "invalid_effort": (
        "is set to a model or effort the queue does not allow",
        "are set to a model or effort the queue does not allow",
    ),
    "unparseable_task": (
        "has a task file that cannot be read",
        "have task files that cannot be read",
    ),
    "unparseable_state": (
        "has a state file that cannot be read",
        "have state files that cannot be read",
    ),
    "running": (
        "is marked running but not tracked by the supervisor",
        "are marked running but not tracked by the supervisor",
    ),
    "possibly_hung": ("looks stuck", "look stuck"),
    "live_worker": (
        "still has a worker running from an earlier attempt",
        "still have a worker running from an earlier attempt",
    ),
    "failed_circuit_breaker": (
        "stopped after repeated failures",
        "stopped after repeated failures",
    ),
    "weekly_paused": ("is held by the weekly limit", "are held by the weekly limit"),
}
_READINESS_WORDS = {
    "missing_file": "a file",
    "awaiting_sidecar_response": "your answer",
}
# The order held categories are listed in.
_HELD_ORDER = (
    "unanswered_sidecar",
    "readiness_awaiting_sidecar_response",
    "readiness_awaiting_sidecar_response+missing_file",
    "readiness_missing_file",
    "depends_on",
    "deferred_cooldown",
    "block_dispatch",
    "weekly_paused",
    "invalid_effort",
    "possibly_hung",
    "live_worker",
    "failed_circuit_breaker",
    "running",
    "unparseable_task",
    "unparseable_state",
)


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


@dataclass
class CommandResult:
    """One CLI call: its parsed JSON, or why there is none."""

    data: dict[str, Any] | None
    error: str | None
    rc: str = ""
    config_error: str | None = None
    """Why the queue settings file did not load, when the CLI said so as
    ``{"ok": false, "error": "configuration error: ..."}``: exit 78 when its
    content is wrong, 1 when it could not be read and may come back."""


@dataclass
class IdleLine:
    at: datetime
    message: str
    source: str


@dataclass
class Facts:
    queue: Path
    now: datetime
    pid_state: str
    pid: str
    psline: str
    started_at: datetime | None
    saved_at: datetime | None
    snapshot: dict[str, Any] | None
    snapshot_error: str | None
    config_unknown: str
    """The supervisor's relative ``--config`` when its working directory
    could not be read, so the CLI was run without it; empty otherwise."""
    accounts: CommandResult
    holds: CommandResult
    sidecars: CommandResult
    journal_state: str
    log_exists: bool
    idle: IdleLine | None
    idle_before_start: IdleLine | None

    @property
    def runtime(self) -> Path:
        return self.queue / ".claude_task_runner"

    @property
    def poll_s(self) -> float | None:
        """``[usage].poll_interval_s`` of the settings the CLI loaded."""
        data = self.holds.data
        value = data.get("poll_interval_s") if data else None
        return float(value) if isinstance(value, int | float) and value > 0 else None


def _env(name: str) -> str:
    return os.environ.get(name, "")


def _read(path: str) -> str:
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _parse_time(text: object) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_etime(etime: str) -> timedelta | None:
    """``ps -o etime=``, ``[[dd-]hh:]mm:ss``, as a duration."""
    m = re.fullmatch(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)", etime.strip())
    if m is None:
        return None
    days, hours, minutes, seconds = (int(g) if g else 0 for g in m.groups())
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def _command(command: str, key: str, prefix: str) -> CommandResult:
    """Parse one CLI call from ``<prefix>_RC``, ``_OUT`` and ``_ERR``; ``key``
    is the field a listing must carry. A non-zero exit with a listing still
    counts: ``account list`` exits 1 when an account's file is broken, and
    lists it. ``{"ok": false, "error": ...}`` is an error with nothing
    listed."""
    rc = _env(f"{prefix}_RC")
    if rc == "missing":
        return CommandResult(None, "claude-task-runner is not on PATH", rc)
    stdout = _read(_env(f"{prefix}_OUT"))
    try:
        data = json.loads(stdout)
    except ValueError:
        data = None
    if isinstance(data, dict) and key in data:
        return CommandResult(data, None, rc)
    where = f"`claude-task-runner {command}` exited {rc or '?'}"
    if isinstance(data, dict) and data.get("ok") is False and isinstance(data.get("error"), str):
        error: str = data["error"]
        config_error = (
            error.removeprefix(CONFIG_ERROR_LEAD) if error.startswith(CONFIG_ERROR_LEAD) else None
        )
        return CommandResult(None, f"{where}: {error}", rc, config_error)
    printed = (stdout + "\n" + _read(_env(f"{prefix}_ERR"))).strip().splitlines()
    tail = printed[-1].strip() if printed else "no output"
    return CommandResult(None, f"{where}: {tail}", rc)


def _status_of(path: str) -> str | None:
    """The ``status:`` a state file names, ``None`` when it cannot be read or names none."""
    try:
        with open(path) as fh:
            text = fh.read(2000)
    except (OSError, ValueError):
        return None
    m = _STATUS_RE.search(text)
    return m.group(1) if m else None


def parse_idle_line(line: str, source: str) -> IdleLine | None:
    """A supervisor log line about eligible tasks, in the text or the JSON
    log format, or ``None`` if ``line`` is not one."""
    line = line.strip()
    if line.startswith("{"):
        try:
            record = json.loads(line)
        except ValueError:
            return None
        if not isinstance(record, dict):
            return None
        message, at = str(record.get("event", "")), _parse_time(record.get("timestamp"))
        if at is None or not _IDLE_MSG_RE.match(message):
            return None
        return IdleLine(at, message, source)
    m = _IDLE_TEXT_RE.search(line)
    at = _parse_time(m.group("ts")) if m else None
    return IdleLine(at, m.group("msg"), source) if m and at else None


def newest_idle_line_in(path: Path, max_bytes: int = LOG_TAIL_BYTES) -> IdleLine | None:
    """The newest idle line in the last ``max_bytes`` of ``path``.

    Reads backwards a block at a time and stops at the first match, so a
    large log costs only the distance from its end to that line."""
    block = 1024 * 1024
    try:
        with open(path, "rb") as fh:
            end = fh.seek(0, os.SEEK_END)
            start = max(0, end - max_bytes)
            pos, carry = end, b""
            while pos > start:
                pos -= min(block, pos - start)
                fh.seek(pos)
                lines = (fh.read(min(block, end - pos)) + carry).split(b"\n")
                # Unless this is the first byte searched, the first piece may
                # be the tail of a line that starts in the next block back.
                carry = lines.pop(0) if pos > start else b""
                for raw in reversed(lines):
                    if any(marker in raw for marker in _IDLE_MARKERS):
                        found = parse_idle_line(raw.decode("utf-8", "replace"), path.name)
                        if found is not None:
                            return found
                end = pos
    except OSError:
        return None
    return None


def gather() -> Facts:
    queue = Path(_env("RS_QUEUE"))
    runtime = queue / ".claude_task_runner"
    now = _parse_time(_env("RS_NOW")) or datetime.now(UTC)
    pid_state = _env("RS_PID_STATE") or "missing"
    elapsed = _parse_etime(_env("RS_PID_ETIME")) if pid_state == "alive" else None
    started_at = now - elapsed if elapsed is not None else None

    sup_json = runtime / "supervisor.json"
    snapshot: dict[str, Any] | None = None
    snapshot_error: str | None = None
    saved_at: datetime | None = None
    if sup_json.exists():
        try:
            saved_at = datetime.fromtimestamp(sup_json.stat().st_mtime, UTC)
            with open(sup_json, encoding="utf-8") as fh:
                loaded = json.load(fh)
        except (OSError, ValueError) as exc:
            snapshot_error = str(exc)
        else:
            if isinstance(loaded, dict):
                snapshot = loaded
            else:
                snapshot_error = f"it holds a JSON {type(loaded).__name__}, not an object"

    log_path = runtime / "supervisor.log"
    candidates = [
        line
        for line in (
            parse_idle_line(_env("RS_JOURNAL_LINE"), "the journal"),
            newest_idle_line_in(log_path) if log_path.exists() else None,
        )
        if line is not None
    ]
    newest = max(candidates, key=lambda line: line.at, default=None)
    idle, idle_before_start = newest, None
    if newest is not None and started_at is not None and newest.at < started_at - START_TOLERANCE:
        idle, idle_before_start = None, newest

    return Facts(
        queue=queue,
        now=now,
        pid_state=pid_state,
        pid=_env("RS_PID"),
        psline=_env("RS_PSLINE"),
        started_at=started_at,
        saved_at=saved_at,
        snapshot=snapshot,
        snapshot_error=snapshot_error,
        config_unknown=_env("RS_CONFIG_UNKNOWN"),
        accounts=_command("account list --json", "accounts", "RS_ACCOUNTS"),
        holds=_command("queue holds --json", "held", "RS_HOLDS"),
        sidecars=_command("sidecar list --json", "sidecars", "RS_SIDECARS"),
        journal_state=_env("RS_JOURNAL_STATE") or "unavailable",
        log_exists=log_path.exists(),
        idle=idle,
        idle_before_start=idle_before_start,
    )


# ---------------------------------------------------------------------------
# Wording helpers
# ---------------------------------------------------------------------------


def _when(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _duration(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            n = seconds // size
            return f"{n} {unit}{'s' if n != 1 else ''}"
    return f"{seconds} second{'s' if seconds != 1 else ''}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _them(n: int) -> str:
    return "it" if n == 1 else "them"


def held_words(name: str, n: int) -> str:
    """``n`` tasks held by category ``name``, in plain words."""
    if name.startswith("readiness_"):
        kinds = name.removeprefix("readiness_").split("+")
        what = " and ".join(_READINESS_WORDS.get(k, k) for k in kinds)
        return f"{n} {'waits' if n == 1 else 'wait'} for {what}"
    singular, plural = _HELD_WORDS.get(name, (f"held ({name})", f"held ({name})"))
    return f"{n} {singular if n == 1 else plural}"


# ---------------------------------------------------------------------------
# The plain-language summary
# ---------------------------------------------------------------------------


@dataclass
class Notes:
    """What the summary says beyond its overview lines."""

    attention: list[str]
    """Needs the operator: what it means and what to do."""
    info: list[str]
    """Routine and resolving by itself, but worth knowing."""


def _account_times(facts: Facts, name: str) -> dict[str, Any]:
    accounts = (facts.snapshot or {}).get("accounts") or {}
    entry = accounts.get(name)
    return entry if isinstance(entry, dict) else {}


def _account_line(row: dict[str, Any]) -> tuple[str, bool]:
    """``(what the account is doing, whether it takes tasks)``."""
    in_flight = int(row.get("in_flight_count") or 0)
    cap = int(row.get("dispatch_cap") or 0)
    state = row.get("state")
    still = f"; {in_flight} still running" if in_flight else ""
    if row.get("policy_error"):
        return f"takes no tasks, because its settings file has an error{still}", False
    if row.get("paused"):
        return f"paused{still}", False
    if state == "throttled_5h":
        return (
            f"not taking new tasks, because its 5-hour allowance is used up "
            f"({row.get('last_5h_util_pct')}% used){still}",
            False,
        )
    if state == "throttled_weekly":
        return (
            f"not taking new tasks, because it is using its weekly allowance faster than planned "
            f"({row.get('last_weekly_util_pct')}% used){still}",
            False,
        )
    if state in ("no_reading", None):
        return f"not taking tasks, because it has no recent usage reading{still}", False
    if state == "error_drift":
        return f"not taking tasks, because its usage could not be read{still}", False
    if state not in _TAKING_STATES:
        return f"not taking tasks (state {state}){still}", False
    if cap == 0:
        return f"not taking new tasks, because its last usage reading allows none{still}", False
    if in_flight > cap:
        return (
            f"{in_flight} running, above its current limit of {cap}; it starts no new tasks "
            f"until fewer run",
            True,
        )
    if state == "slowing_down":
        return (
            f"taking fewer tasks to save its 5-hour allowance, {in_flight} of {cap} slots in "
            f"use (normally {row.get('max_concurrency')})",
            True,
        )
    return f"taking tasks, {in_flight} of {cap} slots in use", True


def _account_notes(facts: Facts, row: dict[str, Any], live: bool, notes: Notes) -> None:
    name = row["name"]
    in_flight = int(row.get("in_flight_count") or 0)
    if not in_flight:
        still = ""
    elif live:
        still = (
            f" {_plural(in_flight, 'task')} it started {'is' if in_flight == 1 else 'are'} "
            f"still running."
        )
    else:
        still = (
            f" {_plural(in_flight, 'task')} it started {'was' if in_flight == 1 else 'were'} "
            f"running at the supervisor's last record."
        )
    error = row.get("policy_error")
    if error:
        path = row.get("policy_file")
        where = "" if not path or path in error else f"{path}: "
        poll_s = facts.poll_s
        within = (
            f"within {_duration(timedelta(seconds=poll_s))}"
            if poll_s and poll_s > 60
            else "within a minute"
        )
        notes.attention.append(
            f"{name} takes no tasks because its settings file has an error: {where}{error}. "
            f"Fix the file; {name} takes tasks again {within}."
        )
        return
    if row.get("paused"):
        notes.attention.append(
            f"{name} is paused, so it starts no new tasks.{still} Resume it with "
            f"`claude-task-runner account resume {name} --queue {facts.queue}`."
        )
        return
    if not live:
        # The usage states are the supervisor's last record; the item about
        # the supervisor says what matters while it is not running.
        return
    times = _account_times(facts, name)
    state = row.get("state")
    cap = int(row.get("dispatch_cap") or 0)
    if state == "throttled_5h":
        until = _parse_time(times.get("scheduled_wakeup_at")) or _parse_time(
            times.get("last_5h_reset_at")
        )
        at = f" about {_when(until)}" if until else ""
        notes.info.append(
            f"{name} starts again{at}, when its 5-hour usage window resets. Nothing to do."
        )
    elif state == "throttled_weekly":
        catch_up = _parse_time(row.get("weekly_catch_up_at"))
        recheck = _parse_time(times.get("scheduled_wakeup_at"))
        if catch_up is not None and catch_up > facts.now:
            notes.info.append(
                f"{name} starts again about {_when(catch_up)}, when its weekly use falls back "
                f"under plan. Nothing to do."
            )
        elif catch_up is not None:
            # The usage at the last reading is back under plan by now, but
            # the account stays throttled until the supervisor reads again.
            about = f" (about {_when(recheck)})" if recheck and recheck > facts.now else ""
            notes.info.append(
                f"{name}'s weekly use should be back under plan by now, so it should start again "
                f"at its next check{about}. Nothing to do."
            )
        else:
            then = f"; the supervisor re-checks at {_when(recheck)}" if recheck else ""
            notes.info.append(
                f"{name} starts again once its weekly use falls back under plan{then}. "
                f"Nothing to do."
            )
    elif state in ("no_reading", None):
        last = _parse_time(row.get("last_reading_at"))
        seen = f"its last one was at {_when(last)}" if last else "it has never had one"
        notes.attention.append(
            f"{name} has no recent usage reading ({seen}), so it takes no tasks. This clears "
            f"by itself within a few minutes of a supervisor start; if it lasts, see the "
            f'runbook section "An account stays in `no_reading`".'
        )
    elif state == "error_drift":
        usage_error = row.get("usage_error") or {}
        message = usage_error.get("message") or times.get("last_drift_message") or ""
        if usage_error.get("kind") == "auth":
            config_dir = row.get("config_dir") or "~/.claude"
            notes.attention.append(
                f"{name} takes no tasks because its login was rejected when it read its usage "
                f"({message}). Log it in again: `CLAUDE_CONFIG_DIR={config_dir} claude /login`, "
                f"or renew its long-lived token with `claude setup-token`."
            )
        else:
            notes.attention.append(
                f"{name} takes no tasks because its usage page could not be read ({message}); "
                f"the page's format may have changed. Run /runner-usage to check the usage "
                f"reader."
            )
    elif state in _TAKING_STATES and cap == 0:
        notes.info.append(
            f"{name}'s last usage reading allows it no tasks; it takes tasks again at a later "
            f"reading. Nothing to do."
        )
    elif state in _TAKING_STATES and in_flight > cap:
        notes.info.append(
            f"{name} has {in_flight} running, above its current limit of {cap}, so it starts "
            f"no new tasks until fewer run. Nothing to do."
        )


def _supervisor_line(facts: Facts, notes: Notes) -> tuple[str, bool]:
    """``(the supervisor line, whether it is live)``."""
    if facts.pid_state != "alive":
        notes.attention.append(
            "The supervisor is not running, so no new tasks start and finished tasks are not "
            f"recorded. Start it with `claude-task-runner supervisor start --queue "
            f"{facts.queue}`; a watchdog, if installed, restarts it within a few minutes."
        )
        return "not running.", False
    if facts.saved_at is None:
        notes.attention.append(
            "The supervisor has not saved its state yet; it normally does within a minute of "
            "starting. If this lasts, look at its log."
        )
        return "its process is running, but it has not saved its state yet.", False
    age = facts.now - facts.saved_at
    poll_s = facts.poll_s
    if poll_s is None:
        # Without the poll interval a stuck supervisor cannot be told from a
        # slow one, so say how old its state is and judge nothing.
        return f"running (it last saved its state {_duration(age)} ago).", True
    if age.total_seconds() > STALE_AFTER_POLLS * poll_s:
        notes.attention.append(
            f"The supervisor has not saved its state for {_duration(age)} (it normally does "
            f"every {poll_s:g} seconds), so it may be stuck. Look at its log; if it stays this "
            f"way, restart it."
        )
        return f"its process is running, but it last saved its state {_duration(age)} ago.", False
    return "running.", True


def _capacity(
    facts: Facts, rows: list[dict[str, Any]], taking: list[str], live: bool, notes: Notes
) -> tuple[str | None, int | None]:
    """``(the "How many can run" line, free slots now)``; ``None`` for each
    that is unknown."""
    ceiling = (facts.accounts.data or {}).get("queue_ceiling")
    if not isinstance(ceiling, dict):
        return None, None
    caps = ceiling.get("account_caps") or {}
    breakdown = " + ".join(f"{name} {cap}" for name, cap in caps.items()) or "no account"
    value = int(ceiling.get("ceiling") or 0)
    configured = ceiling.get("max_concurrency")
    if ceiling.get("binds"):
        text = (
            f"up to {value} at once (the queue-wide cap; the accounts' own limits add up to "
            f"{ceiling.get('accounts_total')}: {breakdown})."
        )
        notes.attention.append(
            f"The queue-wide cap of {configured} ([concurrency].max_concurrency in "
            f"claude_runner.toml) is lower than what the accounts allow together "
            f"({ceiling.get('accounts_total')}), so at most {value} tasks run at once. Raise or "
            f"remove it to use every account slot."
        )
    elif configured is None:
        text = f"up to {value} at once ({breakdown}; no queue-wide cap)."
    else:
        text = f"up to {value} at once ({breakdown}; the queue-wide cap of {configured} is higher)."
    broken = ceiling.get("broken_accounts") or []
    if broken:
        text += f" Not counted: {', '.join(broken)} (settings file error)."
    holds = facts.holds.data or {}
    queue_cap = holds.get("queue_cap")
    if holds.get("warming_up") and isinstance(queue_cap, int) and queue_cap < value:
        text += f" Until the first task finishes, at most {queue_cap} run at once (warm-up)."
        value = queue_cap
    if not live:
        return text, None
    running = sum(int(row.get("in_flight_count") or 0) for row in rows)
    room = sum(
        max(0, int(row.get("dispatch_cap") or 0) - int(row.get("in_flight_count") or 0))
        for row in rows
        if row["name"] in taking
    )
    room = max(0, min(room, value - running))
    now = f"room for {room} more" if room else "no room for more"
    return text + f" Right now {running} running, with {now}.", room


WORKER_START_LEAD = "cannot start the worker: "
"""How the runner's ``deferred_reason`` starts when a task's worker failed
its checks before it was spawned (a config_dir, sudo, working_dir or log
file problem); the supervisor tries again after a cooldown."""

LISTED = 3
"""How many tasks an item names before it says how many more there are."""


def _listing(items: list[str]) -> str:
    shown = "; ".join(items[:LISTED])
    more = len(items) - LISTED
    return shown + (f"; and {more} more" if more > 0 else "")


def _parked_notes(
    watching: list[dict[str, Any]], exited: list[dict[str, Any]], notes: Notes
) -> None:
    """Tasks parked ``possibly_hung`` while a worker ran that the supervisor
    does not watch (stop reason ``worker_still_running``)."""
    within = [w for w in watching if not w.get("past_cap")]
    past = [w for w in watching if w.get("past_cap")]
    kill = "`kill -TERM -<pid>` (the minus signals its process group)"
    if within:
        n = len(within)
        which = _listing([f"{w.get('task_id')}, pid {w.get('pid')}" for w in within])
        notes.info.append(
            f"{_plural(n, 'task')} {'has' if n == 1 else 'have'} a worker still running that "
            f"the supervisor does not watch, left from before a restart or still alive after "
            f"it was stopped ({which}). No second worker starts for "
            f"{'it' if n == 1 else 'them'} meanwhile. Nothing to do; to end one sooner, stop "
            f"it with {kill}."
        )
    if past:
        n = len(past)
        which = _listing([f"{w.get('task_id')}, pid {w.get('pid')}" for w in past])
        notes.attention.append(
            f"{_plural(n, 'task')} {'has' if n == 1 else 'have'} a worker the supervisor does "
            f"not watch that has run past the task's time limit ({which}). Stop "
            f"{'it' if n == 1 else 'each'} with {kill}."
        )
    if exited:
        n = len(exited)
        which = _listing([f"{w.get('task_id')}, pid {w.get('pid')}" for w in exited])
        notes.attention.append(
            f"{_plural(n, 'task')} {'was' if n == 1 else 'were'} held while a worker the "
            f"supervisor does not watch ran, and the worker has exited ({which}), so "
            f"{'it waits' if n == 1 else 'they wait'} only for the run to be recorded. If this "
            f"lasts past the supervisor's next check, record the run as the runbook describes "
            f"for a worker that ran on after a restart."
        )


def _retry_notes(facts: Facts, holds: dict[str, Any], notes: Notes) -> None:
    """Tasks whose worker could not be started, and when each is tried again."""
    failed = [
        r
        for r in holds.get("retrying") or []
        if isinstance(r, dict) and str(r.get("deferred_reason") or "").startswith(WORKER_START_LEAD)
    ]
    if not failed:
        return
    items = []
    for r in failed:
        why = str(r["deferred_reason"]).removeprefix(WORKER_START_LEAD).rstrip(".")
        due = _parse_time(r.get("next_eligible_at"))
        when = (
            f"retries about {_when(due)}"
            if due and due > facts.now
            else "retries at the next check"
        )
        items.append(f"{r.get('task_id')}: {why}; {when}")
    n = len(failed)
    notes.attention.append(
        f"{_plural(n, 'task')} could not start {'its' if n == 1 else 'their'} worker "
        f"({_listing(items)}). Fix what each names; the supervisor tries again at the time "
        f"given, or `claude-task-runner queue force-dispatch <id> --queue {facts.queue}` starts "
        f"one at once after the fix."
    )


def _tasks_line(facts: Facts, notes: Notes, live: bool, room: int | None) -> str:
    holds = facts.holds.data
    if holds is None:
        if facts.holds.config_error is not None:
            # plain_summary words the settings file's error once.
            return f"unknown ({_SETTINGS_UNREADABLE})."
        notes.attention.append(
            f"What holds the tasks could not be read ({facts.holds.error}). Run "
            f"`claude-task-runner queue holds --queue {facts.queue}` to see the error."
        )
        return "unknown."
    held = {k: int(v) for k, v in (holds.get("held") or {}).items() if int(v)}
    eligible = holds.get("eligible") or {}
    n_eligible = int(eligible.get("count") or 0)
    can_start = int(eligible.get("can_start_now", n_eligible) or 0)
    affined = int(eligible.get("waiting_for_their_account") or 0)
    answered = int((eligible.get("by_status") or {}).get("awaiting_sidecar") or 0)
    running = held.pop("in_flight", 0)
    parts: list[str] = []
    if running and live:
        parts.append(f"{running} running")
    elif running:
        parts.append(
            f"{running} {'was' if running == 1 else 'were'} running at the supervisor's last record"
        )
    if n_eligible:
        session = f"only on the account {'its' if n_eligible == 1 else 'their'} session runs on"
        who = (
            f"{n_eligible} answered {'task' if n_eligible == 1 else 'tasks'}"
            if answered == n_eligible
            else str(n_eligible)
        )
        where = session if affined == n_eligible else "as soon as a slot is free"
        of_them = []
        if answered and answered < n_eligible:
            of_them.append(f"{answered} of them answered after asking you")
        if affined and affined < n_eligible:
            of_them.append(
                f"{affined} of them only on the account {'its' if affined == 1 else 'their'} "
                f"session runs on"
            )
        qualified = f" ({'; '.join(of_them)})" if of_them else ""
        parts.append(f"{who} can start {where}{qualified}")
    # Parked workers are counted under possibly_hung, but are not stuck.
    parked = [w for w in holds.get("parked_workers") or [] if isinstance(w, dict)]
    watching = [w for w in parked if w.get("running")]
    exited = [w for w in parked if not w.get("running")]
    held["possibly_hung"] = held.get("possibly_hung", 0) - len(parked)
    if held["possibly_hung"] <= 0:
        del held["possibly_hung"]
    order = {name: i for i, name in enumerate(_HELD_ORDER)}
    for name in sorted(held, key=lambda k: (order.get(k, len(order)), k)):
        parts.append(held_words(name, held[name]))
    if watching:
        n = len(watching)
        parts.append(
            f"{n} {'has' if n == 1 else 'have'} a worker running that the supervisor does not watch"
        )
    if exited:
        n = len(exited)
        parts.append(
            f"{n} {'waits' if n == 1 else 'wait'} for an exited worker's run to be recorded"
        )
    parts.append(f"{int(holds.get('completed') or 0)} finished")
    _parked_notes(watching, exited, notes)
    _retry_notes(facts, holds, notes)

    queue = facts.queue
    doctor = f"`claude-task-runner doctor --queue {queue}` lists the files."
    for name, what in (
        (
            "invalid_effort",
            "Tasks set to a model or effort the queue does not allow: {n}. Fix each task's model "
            "or effort, or add the pair to [effort_levels] and send the supervisor SIGHUP.",
        ),
        (
            "possibly_hung",
            "Tasks that have gone quiet while running and may be stuck: {n}. See the runbook "
            'section "Task `possibly_hung` for hours".',
        ),
        (
            "failed_circuit_breaker",
            "Tasks that stopped after failing repeatedly and will not be retried: {n}. Look with "
            f"`claude-task-runner queue states --status failed_circuit_breaker --queue {queue}`.",
        ),
        ("unparseable_task", "Task files in todo/ that cannot be read: {n}. " + doctor),
        ("unparseable_state", "Tasks held because their state file cannot be read: {n}. " + doctor),
    ):
        if held.get(name):
            notes.attention.append(what.replace("{n}", str(held[name])))

    if live and affined:
        ttl = holds.get("affinity_ttl_s")
        after = (
            f"after {_plural(round(ttl / 60), 'minute')} without running"
            if isinstance(ttl, int | float) and ttl > 0
            else "once its session has been idle long enough"
        )
        notes.info.append(
            f"{_plural(affined, 'task')} can start only on the account "
            f"{'its' if affined == 1 else 'their'} session runs on, which cannot take "
            f"{_them(affined)} now; {'it moves' if affined == 1 else 'they move'} to any account "
            f"{after}. Nothing to do."
        )
    if live and running == 0:
        if can_start and room:
            notes.attention.append(
                f"Nothing is running although {can_start} "
                f"{'task' if can_start == 1 else 'tasks'} can start and there is room for "
                f"{room}. If this lasts beyond the supervisor's next check, look in its log for "
                f"`dispatch skipped task` lines."
            )
        elif not n_eligible and held:
            notes.info.append(
                "No task can start right now: every unfinished task is held, as Tasks shows."
            )
    return "; ".join(parts) + "."


def _questions_line(facts: Facts, notes: Notes) -> str:
    sidecars = facts.sidecars.data
    if sidecars is None:
        notes.attention.append(
            f"The open questions could not be listed ({facts.sidecars.error}), so whether any "
            f"are waiting for you is unknown."
        )
        return "unknown."
    rows = sidecars.get("sidecars") or []
    unreadable = [s for s in rows if s.get("error")]
    readable = [s for s in rows if not s.get("error")]
    asking = [s for s in readable if s.get("outstanding")]
    notices = [s for s in readable if not s.get("outstanding")]
    n_questions = sum(len(s["outstanding"]) for s in asking)
    if unreadable:
        n = len(unreadable)
        which = ", ".join(f"{s.get('task_id')} request {s.get('sequence')}" for s in unreadable)
        notes.attention.append(
            f"{_plural(n, 'sidecar request')} cannot be read ({which}), so what "
            f"{'it asks' if n == 1 else 'they ask'} is unknown and the "
            f"{'task waits' if n == 1 else 'tasks wait'} for nothing anyone can answer. Repair "
            f"{_them(n)}; `claude-task-runner sidecar list --queue {facts.queue}` shows the "
            f"error."
        )
    text = (
        f"{n_questions}, in {_plural(len(asking), 'request')}; answer with /runner-answer-sidecar."
        if n_questions
        else "none."
    )
    if notices:
        text += (
            f" Also {_plural(len(notices), 'notice')} to acknowledge, asking nothing "
            f"(/runner-answer-sidecar)."
        )
    return text


_SETTINGS_UNREADABLE = "the queue settings file cannot be read"


def _settings_notes(facts: Facts, notes: Notes) -> None:
    """What the summary says about the settings the CLI read the facts
    with: the queue settings file's load error, said once whichever call
    met it, and a supervisor ``--config`` that could not be followed."""
    if facts.config_unknown:
        notes.attention.append(
            f"The supervisor was started with the relative settings path "
            f"`--config {facts.config_unknown}`, and its working directory cannot be read, so "
            f"which settings file it uses is unknown; the facts below come from the queue's own "
            f"settings file instead. Run /runner-status as the user the supervisor runs as, or "
            f"start the supervisor with an absolute --config path."
        )
    failed = next(
        (r for r in (facts.accounts, facts.holds) if r.data is None and r.config_error), None
    )
    if failed is None or failed.config_error is None:
        return
    detail = "; ".join(line.strip() for line in failed.config_error.splitlines() if line.strip())
    detail = detail.rstrip(".")
    if failed.rc == "78":
        fix = (
            "Its content is wrong and needs fixing: a supervisor started with it refuses to "
            "start, and a running one keeps the settings it loaded before. Fix the file, then "
            "send the supervisor SIGHUP to load it."
        )
    elif failed.rc == "1":
        fix = (
            "It could not be read just now, which may clear up by itself (a mount or permission "
            "that comes back). If it lasts, check that the file is there and readable."
        )
    else:
        fix = "Check the file."
    unknown = " and ".join(
        what
        for result, what in (
            (facts.accounts, "what each account is doing"),
            (facts.holds, "what holds the tasks"),
        )
        if result.data is None and result.config_error
    )
    notes.attention.append(
        f"The queue settings file cannot be read: {detail}. {fix} Until it can, {unknown} "
        f"{'is' if ' and ' not in unknown else 'are'} unknown."
    )


def _record_note(facts: Facts, notes: Notes) -> None:
    """An unreadable supervisor.json: the in-flight tasks and the accounts'
    usage states come from it."""
    if facts.snapshot_error is None:
        return
    notes.attention.append(
        f"The supervisor's record ({facts.runtime / 'supervisor.json'}) cannot be read "
        f"({facts.snapshot_error}), so which tasks are running and what each account's last "
        f"usage reading said are unknown. A running supervisor rewrites it at every check; if "
        f"this lasts, look at the file and the supervisor's log."
    )


def plain_summary(facts: Facts) -> str:
    notes = Notes(attention=[], info=[])
    supervisor, live = _supervisor_line(facts, notes)
    lines = [f"- **Supervisor:** {supervisor}"]
    _record_note(facts, notes)
    _settings_notes(facts, notes)
    data = facts.accounts.data
    rows: list[dict[str, Any]] = list(data.get("accounts") or []) if data else []
    taking: list[str] = []
    if data is None and facts.accounts.config_error is not None:
        lines.append(f"- **Accounts:** unknown ({_SETTINGS_UNREADABLE}).")
    elif data is None:
        lines.append("- **Accounts:** unknown.")
        notes.attention.append(
            f"The account list could not be read ({facts.accounts.error}), so what each account "
            f"is doing and how many tasks may run at once are unknown. Run `claude-task-runner "
            f"account list --queue {facts.queue}` to see the error."
        )
    elif not rows:
        lines.append("- **Accounts:** none configured, so no task can start.")
    else:
        stale = not live and facts.snapshot is not None
        heading = "Accounts (as of the supervisor's last record)" if stale else "Accounts"
        lines.append(f"- **{heading}:**")
        for row in rows:
            text, takes = _account_line(row)
            lines.append(f"  - {row['name']}: {text}.")
            if takes:
                taking.append(row["name"])
            _account_notes(facts, row, live, notes)
    capacity, room = _capacity(facts, rows, taking, live, notes)
    if capacity:
        lines.append(f"- **How many can run:** {capacity}")
    lines.append(f"- **Tasks:** {_tasks_line(facts, notes, live, room)}")
    lines.append(f"- **Questions for you:** {_questions_line(facts, notes)}")

    out = ["### In plain words", "", *lines, ""]
    if notes.attention:
        out += ["**Not normal right now:**", "", *(f"- {item}" for item in notes.attention), ""]
    if notes.info:
        out += ["**For your information:**", "", *(f"- {item}" for item in notes.info), ""]
    if not notes.attention and not notes.info:
        out += ["Everything looks normal.", ""]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Detail sections
# ---------------------------------------------------------------------------


def supervisor_section(facts: Facts) -> str:
    pidfile = facts.runtime / "supervisor.pid"
    if facts.pid_state == "alive":
        out = [f"**Supervisor**: alive PID {facts.pid}", "```", facts.psline, "```"]
    elif facts.pid_state == "stale":
        out = [f"**Supervisor**: NOT RUNNING (stale pidfile {pidfile} → {facts.pid})"]
    else:
        out = [f"**Supervisor**: NOT RUNNING (no pidfile at {pidfile})"]
    out += [
        "",
        "_The supervisor is the background process that starts tasks, watches each account's "
        "usage and records progress. Nothing new starts while it is not running._",
    ]
    return "\n".join(out) + "\n"


def supervisor_json_section(facts: Facts) -> str:
    path = facts.runtime / "supervisor.json"
    if facts.snapshot is None:
        why = f"unreadable ({facts.snapshot_error})" if facts.snapshot_error else "missing"
        return f"**supervisor.json**: {why} at {path}\n"
    d = facts.snapshot
    saved = facts.saved_at.strftime("%Y-%m-%dT%H:%M:%SZ") if facts.saved_at else "?"
    fields = [
        ("state", d.get("state")),
        ("5h util", f"{d.get('last_5h_util_pct', '?')}%"),
        ("weekly util", f"{d.get('last_weekly_util_pct', '?')}%"),
        ("in_flight", len(d.get("in_flight") or [])),
        ("since", d.get("since", "?")),
        ("scheduled_wakeup", d.get("scheduled_wakeup_at", "-")),
        ("5h reset_at", d.get("last_5h_reset_at", "?")),
        ("weekly reset_at", d.get("last_weekly_reset_at", "?")),
        ("drift", d.get("last_drift_message", "") or "-"),
        ("last saved", saved),
    ]
    out = [
        "**supervisor.json**",
        "",
        "_The supervisor's own record, rewritten every time it checks the queue (each poll "
        "interval), so `last saved` shows whether it is still working. `state` is its overall "
        "mode; `5h util` and `weekly util` are how much of the 5-hour and weekly usage "
        "allowance is used; `in_flight` counts tasks running now; `scheduled_wakeup` is when "
        "it next re-checks a throttled account, not when that account resumes; times are UTC._",
        "",
        "| field | value |",
        "|---|---|",
    ]
    out += [f"| {k} | `{v}` |" for k, v in fields]
    return "\n".join(out) + "\n"


def accounts_section(facts: Facts) -> str:
    out = [
        "**Accounts and concurrency** (from `claude-task-runner account list --json`)",
        "",
        "_Each account runs at most its own limit (`max_concurrency` in its "
        'runner-account.toml). "Allowed now" is what it may run at the moment: lower while it '
        "slows down to save its 5-hour allowance, 0 while it is throttled, paused, without a "
        'usage reading, or its settings file is broken. "In flight" means running now. '
        '"Resumes about" is when a weekly-throttled account\'s use falls back under plan. The '
        "queue ceiling is the most tasks the whole queue may run at once._",
        "",
    ]
    data = facts.accounts.data
    if data is None:
        out.append(f"could not list: {facts.accounts.error}")
        return "\n".join(out) + "\n"
    out.append(
        "| account | limit | allowed now | in flight | paused | resumes about | settings file "
        "| error |"
    )
    out.append("|---|---:|---:|---:|:-:|---|---|---|")
    for row in data.get("accounts") or []:
        limit = row.get("max_concurrency")
        error = (row.get("policy_error") or "").replace("|", r"\|")
        catch_up = _parse_time(row.get("weekly_catch_up_at"))
        out.append(
            f"| {row.get('name')} | {'—' if limit is None else limit} "
            f"| {row.get('dispatch_cap', 0)} | {row.get('in_flight_count', 0)} "
            f"| {'yes' if row.get('paused') else ''} | {_when(catch_up) if catch_up else '—'} "
            f"| {row.get('policy_file') or '—'} | {error or '—'} |"
        )
    ceiling = data.get("queue_ceiling")
    if isinstance(ceiling, dict):
        caps = " + ".join(f"{n} {c}" for n, c in (ceiling.get("account_caps") or {}).items())
        configured = ceiling.get("max_concurrency")
        initial = ceiling.get("initial_concurrency")
        broken = ", ".join(ceiling.get("broken_accounts") or []) or "none"
        out.append("")
        out.append(
            f"Queue ceiling: {ceiling.get('ceiling')} "
            f"(accounts' own limits: {caps or 'none'} = {ceiling.get('accounts_total')}; "
            f"queue-wide cap [concurrency].max_concurrency: "
            f"{'not set' if configured is None else configured}"
            f"{', which binds' if ceiling.get('binds') else ''}; "
            f"warm-up cap [concurrency].initial_concurrency: "
            f"{'not set' if initial is None else initial}; broken account files: {broken})"
        )
    return "\n".join(out) + "\n"


def per_account_section(facts: Facts) -> str:
    """The usage table from supervisor.json's ``accounts`` map. ``paused`` is
    read from the account_paused/ markers, which supervisor.json trails by a
    tick, or from supervisor.json on a queue that has no such directory yet."""
    if facts.snapshot is None:
        return ""
    d = facts.snapshot
    accounts = d.get("accounts") or {}
    if not accounts:
        return (
            "**Per-account state**\n\n"
            "_no `accounts` map in supervisor.json "
            f"(schema_version={d.get('schema_version')!r}); v2 files are "
            "auto-migrated on next supervisor start. If this is a "
            "v3 snapshot, the supervisor has not completed a tick "
            "yet — the `accounts` map is populated by `initial_snapshot`._\n"
        )
    pause_dir = facts.runtime / "account_paused"
    pause_note = ""
    markers: set[str] | None
    try:
        markers = {
            n
            for n in os.listdir(pause_dir)
            if not n.startswith(".") and os.path.isfile(os.path.join(pause_dir, n))
        }
    except FileNotFoundError:
        markers = None
    except OSError as exc:
        markers = None
        pause_note = f"_paused shows supervisor.json's copy: cannot read {pause_dir}: {exc}_"
    in_flight_by_account = Counter(
        rec.get("account") for rec in d.get("in_flight") or [] if rec.get("account")
    )
    out = [
        "**Per-account state** (from supervisor.json `accounts`)",
        "",
        "_What each account's last usage reading says. `state`: dispatching or idle take tasks, "
        "slowing_down takes fewer, throttled_5h waits for its 5-hour window to reset, "
        "throttled_weekly waits until its weekly use falls back under plan, no_reading and "
        "error_drift take none until a reading arrives. `in-flight` is how many tasks the "
        "account is running now; `target` (the throttle target) is how many the throttle lets "
        'it run, "—" when no reading has set one; `wakeup` is when the supervisor re-checks; '
        "times are UTC._",
        "",
        "| account | state | 5h | weekly | paused | in-flight | target | "
        "5h reset | weekly reset | wakeup | last capture |",
        "|---|---|---:|---:|:-:|---:|---:|---|---|---|---|",
    ]
    for name in sorted(accounts):
        a = accounts[name]
        is_paused = (name in markers) if markers is not None else a.get("paused")
        target = a.get("target_concurrency")
        out.append(
            f"| {name} | `{a.get('state', '?')}` "
            f"| {a.get('last_5h_util_pct', '?')}% "
            f"| {a.get('last_weekly_util_pct', '?')}% "
            f"| {'yes' if is_paused else ''} "
            f"| {in_flight_by_account.get(name, 0)} "
            f"| {'—' if target is None else target} "
            f"| {a.get('last_5h_reset_at', '—')} "
            f"| {a.get('last_weekly_reset_at', '—')} "
            f"| {a.get('scheduled_wakeup_at') or '—'} "
            f"| {a.get('last_capture_at') or '—'} |"
        )
    if pause_note:
        out += ["", pause_note]
    drift_rows = [
        (name, str(accounts[name].get("last_drift_message", "")))
        for name in sorted(accounts)
        if accounts[name].get("last_drift_message")
    ]
    if drift_rows:
        out += ["", "_Per-account drift messages:_", ""]
        for name, msg in drift_rows:
            # Pipe-escaped so a markdown renderer does not cut the bullet short.
            safe = msg.replace("|", "\\|")
            out.append(f"- `{name}`: {safe}")
    return "\n".join(out) + "\n"


def holds_section(facts: Facts) -> str:
    out = [
        "**What holds the tasks** (from `claude-task-runner queue holds --json`)",
        "",
        "_The supervisor's own selection, run once without changing anything: every task in "
        "todo/ either can start as soon as an account has a free slot, is held for one reason, "
        "or is finished. A task that asked a question stays awaiting_sidecar until it runs "
        "again, so an answered one counts here as able to start._",
        "",
    ]
    data = facts.holds.data
    if data is None:
        out.append(f"could not list: {facts.holds.error}")
        return "\n".join(out) + "\n"
    eligible = data.get("eligible") or {}
    statuses = ", ".join(f"{k} {v}" for k, v in sorted((eligible.get("by_status") or {}).items()))
    out += ["| category | tasks |", "|---|---:|"]
    out.append(f"| can start ({statuses or 'none'}) | {eligible.get('count', 0)} |")
    out += [f"| {name} | {n} |" for name, n in sorted((data.get("held") or {}).items())]
    out.append(f"| completed | {data.get('completed', 0)} |")
    out.append(f"| todo/ tasks (total) | {data.get('todo_tasks', 0)} |")
    retrying = [r for r in data.get("retrying") or [] if isinstance(r, dict)]
    if retrying:
        # Grouped by the reason's lead, the part before its first colon,
        # which names who deferred the task.
        counts: Counter[str] = Counter()
        firsts: dict[str, datetime] = {}
        for r in retrying:
            lead = str(r.get("deferred_reason") or "no reason").split(":", 1)[0]
            counts[lead] += 1
            due = _parse_time(r.get("next_eligible_at"))
            if due is not None and (lead not in firsts or due < firsts[lead]):
                firsts[lead] = due
        out += ["", "_Why the tasks waiting for a retry time were deferred:_", ""]
        for lead, n in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
            first = f", the first retried about {_when(firsts[lead])}" if lead in firsts else ""
            safe = lead.replace("|", "\\|")
            out.append(f"- {safe}: {n}{first}")
    parked = [w for w in data.get("parked_workers") or [] if isinstance(w, dict)]
    if parked:
        out += [
            "",
            "_Counted under possibly_hung, parked while a worker the supervisor does not watch "
            "ran:_",
            "",
            "| task | pid | started | still running | past its time limit |",
            "|---|---:|---|:-:|:-:|",
        ]
        out += [
            f"| {w.get('task_id')} | {w.get('pid')} | {w.get('started_at') or '—'} "
            f"| {'yes' if w.get('running') else 'no'} | {'yes' if w.get('past_cap') else 'no'} |"
            for w in parked
        ]
    if data.get("supervisor_json_error"):
        out += ["", f"_in-flight tasks unknown: {data['supervisor_json_error']}_"]
    return "\n".join(out) + "\n"


def counts_section(facts: Facts) -> str:
    """Status breakdown across every state YAML, todo/ and done/ alike: a row
    for every task status, a row for each status the runner does not know, and
    one for files that could not be read or name no status, so the rows add up
    to the total."""
    state_dir = facts.runtime / "state"
    counts: Counter[str | None] = Counter(
        _status_of(p) for p in glob.glob(os.path.join(state_dir, "*.yaml"))
    )
    out = [
        "**Queue counts**",
        "",
        "_How many tasks the runner has a record of in each state, finished ones too. pending: "
        "not started yet; running: being worked on now; awaiting_sidecar: stopped to ask a "
        "question, and stays so until it runs again, so it may already be answered and waiting "
        "for a slot; deferred: on hold until a file appears, a retry time passes or a setting "
        "is fixed; possibly_hung: running but silent for a long time; completed: done; failed: "
        "failed once and will be retried; failed_circuit_breaker: failed repeatedly and will "
        "not be retried; weekly_paused: held by the weekly limit. todo/*.yaml counts the task "
        "files in todo/._",
        "",
        "| field | value |",
        "|---|---|",
    ]
    out += [f"| state.{k} | {counts[k]} |" for k in STATUSES]
    out += [
        f"| state.{k} (unknown status) | {counts[k]} |"
        for k in sorted(k for k in counts if k is not None and k not in STATUSES)
    ]
    out.append(f"| state files unreadable or without a status | {counts[None]} |")
    out.append(f"| state files (total) | {sum(counts.values())} |")
    todo = [p for p in glob.glob(os.path.join(facts.queue, "todo", "*.yaml")) if os.path.isfile(p)]
    out.append(f"| todo/*.yaml | {len(todo)} |")
    return "\n".join(out) + "\n"


def idle_section(facts: Facts) -> str:
    out = [
        "**Last idle report**",
        "",
        "_When the supervisor finds no task it can start, it logs one `no eligible tasks:` line "
        "counting what holds the unfinished tasks, and logs again only when those counts "
        "change; `eligible tasks available again:` ends such a spell. The line describes the "
        "queue at its own time, which may be long ago; What holds the tasks, above, is now._",
        "",
    ]
    if facts.idle is not None:
        line = facts.idle
        meaning = (
            ": tasks were ready to start then"
            if line.message.startswith("eligible tasks available again")
            else ""
        )
        out.append(f"{_when(line.at)} ({line.source}): `{line.message}`{meaning}")
    elif facts.idle_before_start is not None:
        line = facts.idle_before_start
        out.append(
            f"none since the supervisor started; the last one, from before it started, is "
            f"{_when(line.at)} ({line.source}): `{line.message}`"
        )
    elif facts.journal_state == "read" or facts.log_exists:
        out.append("none since the supervisor started")
    elif facts.pid_state != "alive":
        out.append(
            "not available: no supervisor is running to look up in the journal, and there is no "
            "supervisor.log in the queue"
        )
    else:
        out.append(
            "not available: the supervisor's log is not in the journal for its PID and there "
            "is no supervisor.log in the queue (a supervisor started by hand logs to its "
            "terminal)"
        )
    return "\n".join(out) + "\n"


SECTIONS: list[tuple[str, Callable[[Facts], str]]] = [
    ("In plain words", plain_summary),
    ("Supervisor", supervisor_section),
    ("supervisor.json", supervisor_json_section),
    ("Accounts and concurrency", accounts_section),
    ("Per-account state", per_account_section),
    ("What holds the tasks", holds_section),
    ("Queue counts", counts_section),
    ("Last idle report", idle_section),
]


def main() -> int:
    """Print every section; one that fails says so and the rest still print."""
    facts = gather()
    for index, (title, render) in enumerate(SECTIONS):
        if index == 1:
            print("### Details")
            print()
        try:
            text = render(facts)
        except Exception as exc:  # one broken section must not hide the rest
            where = traceback.extract_tb(exc.__traceback__)[-1].lineno
            text = (
                f"**{title}**: unavailable: {type(exc).__name__}: {exc} (report.py line {where})\n"
            )
        if text:
            print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
