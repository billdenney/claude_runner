"""The plain-language summary the runner-status ``snapshot.sh`` prints first.

``report.py summary`` turns what the script gathered into sentences a
non-specialist can read: whether the supervisor is running, what each account
is doing, how many tasks may run at once and why, broken account files, task
counts and questions, then one sentence per condition that is not normal.
These tests run the whole script against fixture queues, with the real CLI
(``account list --json``, ``sidecar list --json``) and a stub ``journalctl``,
and compare the summary word for word:

* a healthy two-account queue;
* an account whose ``runner-account.toml`` does not load;
* a queue-wide ``[concurrency].max_concurrency`` that binds;
* a paused account with a task still running;
* a drained queue, explained by the supervisor's idle line, and an idle line
  from before the supervisor started, which does not count;
* a supervisor that is not running, and one that stopped saving its state.

A live supervisor is faked with this test process's PID, which ``ps`` sees
alive, and a ``supervisor.json`` written just now.
"""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import get_args

import pytest

from claude_task_runner.cli.install_skills_cmd import _packaged_skill_dir
from claude_task_runner.config.loader import (
    PER_ACCOUNT_TOML_NAME,
    load_defaults,
    load_settings,
    resolve_accounts,
)
from claude_task_runner.observability import _reset_for_tests as _reset_logging_for_tests
from claude_task_runner.observability import configure_logging
from claude_task_runner.queue.schema import ReadinessKind, Task, TaskState, TaskStatus
from claude_task_runner.queue.store import (
    queue_runtime_dir,
    state_path_for,
    task_path_for,
    write_state_atomic,
    write_task_atomic,
)
from claude_task_runner.runner import orchestrator as orchestrator_mod
from claude_task_runner.runner import readiness as readiness_mod
from claude_task_runner.supervisor import persistence as persist_mod
from claude_task_runner.supervisor.states import (
    AccountState,
    InFlightRecord,
    SupervisorSnapshot,
    SupervisorState,
)

from ._cli_on_path import (
    account_list_call,
    make_bin,
    real_cli,
    run_script,
    sidecar_list_call,
    stub_journalctl,
)
from ._concurrency_fixtures import queue_toml, write_account_cap
from ._sidecar_files import make_request, write_request

SKILL_DIR = _packaged_skill_dir("runner-status")
SNAPSHOT = SKILL_DIR / "snapshot.sh"
REPORT = SKILL_DIR / "report.py"

SINCE = datetime(2026, 10, 16, 8, 0, tzinfo=UTC)
WAKEUP = datetime(2026, 10, 16, 14, 5, tzinfo=UTC)
BAD_TOML = "max_concurrency 5\n"
NOTHING_WRONG = "Everything looks normal."
RUNBOOK_IDLE = 'See the runbook section "Nothing dispatches, but an account has room".'


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    return make_bin(tmp_path)


@pytest.fixture
def queue(tmp_path: Path) -> Path:
    queue = tmp_path / "queue"
    (queue / "todo").mkdir(parents=True)
    queue_runtime_dir(queue)
    return queue


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _accounts(
    queue: Path, tmp_path: Path, caps: dict[str, int | None], concurrency: str = ""
) -> dict[str, Path]:
    """Declare ``caps`` in the queue's claude_runner.toml; ``None`` writes a
    runner-account.toml that does not load."""
    dirs = {name: tmp_path / f"cfg-{name}" for name in caps}
    for name, cap in caps.items():
        if cap is None:
            dirs[name].mkdir()
            (dirs[name] / PER_ACCOUNT_TOML_NAME).write_text(BAD_TOML, encoding="utf-8")
        else:
            write_account_cap(dirs[name], cap)
    queue_toml(queue / "claude_runner.toml", dirs, concurrency)
    return dirs


def _snapshot(
    queue: Path,
    states: dict[str, AccountState],
    in_flight: dict[str, int],
    *,
    saved_ago: timedelta = timedelta(0),
) -> None:
    """Write supervisor.json as the supervisor does, modified ``saved_ago``."""
    records = [
        InFlightRecord(task_id=f"run-{name}-{i}", account=name, started_at=SINCE)
        for name, n in in_flight.items()
        for i in range(n)
    ]
    snapshot = SupervisorSnapshot(
        state=SupervisorState.DISPATCHING,
        since=SINCE,
        accounts=states,
        in_flight=records,
        in_flight_task_ids=[r.task_id for r in records],
    )
    path = persist_mod.supervisor_state_path(queue)
    persist_mod.write_atomic(snapshot, path)
    when = (datetime.now(UTC) - saved_ago).timestamp()
    os.utime(path, (when, when))


def _state(state: SupervisorState, **fields: object) -> AccountState:
    return AccountState.model_validate({"state": state, "since": SINCE, **fields})


def _tasks(queue: Path, statuses: dict[str, str | None]) -> None:
    """A todo/ task per id, with a state file of that status (none for ``None``)."""
    for task_id, status in statuses.items():
        task = Task.model_validate({"id": task_id, "title": task_id, "prompt": "do it"})
        write_task_atomic(task, task_path_for(queue, task_id))
        if status is not None:
            write_state_atomic(
                TaskState.model_validate({"task_id": task_id, "status": status}),
                state_path_for(queue, task_id),
            )


def _mix(running: int, waiting: int, asking: int, held: int, done: int) -> dict[str, str | None]:
    statuses: dict[str, str | None] = {}
    for prefix, status, n in (
        ("run", "running", running),
        ("wait", None, waiting),
        ("ask", "awaiting_sidecar", asking),
        ("hold", "deferred", held),
        ("done", "completed", done),
    ):
        statuses.update({f"{prefix}-{i:02d}": status for i in range(n)})
    return statuses


def _live(queue: Path) -> None:
    """A PID file naming a live process: this test's own."""
    (queue / ".claude_task_runner" / "supervisor.pid").write_text(f"{os.getpid()}\n")


def _idle_line(at: datetime, message: str) -> str:
    stamp = at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return f"{stamp} [info     ] {message} [claude_task_runner.runner.orchestrator]"


def _when(at: datetime) -> str:
    return at.strftime("%Y-%m-%d %H:%M UTC")


def _run(queue: Path, bin_dir: Path) -> subprocess.CompletedProcess[str]:
    return run_script(SNAPSHOT, bin_dir, "--queue", str(queue), cwd=queue.parent)


def _summary(queue: Path, bin_dir: Path) -> str:
    """The summary block, between its heading and the details."""
    real_cli(bin_dir)
    proc = _run(queue, bin_dir)
    assert (proc.returncode, proc.stderr) == (0, ""), proc.stdout + proc.stderr
    head, _, rest = proc.stdout.partition("### In plain words\n\n")
    assert head.startswith("## Queue status — ")
    summary, _, _ = rest.partition("\n### Details\n")
    return summary


def _expected(paragraph: str, attention: list[str] | None = None) -> str:
    tail = (
        "**Not normal right now:**\n\n" + "".join(f"- {item}\n" for item in attention)
        if attention
        else NOTHING_WRONG + "\n"
    )
    return f"{paragraph}\n\n{tail}"


TASKS_HEALTHY = (
    "Tasks: 8 running, 10 waiting to start, 3 waiting for answers to questions, 2 on hold "
    "(waiting for a file, a retry time or a fix), 4 finished."
)
NO_ERRORS = "No account settings file has errors."


# ---------------------------------------------------------------------------
# The fixtures
# ---------------------------------------------------------------------------


def test_healthy_two_account_queue(queue: Path, tmp_path: Path, bin_dir: Path) -> None:
    _accounts(queue, tmp_path, {"personal": 5, "work": 3})
    _snapshot(
        queue,
        {
            "personal": _state(SupervisorState.DISPATCHING),
            "work": _state(SupervisorState.DISPATCHING),
        },
        {"personal": 5, "work": 3},
    )
    _tasks(queue, _mix(running=8, waiting=10, asking=3, held=2, done=4))
    write_request(queue, make_request("ask-00", question_ids=("q1", "q2")))
    _live(queue)
    stub_journalctl(bin_dir)

    assert _summary(queue, bin_dir) == _expected(
        "The supervisor is running. personal is taking tasks (5 of 5 slots in use); work is "
        "taking tasks (3 of 3 slots in use). Together they may run 8 tasks at once (the sum of "
        "the accounts' own limits: personal 5 + work 3; there is no queue-wide cap). "
        f"{NO_ERRORS} {TASKS_HEALTHY} 2 questions are waiting for your answer; answer them "
        "with /runner-answer-sidecar."
    )


def test_broken_account_file(queue: Path, tmp_path: Path, bin_dir: Path) -> None:
    dirs = _accounts(queue, tmp_path, {"personal": None, "work": 3})
    _snapshot(
        queue,
        {
            "personal": _state(SupervisorState.DISPATCHING),
            "work": _state(SupervisorState.DISPATCHING),
        },
        {"work": 3},
    )
    _tasks(queue, _mix(running=3, waiting=1, asking=0, held=0, done=0))
    _live(queue)
    stub_journalctl(bin_dir)
    # The error is the loader's own message, which the summary relays as is.
    (broken,) = resolve_accounts(load_settings(queue / "claude_runner.toml")).broken
    assert broken.path == str(dirs["personal"] / PER_ACCOUNT_TOML_NAME)
    assert broken.path in broken.error

    assert _summary(queue, bin_dir) == _expected(
        "The supervisor is running. personal is not taking new tasks (its settings file has an "
        "error); work is taking tasks (3 of 3 slots in use). Together they may run 3 tasks at "
        "once (the sum of the working accounts' own limits: work 3; there is no queue-wide "
        "cap). Tasks: 3 running, 1 waiting to start, 0 waiting for answers to questions, 0 on "
        "hold (waiting for a file, a retry time or a fix), 0 finished. No questions are waiting "
        "for you.",
        [
            f"personal takes no tasks because its settings file has an error: {broken.error}. "
            "Fix the file; personal takes tasks again within a minute."
        ],
    )


def test_binding_queue_wide_cap(queue: Path, tmp_path: Path, bin_dir: Path) -> None:
    _accounts(queue, tmp_path, {"personal": 5, "work": 3}, concurrency="max_concurrency = 4")
    _snapshot(
        queue,
        {
            "personal": _state(SupervisorState.DISPATCHING),
            "work": _state(SupervisorState.DISPATCHING),
        },
        {"personal": 3, "work": 1},
    )
    _tasks(queue, _mix(running=4, waiting=6, asking=0, held=0, done=1))
    _live(queue)
    stub_journalctl(bin_dir)

    assert _summary(queue, bin_dir) == _expected(
        "The supervisor is running. personal is taking tasks (3 of 5 slots in use); work is "
        "taking tasks (1 of 3 slots in use). Together they may run 4 tasks at once (the "
        "queue-wide cap; the accounts' own limits add up to 8: personal 5 + work 3). "
        f"{NO_ERRORS} Tasks: 4 running, 6 waiting to start, 0 waiting for answers to "
        "questions, 0 on hold (waiting for a file, a retry time or a fix), 1 finished. No "
        "questions are waiting for you.",
        [
            "The queue-wide cap of 4 ([concurrency].max_concurrency in claude_runner.toml) is "
            "lower than what the accounts allow together (8), so at most 4 tasks run at once. "
            "Raise or remove it to use every account slot."
        ],
    )


def test_paused_account_with_a_task_still_running(
    queue: Path, tmp_path: Path, bin_dir: Path
) -> None:
    _accounts(queue, tmp_path, {"personal": 5, "work": 3})
    _snapshot(
        queue,
        {
            "personal": _state(SupervisorState.DISPATCHING),
            "work": _state(SupervisorState.DISPATCHING),
        },
        {"personal": 2, "work": 1},
    )
    markers = queue / ".claude_task_runner" / "account_paused"
    markers.mkdir()
    (markers / "work").touch()
    _tasks(queue, _mix(running=3, waiting=4, asking=0, held=0, done=0))
    _live(queue)
    stub_journalctl(bin_dir)

    assert _summary(queue, bin_dir) == _expected(
        "The supervisor is running. personal is taking tasks (2 of 5 slots in use); work is not "
        "taking new tasks (paused; 1 still running). Together they may run 8 tasks at once (the "
        "sum of the accounts' own limits: personal 5 + work 3; there is no queue-wide cap). "
        f"Right now they may run 5. {NO_ERRORS} Tasks: 3 running, 4 waiting to start, 0 "
        "waiting for answers to questions, 0 on hold (waiting for a file, a retry time or a "
        "fix), 0 finished. No questions are waiting for you.",
        [
            "work is paused, so it starts no new tasks. 1 task it started is still running. "
            f"Resume it with `claude-task-runner account resume work --queue {queue}`."
        ],
    )


def test_throttled_account_says_when_it_starts_again(
    queue: Path, tmp_path: Path, bin_dir: Path
) -> None:
    _accounts(queue, tmp_path, {"personal": 5, "work": 3})
    _snapshot(
        queue,
        {
            "personal": _state(
                SupervisorState.THROTTLED_WEEKLY,
                last_weekly_util_pct=33,
                target_concurrency=0,
                scheduled_wakeup_at=WAKEUP,
            ),
            "work": _state(SupervisorState.DISPATCHING),
        },
        {"work": 3},
    )
    _tasks(queue, _mix(running=3, waiting=2, asking=0, held=0, done=0))
    _live(queue)
    stub_journalctl(bin_dir)

    assert _summary(queue, bin_dir) == _expected(
        "The supervisor is running. personal is not taking new tasks (using its weekly allowance "
        "faster than planned); work is taking tasks (3 of 3 slots in use). Together they may "
        "run 8 tasks at once (the sum of the accounts' own limits: personal 5 + work 3; there "
        f"is no queue-wide cap). Right now they may run 3. {NO_ERRORS} Tasks: 3 running, 2 "
        "waiting to start, 0 waiting for answers to questions, 0 on hold (waiting for a file, a "
        "retry time or a fix), 0 finished. No questions are waiting for you.",
        [
            "personal is using its weekly allowance faster than planned (33% used so far), so "
            f"it starts no new tasks until about {_when(WAKEUP)}. Nothing to do: it starts "
            "again by itself."
        ],
    )


def _drained(queue: Path, tmp_path: Path) -> None:
    _accounts(queue, tmp_path, {"personal": 5, "work": 3})
    _snapshot(
        queue,
        {
            "personal": _state(SupervisorState.DISPATCHING),
            "work": _state(SupervisorState.DISPATCHING),
        },
        {},
    )
    _tasks(queue, _mix(running=0, waiting=2, asking=1, held=1, done=7))
    write_request(queue, make_request("ask-00"))
    _live(queue)


DRAINED_PARAGRAPH = (
    "The supervisor is running. personal is taking tasks (0 of 5 slots in use); work is taking "
    "tasks (0 of 3 slots in use). Together they may run 8 tasks at once (the sum of the "
    f"accounts' own limits: personal 5 + work 3; there is no queue-wide cap). {NO_ERRORS} "
    "Tasks: 0 running, 2 waiting to start, 1 waiting for answers to questions, 1 on hold "
    "(waiting for a file, a retry time or a fix), 7 finished. 1 question is waiting for your "
    "answer; answer it with /runner-answer-sidecar."
)


def test_drained_queue_is_explained_by_the_idle_line(
    queue: Path, tmp_path: Path, bin_dir: Path
) -> None:
    _drained(queue, tmp_path)
    at = datetime.now(UTC) - timedelta(seconds=5)
    stub_journalctl(
        bin_dir,
        _idle_line(
            at,
            "no eligible tasks: 4 unfinished held "
            "(block_dispatch 2, readiness_awaiting_sidecar_response+missing_file 1, "
            "unanswered_sidecar 1)",
        ),
    )

    assert _summary(queue, bin_dir) == _expected(
        DRAINED_PARAGRAPH,
        [
            f"Nothing is running because no task can start: as of {_when(at)}, all 4 unfinished "
            "tasks were held: 2 blocked by the block list, 1 waiting for an answer and a file, 1 "
            f"waiting for your answer. {RUNBOOK_IDLE}"
        ],
    )


def test_idle_line_from_before_the_supervisor_started_does_not_count(
    queue: Path, tmp_path: Path, bin_dir: Path
) -> None:
    """A cron supervisor's log keeps earlier runs' lines; one older than the
    running process says nothing about now."""
    _drained(queue, tmp_path)
    stub_journalctl(bin_dir)
    old = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    (queue / ".claude_task_runner" / "supervisor.log").write_text(
        _idle_line(old, "no eligible tasks: 1 unfinished held (depends_on 1)") + "\n"
    )

    assert _summary(queue, bin_dir) == _expected(
        DRAINED_PARAGRAPH,
        [
            "Nothing is running although personal and work can take tasks, and the supervisor "
            "has not logged why since it started. Look in its log for `dispatch skipped task` "
            "lines."
        ],
    )
    proc = _run(queue, bin_dir)
    assert (
        "none since the supervisor started; the last one, from before it started, is "
        f"{_when(old)} (supervisor.log): `no eligible tasks: 1 unfinished held (depends_on 1)`"
    ) in proc.stdout


def test_supervisor_not_running(queue: Path, tmp_path: Path, bin_dir: Path) -> None:
    """The account states are the last record, so they are reported as such,
    and no usage item is raised on the strength of them."""
    _accounts(queue, tmp_path, {"personal": 5, "work": 3})
    _snapshot(
        queue,
        {
            "personal": _state(SupervisorState.THROTTLED_5H, last_5h_util_pct=97),
            "work": _state(SupervisorState.DISPATCHING),
        },
        {},
    )
    _tasks(queue, _mix(running=0, waiting=5, asking=0, held=0, done=0))
    (queue / ".claude_task_runner" / "supervisor.pid").write_text("999999999\n")
    stub_journalctl(bin_dir)

    assert _summary(queue, bin_dir) == _expected(
        "The supervisor is not running. Its last record shows: personal is not taking new tasks "
        "(5-hour limit reached); work is taking tasks (0 of 3 slots in use). Together they may "
        "run 8 tasks at once (the sum of the accounts' own limits: personal 5 + work 3; there "
        f"is no queue-wide cap). {NO_ERRORS} Tasks: 0 running, 5 "
        "waiting to start, 0 waiting for answers to questions, 0 on hold (waiting for a file, a "
        "retry time or a fix), 0 finished. No questions are waiting for you.",
        [
            "The supervisor is not running, so no new tasks start and finished tasks are not "
            f"recorded. Start it with `claude-task-runner supervisor start --queue {queue}`; a "
            "watchdog, if installed, restarts it within a few minutes."
        ],
    )


def test_supervisor_that_stopped_saving_its_state(
    queue: Path, tmp_path: Path, bin_dir: Path
) -> None:
    _accounts(queue, tmp_path, {"personal": 5, "work": 3})
    _snapshot(
        queue,
        {
            "personal": _state(SupervisorState.DISPATCHING),
            "work": _state(SupervisorState.DISPATCHING),
        },
        {"personal": 1},
        saved_ago=timedelta(minutes=30, seconds=10),
    )
    _tasks(queue, _mix(running=1, waiting=0, asking=0, held=0, done=0))
    _live(queue)
    stub_journalctl(bin_dir)

    assert _summary(queue, bin_dir) == _expected(
        "The supervisor's process is running, but it last saved its state 30 minutes ago. Its "
        "last record shows: personal is taking tasks (1 of 5 slots in use); work is taking tasks "
        "(0 of 3 slots in use). Together they may run 8 tasks at once (the sum of the accounts' "
        f"own limits: personal 5 + work 3; there is no queue-wide cap). {NO_ERRORS} Tasks: 1 "
        "running, 0 waiting to start, 0 waiting for answers to questions, 0 on hold (waiting "
        "for a file, a retry time or a fix), 0 finished. No questions are waiting for you.",
        [
            "The supervisor has not saved its state for 30 minutes (it normally does every 60 "
            "seconds), so it may be stuck. Look at its log; if it stays this way, restart it."
        ],
    )


# ---------------------------------------------------------------------------
# Contracts with the CLI and the package
# ---------------------------------------------------------------------------


def test_script_calls_the_cli_once_per_listing(queue: Path, bin_dir: Path) -> None:
    log = real_cli(bin_dir)
    stub_journalctl(bin_dir)
    proc = _run(queue, bin_dir)
    assert proc.returncode == 0, proc.stderr
    assert log.read_text() == account_list_call(queue) + sidecar_list_call(queue)


def test_default_poll_interval_matches_the_package() -> None:
    """report.py cannot import the package, so it carries its own copy of the
    default ``[usage].poll_interval_s`` for the staleness test."""
    source = REPORT.read_text()
    default = load_defaults()["usage"]["poll_interval_s"]
    assert f"POLL_INTERVAL_DEFAULT_S = {float(default)!r}" in source


def _report_module() -> ModuleType:
    """report.py, imported from its file: it is a standalone script."""
    spec = importlib.util.spec_from_file_location("runner_status_report", REPORT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_every_skip_category_has_plain_words() -> None:
    """The idle line's categories come from the selector and the readiness
    gate; report.py keeps its own words for them. A category without words
    would print as "in state <name>"."""
    report = _report_module()
    categories = {
        value
        for name, value in vars(orchestrator_mod).items()
        if name.startswith("_SKIP_") and name != "_SKIP_READINESS_PREFIX"
    }
    # Statuses the selector reports under their own name (never pending or
    # failed, which it dispatches; awaiting_sidecar and deferred have their
    # own categories; completed is left out of the line).
    categories |= set(get_args(TaskStatus)) - {
        "pending",
        "failed",
        "awaiting_sidecar",
        "deferred",
        "completed",
    }
    assert sorted(categories - set(report._SKIP_WORDS)) == []

    task = Task.model_validate(
        {
            "id": "t",
            "title": "t",
            "prompt": "p",
            "requires": [
                {"kind": kind, "path": "/nonexistent/file"} if kind == "file" else {"kind": kind}
                for kind in get_args(ReadinessKind)
            ],
        }
    )
    unmet = readiness_mod.unmet_details(task, Path("/nonexistent"), open_sidecar_task_ids={"t"})
    assert len(unmet) == len(get_args(ReadinessKind))
    assert sorted({u.category for u in unmet} - set(report._READINESS_WORDS)) == []


def test_idle_line_is_read_as_the_supervisor_writes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The line comes from the orchestrator through the supervisor's text log
    format; report.py must find it in that format, message intact."""
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    _reset_logging_for_tests()
    try:
        configure_logging(level="INFO", fmt="text")
        orchestrator_mod.IdleDispatchLog().observe(
            0, Counter({"block_dispatch": 2, "readiness_missing_file": 1, "completed": 9})
        )
    finally:
        _reset_logging_for_tests()
    report = _report_module()
    line = report._idle_line_in(buf.getvalue(), "the journal")
    assert line is not None, buf.getvalue()
    assert line.message == (
        "no eligible tasks: 3 unfinished held (block_dispatch 2, readiness_missing_file 1)"
    )
    assert abs((datetime.now(UTC) - line.at).total_seconds()) < 60
    assert report.idle_breakdown_words(line.message) == (
        "all 3 unfinished tasks were held: 2 blocked by the block list, 1 waiting for a file"
    )
