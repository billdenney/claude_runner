"""Sections of the runner-status report that need facts from several sources.

``snapshot.sh`` runs the CLI commands and the journal search, then calls
``python3 report.py <section>`` with what it gathered in ``RS_*`` environment
variables:

* ``summary``: the plain-language summary printed first;
* ``accounts``: the accounts and concurrency table (``account list --json``);
* ``counts``: the task counts by state;
* ``idle``: the supervisor's last "no eligible tasks" report.

Standalone on purpose: it runs under whatever ``python3`` is on ``PATH``,
not the runner's environment, so it imports only the standard library, is
written for Python 3.9 and later (``tomllib``, 3.11, is optional), and reads
the runner's facts from the CLI's JSON output and the queue's files.

Environment (paths are files ``snapshot.sh`` wrote; a missing variable reads
as empty):

``RS_QUEUE``          the queue directory
``RS_NOW``            the report's time, ISO 8601 UTC
``RS_PID_STATE``      ``alive``, ``stale`` (PID file names no live process)
                      or ``missing`` (no PID file)
``RS_PID_ETIME``      ``ps -o etime=`` of the live supervisor, ``[[dd-]hh:]mm:ss``
``RS_ACCOUNTS_RC``    exit code of ``account list --json``, ``missing`` when
                      the CLI is not on PATH
``RS_ACCOUNTS_OUT``   its stdout; ``RS_ACCOUNTS_ERR`` its stderr
``RS_SIDECARS_RC``    the same for ``sidecar list --json``
``RS_SIDECARS_OUT``   its stdout; ``RS_SIDECARS_ERR`` its stderr
``RS_JOURNAL_LINE``   the newest idle line the live supervisor wrote to the
                      journal, or empty
"""

from __future__ import annotations

import glob
import json
import math
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

POLL_INTERVAL_DEFAULT_S = 60.0
"""The package default of ``[usage].poll_interval_s``; a test pins it to the
packaged defaults."""

STALE_AFTER_POLLS = 3
"""supervisor.json older than this many poll intervals means a stuck
supervisor. It is rewritten every tick, and a tick can run past its interval
while it captures usage, so one or two intervals are normal."""

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

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+; this runs on older python3 too

_STATUS_RE = re.compile(r"^status:\s*(\S+)\s*$", re.M)

# Account states in which an account takes tasks (account_dispatch's
# _DISPATCHABLE_STATES).
_TAKING_STATES = frozenset({"dispatching", "slowing_down", "idle"})

# A supervisor log line about eligible tasks: the structlog text renderer
# prints an ISO timestamp, the level and the message, then the logger name.
_IDLE_RE = re.compile(
    r"(?P<ts>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)\s+\[\s*info\s*\]\s+"
    r"(?P<msg>(?:no eligible tasks: .*?|eligible tasks available again: \d+))"
    r"(?:\s+\[[\w.]+\])?\s*$"
)

# Plain words for the skip categories the "no eligible tasks" line prints
# (runner/orchestrator.py). A status name the runner never dispatches from is
# its own category; a readiness hold is readiness_ plus its requirement
# categories joined by "+".
_SKIP_WORDS = {
    "block_dispatch": "blocked by the block list",
    "in_flight": "already running",
    "unparseable_task": "with a task file that cannot be read",
    "unparseable_state": "with a state file that cannot be read",
    "unanswered_sidecar": "waiting for your answer",
    "deferred_cooldown": "waiting for a retry time",
    "invalid_effort": "set to a model or effort the queue does not allow",
    "depends_on": "waiting for another task to finish",
    "running": "marked running",
    "possibly_hung": "that look stuck",
    "failed_circuit_breaker": "stopped after repeated failures",
    "weekly_paused": "held by the weekly limit",
}
_READINESS_WORDS = {
    "missing_file": "a file",
    "awaiting_sidecar_response": "an answer",
}


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


@dataclass
class CommandResult:
    """One CLI call: its parsed JSON, or why there is none."""

    data: dict[str, Any] | None
    error: str | None


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
    started_at: datetime | None
    saved_at: datetime | None
    poll_s: float
    snapshot: dict[str, Any] | None
    accounts: CommandResult
    sidecars: CommandResult
    idle: IdleLine | None
    idle_before_start: IdleLine | None

    @property
    def state_dir(self) -> Path:
        return self.queue / ".claude_task_runner" / "state"

    def todo_statuses(self) -> Counter[str | None]:
        """The status of each task in todo/: ``no_state`` when it has no state
        file yet, ``None`` when its state file cannot be read or names none."""
        statuses: Counter[str | None] = Counter()
        for path in glob.glob(os.path.join(self.queue, "todo", "*.yaml")):
            state_path = self.state_dir / os.path.basename(path)
            statuses["no_state" if not state_path.exists() else _status_of(str(state_path))] += 1
        return statuses

    def any_completed(self) -> bool:
        """Whether any task, in todo/ or not, has completed: the warm-up cap
        lifts at the first."""
        return any(
            _status_of(p) == "completed" for p in glob.glob(os.path.join(self.state_dir, "*.yaml"))
        )


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


def _parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_etime(etime: str) -> timedelta | None:
    """``ps -o etime=``, ``[[dd-]hh:]mm:ss``, as a duration."""
    etime = etime.strip()
    m = re.fullmatch(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)", etime)
    if m is None:
        return None
    days, hours, minutes, seconds = (int(g) if g else 0 for g in m.groups())
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def _command(command: str, key: str, prefix: str) -> CommandResult:
    """Parse one CLI call from ``<prefix>_RC``, ``_OUT`` and ``_ERR``; ``key``
    is the field a listing must carry. A non-zero exit with a listing still
    counts: ``account list`` exits 1 when an account's file is broken, and
    lists it."""
    rc = _env(f"{prefix}_RC")
    if rc == "missing":
        return CommandResult(None, "claude-task-runner is not on PATH")
    stdout = _read(_env(f"{prefix}_OUT"))
    try:
        data = json.loads(stdout)
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get(key), list):
        return CommandResult(data, None)
    printed = (stdout + "\n" + _read(_env(f"{prefix}_ERR"))).strip().splitlines()
    tail = printed[-1].strip() if printed else "no output"
    return CommandResult(None, f"`claude-task-runner {command}` exited {rc or '?'}: {tail}")


def _poll_interval(queue: Path) -> float:
    """``[usage].poll_interval_s`` from the queue's claude_runner.toml, else the default."""
    try:
        import tomllib

        with open(queue / "claude_runner.toml", "rb") as fh:
            value = tomllib.load(fh).get("usage", {}).get("poll_interval_s")
    except (ImportError, OSError, ValueError):
        return POLL_INTERVAL_DEFAULT_S
    return (
        float(value) if isinstance(value, (int, float)) and value > 0 else POLL_INTERVAL_DEFAULT_S
    )


def _status_of(path: str) -> str | None:
    """The ``status:`` a state file names, ``None`` when it cannot be read or names none."""
    try:
        with open(path) as fh:
            text = fh.read(2000)
    except (OSError, ValueError):
        return None
    m = _STATUS_RE.search(text)
    return m.group(1) if m else None


def _idle_line_in(text: str, source: str) -> IdleLine | None:
    """The newest line about eligible tasks in ``text``."""
    found: IdleLine | None = None
    for line in text.splitlines():
        m = _IDLE_RE.search(line)
        if m is None:
            continue
        at = _parse_time(m.group("ts"))
        if at is not None:
            found = IdleLine(at, m.group("msg"), source)
    return found


def gather() -> Facts:
    queue = Path(_env("RS_QUEUE"))
    runtime = queue / ".claude_task_runner"
    now = _parse_time(_env("RS_NOW")) or datetime.now(UTC)
    pid_state = _env("RS_PID_STATE") or "missing"
    elapsed = _parse_etime(_env("RS_PID_ETIME")) if pid_state == "alive" else None
    started_at = now - elapsed if elapsed is not None else None

    sup_json = runtime / "supervisor.json"
    snapshot: dict[str, Any] | None = None
    saved_at: datetime | None = None
    try:
        saved_at = datetime.fromtimestamp(sup_json.stat().st_mtime, UTC)
        with open(sup_json, encoding="utf-8") as fh:
            loaded = json.load(fh)
        snapshot = loaded if isinstance(loaded, dict) else None
    except (OSError, ValueError):
        pass

    candidates = [
        line
        for line in (
            _idle_line_in(_env("RS_JOURNAL_LINE"), "the journal"),
            _idle_line_in(_read(str(runtime / "supervisor.log")), "supervisor.log"),
        )
        if line is not None
    ]
    newest = max(candidates, key=lambda line: line.at, default=None)
    idle, idle_before_start = newest, None
    if newest is not None and started_at is not None and newest.at < started_at:
        idle, idle_before_start = None, newest

    return Facts(
        queue=queue,
        now=now,
        pid_state=pid_state,
        started_at=started_at,
        saved_at=saved_at,
        poll_s=_poll_interval(queue),
        snapshot=snapshot,
        accounts=_command("account list --json", "accounts", "RS_ACCOUNTS"),
        sidecars=_command("sidecar list --json", "sidecars", "RS_SIDECARS"),
        idle=idle,
        idle_before_start=idle_before_start,
    )


# ---------------------------------------------------------------------------
# Wording helpers
# ---------------------------------------------------------------------------


def _when(at: datetime | None) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if at else "an unknown time"


def _duration(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            n = seconds // size
            return f"{n} {unit}{'s' if n != 1 else ''}"
    return f"{seconds} second{'s' if seconds != 1 else ''}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _within(poll_s: float) -> str:
    """How soon the supervisor's next check comes, in words."""
    if poll_s <= 60:
        return "within a minute"
    return f"within {math.ceil(poll_s / 60)} minutes"


def _skip_words(name: str) -> str:
    if name.startswith("readiness_"):
        kinds = name.removeprefix("readiness_").split("+")
        return "waiting for " + " and ".join(_READINESS_WORDS.get(k, k) for k in kinds)
    return _SKIP_WORDS.get(name, f"in state {name}")


def idle_breakdown_words(message: str) -> str | None:
    """``N unfinished held (a 1, b 2)`` in plain words, or None if it says
    nothing is unfinished."""
    m = re.fullmatch(r"no eligible tasks: (\d+) unfinished held \((.*)\)", message)
    if m is None:
        return None
    parts = []
    for item in m.group(2).split(", "):
        name, _, count = item.rpartition(" ")
        parts.append(f"{count} {_skip_words(name)}")
    return f"all {m.group(1)} unfinished tasks were held: " + ", ".join(parts)


# ---------------------------------------------------------------------------
# The plain-language summary
# ---------------------------------------------------------------------------


def _account_times(facts: Facts, name: str) -> dict[str, Any]:
    accounts = (facts.snapshot or {}).get("accounts") or {}
    entry = accounts.get(name)
    return entry if isinstance(entry, dict) else {}


def _account_clause(row: dict[str, Any]) -> tuple[str, bool]:
    """``(clause, takes_tasks)`` for one ``account list`` row."""
    name = row["name"]
    in_flight = int(row.get("in_flight_count") or 0)
    cap = int(row.get("dispatch_cap") or 0)
    state = row.get("state")
    still = f"; {in_flight} still running" if in_flight else ""
    if row.get("policy_error"):
        return f"{name} is not taking new tasks (its settings file has an error{still})", False
    if row.get("paused"):
        return f"{name} is not taking new tasks (paused{still})", False
    reasons = {
        "throttled_5h": "5-hour limit reached",
        "throttled_weekly": "using its weekly allowance faster than planned",
        "no_reading": "no usage reading yet",
        "error_drift": "its usage page cannot be read",
        None: "no usage reading yet",
    }
    if state not in _TAKING_STATES:
        reason = reasons.get(state, f"state {state}")
        return f"{name} is not taking new tasks ({reason}{still})", False
    if cap == 0:
        return f"{name} is not taking new tasks (its last usage reading allows none{still})", False
    if state == "slowing_down":
        normal = row.get("max_concurrency")
        return (
            f"{name} is taking tasks more slowly to save its 5-hour allowance "
            f"({in_flight} of {cap} slots in use; normally {normal})",
            True,
        )
    return f"{name} is taking tasks ({in_flight} of {cap} slots in use)", True


def _account_attention(facts: Facts, row: dict[str, Any], live: bool) -> str | None:
    """The 'not normal' item for an account that is not taking tasks, if any.

    A broken settings file and a pause are the operator's to fix whatever the
    supervisor is doing. The usage states are the supervisor's last reading,
    so they are left out while it is not running: the item about the
    supervisor says what matters then."""
    name = row["name"]
    in_flight = int(row.get("in_flight_count") or 0)
    still = (
        f" {_plural(in_flight, 'task')} it started {'is' if in_flight == 1 else 'are'} still running."
        if in_flight
        else ""
    )
    times = _account_times(facts, name)
    wakeup = _parse_time(times.get("scheduled_wakeup_at"))
    error = row.get("policy_error")
    if error:
        path = row.get("policy_file")
        where = "" if not path or path in error else f"{path}: "
        return (
            f"{name} takes no tasks because its settings file has an error: {where}{error}. "
            f"Fix the file; {name} takes tasks again {_within(facts.poll_s)}."
        )
    queue = facts.queue
    if row.get("paused"):
        return (
            f"{name} is paused, so it starts no new tasks.{still} Resume it with "
            f"`claude-task-runner account resume {name} --queue {queue}`."
        )
    state = row.get("state")
    if not live:
        return None
    if state == "throttled_5h":
        until = wakeup or _parse_time(times.get("last_5h_reset_at"))
        return (
            f"{name} has used {row.get('last_5h_util_pct')}% of its 5-hour allowance, so it "
            f"starts no new tasks until about {_when(until)}.{still} Nothing to do: it starts "
            f"again by itself."
        )
    if state == "throttled_weekly":
        until_text = f"until about {_when(wakeup)}" if wakeup else "for now"
        return (
            f"{name} is using its weekly allowance faster than planned "
            f"({row.get('last_weekly_util_pct')}% used so far), so it starts no new tasks "
            f"{until_text}.{still} Nothing to do: it starts again by itself."
        )
    if state in ("no_reading", None):
        last = _parse_time(row.get("last_reading_at"))
        seen = f"its last reading was at {_when(last)}" if last else "it has never had one"
        return (
            f"{name} has no recent usage reading ({seen}), so it starts no tasks. This clears "
            f"by itself within a few minutes of a supervisor start; if it lasts, see the runbook "
            f'section "An account stays in `no_reading`".'
        )
    if state == "error_drift":
        return (
            f"{name}'s usage page could not be read (its format changed), so it starts no "
            f"tasks. Run /runner-usage to check the usage reader."
        )
    if state in _TAKING_STATES and int(row.get("dispatch_cap") or 0) == 0:
        return (
            f"{name}'s last usage reading allows it no tasks, so it starts none until its next "
            f"reading. Nothing to do: it starts again by itself."
        )
    if state not in _TAKING_STATES:
        return f"{name} is in state {state} and starts no new tasks."
    return None


def _ceiling_sentence(
    facts: Facts, ceiling: dict[str, Any], rows: list[dict[str, Any]], taking: list[str] | None
) -> str:
    """How many tasks may run at once, and why. ``taking`` names the accounts
    taking tasks now; ``None`` (no live supervisor) leaves out what may run
    right now."""
    caps = ceiling.get("account_caps") or {}
    breakdown = " + ".join(f"{name} {cap}" for name, cap in caps.items()) or "none"
    working = "working accounts'" if ceiling.get("broken_accounts") else "accounts'"
    configured = ceiling.get("max_concurrency")
    value = int(ceiling.get("ceiling") or 0)
    subject = "Together they" if len(rows) > 1 else "It"
    tasks = _plural(value, "task")
    if ceiling.get("binds"):
        text = (
            f"{subject} may run {tasks} at once (the queue-wide cap; the {working} own "
            f"limits add up to {ceiling.get('accounts_total')}: {breakdown})."
        )
    elif configured is None:
        text = (
            f"{subject} may run {tasks} at once (the sum of the {working} own limits: "
            f"{breakdown}; there is no queue-wide cap)."
        )
    else:
        text = (
            f"{subject} may run {tasks} at once (the sum of the {working} own limits: "
            f"{breakdown}; the queue-wide cap of {configured} is not lower, so it changes "
            f"nothing)."
        )
    allowed = min(
        value,
        sum(int(row.get("dispatch_cap") or 0) for row in rows if row["name"] in (taking or [])),
    )
    if taking is not None and allowed < value:
        text += f" Right now {'they' if len(rows) > 1 else 'it'} may run {allowed}."
    initial = ceiling.get("initial_concurrency")
    if initial is not None and not facts.any_completed():
        text += f" Until the first task finishes, at most {initial} run at once."
    return text


def _supervisor(facts: Facts) -> tuple[str, str | None, bool]:
    """``(sentence, attention, live)`` about the supervisor."""
    if facts.pid_state != "alive":
        return (
            "The supervisor is not running.",
            "The supervisor is not running, so no new tasks start and finished tasks are not "
            f"recorded. Start it with `claude-task-runner supervisor start --queue {facts.queue}`; "
            "a watchdog, if installed, restarts it within a few minutes.",
            False,
        )
    if facts.saved_at is None:
        return (
            "The supervisor's process is running but has not saved its state yet.",
            "The supervisor has not saved its state yet; it normally does within a minute of "
            "starting. If this lasts, look at its log.",
            False,
        )
    age = facts.now - facts.saved_at
    if age.total_seconds() > STALE_AFTER_POLLS * facts.poll_s:
        return (
            f"The supervisor's process is running, but it last saved its state "
            f"{_duration(age)} ago.",
            f"The supervisor has not saved its state for {_duration(age)} (it normally does every "
            f"{facts.poll_s:g} seconds), so it may be stuck. Look at its log; if it stays this "
            f"way, restart it.",
            False,
        )
    return "The supervisor is running.", None, True


def plain_summary(facts: Facts) -> str:
    sentences: list[str] = []
    attention: list[str] = []

    sentence, item, live = _supervisor(facts)
    sentences.append(sentence)
    if item:
        attention.append(item)

    accounts_data = facts.accounts.data
    rows: list[dict[str, Any]] = list(accounts_data.get("accounts") or []) if accounts_data else []
    taking: list[str] = []
    if accounts_data is None:
        sentences.append("The accounts could not be read.")
        attention.append(
            f"The account list could not be read ({facts.accounts.error}), so what each account "
            f"is doing and how many tasks may run at once are unknown. Run `claude-task-runner "
            f"account list --queue {facts.queue}` to see the error."
        )
    elif not rows:
        sentences.append("No accounts are configured, so no task can start.")
    else:
        clauses = []
        for row in rows:
            clause, takes = _account_clause(row)
            clauses.append(clause)
            if takes:
                taking.append(row["name"])
            account_item = _account_attention(facts, row, live)
            if account_item:
                attention.append(account_item)
        # Without a live supervisor the states are its last record, not now.
        lead = "Its last record shows: " if not live and facts.snapshot is not None else ""
        sentences.append(lead + "; ".join(clauses) + ".")
        ceiling = accounts_data.get("queue_ceiling")
        if isinstance(ceiling, dict):
            sentences.append(_ceiling_sentence(facts, ceiling, rows, taking if live else None))
            if ceiling.get("binds"):
                attention.append(
                    f"The queue-wide cap of {ceiling.get('max_concurrency')} "
                    f"([concurrency].max_concurrency in claude_runner.toml) is lower than what the "
                    f"accounts allow together ({ceiling.get('accounts_total')}), so at most "
                    f"{ceiling.get('ceiling')} tasks run at once. Raise or remove it to use every "
                    f"account slot."
                )
        if not any(row.get("policy_error") for row in rows):
            sentences.append("No account settings file has errors.")

    st = facts.todo_statuses()
    waiting = st["no_state"] + st["pending"] + st["failed"]
    on_hold = st["deferred"] + st["weekly_paused"]
    sentences.append(
        f"Tasks: {st['running']} running, {waiting} waiting to start, "
        f"{st['awaiting_sidecar']} waiting for answers to questions, {on_hold} on hold "
        f"(waiting for a file, a retry time or a fix), {st['completed']} finished."
    )

    sidecars = facts.sidecars.data
    if sidecars is None:
        sentences.append("Whether any questions are waiting for you is unknown.")
        attention.append(
            f"The open questions could not be listed ({facts.sidecars.error}), so whether any "
            f"are waiting for you is unknown."
        )
    else:
        rows_sc = sidecars.get("sidecars") or []
        n_open = int(sidecars.get("n_open", len(rows_sc)))
        n_questions = sidecars.get("n_outstanding_questions")
        if n_questions is None:
            n_questions = sum(len(s.get("outstanding") or []) for s in rows_sc)
        if n_questions:
            sentences.append(
                f"{_plural(n_questions, 'question')} {'is' if n_questions == 1 else 'are'} "
                f"waiting for your answer; answer {'it' if n_questions == 1 else 'them'} with "
                f"/runner-answer-sidecar."
            )
        elif n_open:
            sentences.append(
                f"No questions are waiting for you, but {_plural(n_open, 'notice')} "
                f"{'is' if n_open == 1 else 'are'} waiting to be acknowledged "
                f"(/runner-answer-sidecar)."
            )
        else:
            sentences.append("No questions are waiting for you.")

    if st["failed_circuit_breaker"]:
        n = st["failed_circuit_breaker"]
        attention.append(
            f"{_plural(n, 'task')} stopped after failing repeatedly and will not be retried. "
            f"Look at {'it' if n == 1 else 'them'} with `claude-task-runner queue states "
            f"--status failed_circuit_breaker --queue {facts.queue}`."
        )
    if st["possibly_hung"]:
        n = st["possibly_hung"]
        attention.append(
            f"{_plural(n, 'task')} {'has' if n == 1 else 'have'} gone quiet while running and "
            f'may be stuck. See the runbook section "Task `possibly_hung` for hours".'
        )
    if st[None]:
        n = st[None]
        attention.append(
            f"{_plural(n, 'task state file')} cannot be read, so "
            f"{'that task is' if n == 1 else 'those tasks are'} skipped. Run "
            f"`claude-task-runner doctor --queue {facts.queue}` to find "
            f"{'it' if n == 1 else 'them'}."
        )

    in_flight = sum(int(row.get("in_flight_count") or 0) for row in rows)
    if live and taking and in_flight == 0:
        breakdown = (
            idle_breakdown_words(facts.idle.message)
            if facts.idle is not None and facts.idle.message.startswith("no eligible")
            else None
        )
        if facts.idle is not None and breakdown is not None:
            attention.append(
                f"Nothing is running because no task can start: as of {_when(facts.idle.at)}, "
                f'{breakdown}. See the runbook section "Nothing dispatches, but an account has '
                f'room".'
            )
        elif facts.idle is None or facts.idle.message.startswith("eligible tasks available"):
            attention.append(
                f"Nothing is running although {' and '.join(taking)} can take tasks, and the "
                f"supervisor has not logged why since it started. Look in its log for "
                f"`dispatch skipped task` lines."
            )

    out = ["### In plain words", "", " ".join(sentences), ""]
    if attention:
        out += ["**Not normal right now:**", ""]
        out += [f"- {item}" for item in attention]
    else:
        out.append("Everything looks normal.")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Detail sections
# ---------------------------------------------------------------------------


def accounts_section(facts: Facts) -> str:
    out = ["**Accounts and concurrency** (from `claude-task-runner account list --json`)", ""]
    out.append(
        "_Each account runs at most its own limit (`max_concurrency` in its "
        'runner-account.toml). "Allowed now" is that limit after throttling: lower while the '
        "account slows down to save its 5-hour allowance, 0 while it is throttled, paused, "
        'without a usage reading, or its settings file is broken. "In flight" means running '
        "now. The queue ceiling is the most tasks the whole queue may run at once._"
    )
    out.append("")
    data = facts.accounts.data
    if data is None:
        out.append(f"could not list: {facts.accounts.error}")
        return "\n".join(out) + "\n"
    rows = data.get("accounts") or []
    out.append("| account | limit | allowed now | in flight | paused | settings file | error |")
    out.append("|---|---:|---:|---:|:-:|---|---|")
    for row in rows:
        limit = row.get("max_concurrency")
        error = (row.get("policy_error") or "").replace("|", r"\|")
        out.append(
            f"| {row.get('name')} | {'—' if limit is None else limit} "
            f"| {row.get('dispatch_cap', 0)} | {row.get('in_flight_count', 0)} "
            f"| {'yes' if row.get('paused') else ''} | {row.get('policy_file') or '—'} "
            f"| {error or '—'} |"
        )
    ceiling = data.get("queue_ceiling")
    if isinstance(ceiling, dict):
        caps = " + ".join(f"{n} {c}" for n, c in (ceiling.get("account_caps") or {}).items())
        configured = ceiling.get("max_concurrency")
        out.append("")
        out.append(
            f"Queue ceiling: {ceiling.get('ceiling')} "
            f"(accounts' own limits: {caps or 'none'} = {ceiling.get('accounts_total')}; "
            f"queue-wide cap [concurrency].max_concurrency: "
            f"{'not set' if configured is None else configured}"
            f"{', which binds' if ceiling.get('binds') else ''}; "
            f"warm-up cap [concurrency].initial_concurrency: "
            f"{ceiling.get('initial_concurrency') if ceiling.get('initial_concurrency') is not None else 'not set'}"
            f"; broken account files: {', '.join(ceiling.get('broken_accounts') or []) or 'none'})"
        )
    return "\n".join(out) + "\n"


def counts_section(facts: Facts) -> str:
    """Status breakdown across every state YAML, todo/ and done/ alike: a row
    for every task status, a row for each status the runner does not know, and
    one for files that could not be read or name no status, so the rows add up
    to the total."""
    state_dir = facts.queue / ".claude_task_runner" / "state"
    counts: Counter[str | None] = Counter(
        _status_of(p) for p in glob.glob(os.path.join(state_dir, "*.yaml"))
    )
    out = ["**Queue counts**", ""]
    out.append(
        "_How many tasks are in each state. pending: not started yet; running: being worked on "
        "now; awaiting_sidecar: stopped to ask you a question; deferred: on hold until a file "
        "appears, a retry time passes or a setting is fixed; possibly_hung: running but silent "
        "for a long time; completed: done; failed: failed once and will be retried; "
        "failed_circuit_breaker: failed repeatedly and will not be retried; weekly_paused: held "
        "by the weekly limit. The state rows count every task the runner has a record of, "
        "finished ones too; todo/*.yaml counts the task files in todo/._"
    )
    out.append("")
    out.append("| field | value |")
    out.append("|---|---|")
    for k in STATUSES:
        out.append(f"| state.{k} | {counts[k]} |")
    for k in sorted(k for k in counts if k is not None and k not in STATUSES):
        out.append(f"| state.{k} (unknown status) | {counts[k]} |")
    out.append(f"| state files unreadable or without a status | {counts[None]} |")
    out.append(f"| state files (total) | {sum(counts.values())} |")
    todo = len(
        [p for p in glob.glob(os.path.join(facts.queue, "todo", "*.yaml")) if os.path.isfile(p)]
    )
    out.append(f"| todo/*.yaml | {todo} |")
    return "\n".join(out) + "\n"


def idle_section(facts: Facts) -> str:
    out = ["**Last idle report**", ""]
    out.append(
        "_When the supervisor finds no task it can start, it logs one `no eligible tasks:` line "
        "counting what holds the unfinished tasks, and logs again only when those counts "
        "change; `eligible tasks available again:` ends such a spell. The line describes the "
        "queue at its own time, which may be long ago._"
    )
    out.append("")
    if facts.idle is not None:
        out.append(f"{_when(facts.idle.at)} ({facts.idle.source}): `{facts.idle.message}`")
    elif facts.idle_before_start is not None:
        line = facts.idle_before_start
        out.append(
            f"none since the supervisor started; the last one, from before it started, is "
            f"{_when(line.at)} ({line.source}): `{line.message}`"
        )
    else:
        out.append("none found in the journal or supervisor.log")
    return "\n".join(out) + "\n"


SECTIONS = {
    "summary": plain_summary,
    "accounts": accounts_section,
    "counts": counts_section,
    "idle": idle_section,
}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in SECTIONS:
        print(f"usage: report.py {{{','.join(SECTIONS)}}}", file=sys.stderr)
        return 2
    sys.stdout.write(SECTIONS[argv[1]](gather()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
