"""The plain-language summary the runner-status ``snapshot.sh`` prints first.

``report.py`` words what the runner's own CLI says: ``account list --json``,
``queue holds --json`` (the supervisor's candidate selection, read-only) and
``sidecar list --json``. These tests run the whole script against fixture
queues with the real CLI and a stub ``journalctl``, and compare the summary
word for word. A live supervisor is a child process this test starts, with
the command line a supervisor has, so the script can read its ``--config``.

Further down: the details' idle report (journal or supervisor.log, the text
and JSON log formats, a large log), a section that fails, and gates that
keep report.py's words in step with the runner's categories and states.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import get_args

import pytest

from claude_task_runner.cli.install_skills_cmd import _packaged_skill_dir
from claude_task_runner.config.loader import PER_ACCOUNT_TOML_NAME, load_settings, resolve_accounts
from claude_task_runner.observability import _reset_for_tests as _reset_logging_for_tests
from claude_task_runner.observability import configure_logging
from claude_task_runner.queue.schema import ReadinessKind, Task, TaskStatus
from claude_task_runner.queue.sidecar import sidecar_dir_for, write_response
from claude_task_runner.queue.store import queue_runtime_dir, state_path_for
from claude_task_runner.runner import orchestrator as orchestrator_mod
from claude_task_runner.runner import readiness as readiness_mod
from claude_task_runner.supervisor import persistence as persist_mod
from claude_task_runner.supervisor.states import (
    AccountState,
    InFlightRecord,
    SupervisorSnapshot,
    SupervisorState,
)
from claude_task_runner.usage.drift import AUTH_FAILURE_PREFIX

from ._cli_on_path import (
    account_list_call,
    calls_in,
    make_bin,
    real_cli,
    run_script,
    sidecar_list_call,
    stub_journalctl,
)
from ._concurrency_fixtures import write_account_cap
from ._mixed_queue import add_state, add_task
from ._sidecar_files import make_request, make_response, write_request

SKILL_DIR = _packaged_skill_dir("runner-status")
SNAPSHOT = SKILL_DIR / "snapshot.sh"
REPORT = SKILL_DIR / "report.py"

SINCE = datetime(2026, 10, 16, 8, 0, tzinfo=UTC)
WEEKLY_RESET = datetime(2026, 10, 21, 11, 0, tzinfo=UTC)
BLOCK_FILE = "needs_acquisition.jsonl"
BAD_TOML = "max_concurrency 5\n"
NORMAL = "\nEverything looks normal.\n"
RUNNING = "- **Supervisor:** running.\n"
NO_QUESTIONS = "- **Questions for you:** none.\n"
EIGHT = "up to 8 at once (personal 5 + work 3; no queue-wide cap)."
CRASH = "Traceback (most recent call last):\nRuntimeError: boom\n"


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    bin_dir = make_bin(tmp_path)
    real_cli(bin_dir)
    stub_journalctl(bin_dir)
    return bin_dir


@pytest.fixture
def queue(tmp_path: Path) -> Path:
    queue = tmp_path / "queue"
    (queue / "todo").mkdir(parents=True)
    queue_runtime_dir(queue)
    return queue


@pytest.fixture
def supervisor(queue: Path) -> Iterator[Callable[..., int]]:
    """Start a stand-in supervisor: an idle child process with a supervisor's
    command line, named by the queue's PID file. Killed at teardown."""
    children: list[subprocess.Popen[bytes]] = []

    def start(config: Path | None = None) -> int:
        argv = [sys.executable, "-c", "import time; time.sleep(600)"]
        argv += ["supervisor", "start", "--queue", str(queue)]
        if config is not None:
            argv += ["--config", str(config)]
        child = subprocess.Popen(argv, start_new_session=True)
        children.append(child)
        (queue / ".claude_task_runner" / "supervisor.pid").write_text(f"{child.pid}\n")
        return child.pid

    yield start
    for child in children:
        child.kill()
        child.wait(timeout=10)


def _config_text(dirs: dict[str, Path], extra: str = "") -> str:
    lines = [f'[dispatch]\ndispatch_block_file = "{BLOCK_FILE}"\n']
    lines += [f'[[accounts]]\nname = "{n}"\nconfig_dir = "{d}"\n' for n, d in dirs.items()]
    return "\n".join(lines) + extra


def _config(
    queue: Path, tmp_path: Path, caps: dict[str, int | None], extra: str = ""
) -> dict[str, Path]:
    """The queue's claude_runner.toml declaring ``caps`` (``None`` writes a
    runner-account.toml that does not load) and the block-list file."""
    dirs = {name: tmp_path / f"cfg-{name}" for name in caps}
    for name, cap in caps.items():
        if cap is None:
            dirs[name].mkdir()
            (dirs[name] / PER_ACCOUNT_TOML_NAME).write_text(BAD_TOML, encoding="utf-8")
        else:
            write_account_cap(dirs[name], cap)
    (queue / "claude_runner.toml").write_text(_config_text(dirs, extra), encoding="utf-8")
    return dirs


def _state(state: SupervisorState, **fields: object) -> AccountState:
    return AccountState.model_validate({"state": state, "since": SINCE, **fields})


def _supervisor_json(
    queue: Path,
    states: dict[str, AccountState],
    running: dict[str, int] | None = None,
    *,
    saved_ago: timedelta = timedelta(0),
) -> None:
    """supervisor.json as the supervisor writes it, and for each account in
    ``running`` that many todo tasks it has in flight."""
    records = []
    for name, n in (running or {}).items():
        for i in range(n):
            task_id = f"run-{name}-{i}"
            add_task(queue, task_id)
            add_state(queue, task_id, "running")
            records.append(InFlightRecord(task_id=task_id, account=name, started_at=SINCE))
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


def _tasks(
    queue: Path,
    *,
    new: int = 0,
    answered: int = 0,
    asking: int = 0,
    questions: int = 1,
    blocked: int = 0,
    depends: int = 0,
    files: int = 0,
    done: int = 0,
) -> None:
    """Todo tasks in the usual places: ``new`` never started; ``answered``
    asked and were answered; ``asking`` wait for ``questions`` answers each;
    ``blocked`` are in the block list; ``depends`` wait for a task that never
    finishes; ``files`` for a file that is absent; ``done`` finished."""
    for i in range(new):
        add_task(queue, f"new-{i}")
    for i in range(answered):
        add_task(queue, f"answered-{i}")
        add_state(queue, f"answered-{i}", "awaiting_sidecar")
        write_request(queue, make_request(f"answered-{i}"))
        write_response(queue, make_response(f"answered-{i}"))
    ids = tuple(f"q{n}" for n in range(1, questions + 1))
    for i in range(asking):
        add_task(queue, f"asking-{i}")
        add_state(queue, f"asking-{i}", "awaiting_sidecar")
        write_request(queue, make_request(f"asking-{i}", question_ids=ids))
    rows = []
    for i in range(blocked):
        add_task(queue, f"blocked-{i}")
        add_state(queue, f"blocked-{i}", "pending")
        rows.append(json.dumps({"task": f"blocked-{i}", "block_dispatch": True}) + "\n")
    (queue / BLOCK_FILE).write_text("".join(rows), encoding="utf-8")
    for i in range(depends):
        add_task(queue, f"depends-{i}", depends_on=["never-finishes"])
    for i in range(files):
        add_task(queue, f"files-{i}", requires=[{"kind": "file", "path": str(queue / "absent.md")}])
    for i in range(done):
        add_task(queue, f"done-{i}")
        add_state(queue, f"done-{i}", "completed")


def _two_accounts(
    queue: Path,
    tmp_path: Path,
    personal: AccountState | None = None,
    work: AccountState | None = None,
    running: dict[str, int] | None = None,
    extra: str = "",
) -> dict[str, Path]:
    dirs = _config(queue, tmp_path, {"personal": 5, "work": 3}, extra)
    _supervisor_json(
        queue,
        {
            "personal": personal or _state(SupervisorState.DISPATCHING),
            "work": work or _state(SupervisorState.DISPATCHING),
        },
        running,
    )
    return dirs


def _cli_with(bin_dir: Path, **overrides: tuple[str, str, int]) -> None:
    """A ``claude-task-runner`` that answers the commands named in
    ``overrides`` (``account``, ``queue``, ``sidecar``) with (stdout,
    stderr, exit code) and runs the real CLI for the rest."""
    real = (
        f'exec {shlex.quote(sys.executable)} -c "from claude_task_runner.cli import main; main()"'
    )
    cases = "".join(
        f"  {command})\n    printf %s {shlex.quote(out)}\n"
        f"    printf %s {shlex.quote(err)} >&2\n    exit {code} ;;\n"
        for command, (out, err, code) in overrides.items()
    )
    exe = bin_dir / "claude-task-runner"
    exe.write_text(f'#!/bin/sh\ncase "$1" in\n{cases}esac\n{real} "$@"\n')
    exe.chmod(0o755)


def _run(queue: Path, bin_dir: Path) -> subprocess.CompletedProcess[str]:
    return run_script(SNAPSHOT, bin_dir, "--queue", str(queue), cwd=queue.parent)


def _summary(queue: Path, bin_dir: Path, rc: int = 0) -> str:
    """The summary, between its heading and the details."""
    proc = _run(queue, bin_dir)
    assert proc.returncode == rc, proc.stdout + proc.stderr
    head, _, rest = proc.stdout.partition("### In plain words\n\n")
    assert head.startswith("## Queue status — "), proc.stdout
    summary, _, _ = rest.partition("### Details\n")
    # The blank line before the details heading is not part of the summary.
    return summary.removesuffix("\n")


def _when(at: datetime) -> str:
    return at.strftime("%Y-%m-%d %H:%M UTC")


def _accounts(*lines: str, heading: str = "Accounts") -> str:
    return f"- **{heading}:**\n" + "".join(f"  - {line}.\n" for line in lines)


def _attention(*items: str) -> str:
    return "\n**Not normal right now:**\n\n" + "".join(f"- {i}\n" for i in items)


def _info(*items: str) -> str:
    return "\n**For your information:**\n\n" + "".join(f"- {i}\n" for i in items)


def _idle_line(at: datetime, message: str) -> str:
    stamp = at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return f"{stamp} [info     ] {message} [claude_task_runner.runner.orchestrator]"


def _report_module() -> ModuleType:
    """report.py, imported from its file: it is a standalone script."""
    spec = importlib.util.spec_from_file_location("runner_status_report", REPORT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# The summary, fixture by fixture
# ---------------------------------------------------------------------------


def test_healthy_two_account_queue(
    queue: Path, tmp_path: Path, bin_dir: Path, supervisor: Callable[..., int]
) -> None:
    """Answered tasks count as able to start, not as waiting for an answer;
    only the open request's questions are "for you"."""
    _two_accounts(queue, tmp_path, running={"personal": 5, "work": 3})
    _tasks(queue, new=1, answered=21, asking=1, questions=2, blocked=6, depends=3, files=4, done=9)
    supervisor()

    assert _summary(queue, bin_dir) == (
        RUNNING
        + _accounts(
            "personal: taking tasks, 5 of 5 slots in use", "work: taking tasks, 3 of 3 slots in use"
        )
        + f"- **How many can run:** {EIGHT} Right now 8 running, with no room for more.\n"
        "- **Tasks:** 8 running; 22 can start as soon as a slot is free (21 of them answered "
        "after asking you); 1 waits for your answer; 4 wait for a file; 3 wait for another task "
        "to finish; 6 held by the block list (papers or files still needed, or deliberate "
        "holds); 9 finished.\n"
        "- **Questions for you:** 2, in 1 request; answer with /runner-answer-sidecar.\n" + NORMAL
    )


def test_broken_account_file(
    queue: Path, tmp_path: Path, bin_dir: Path, supervisor: Callable[..., int]
) -> None:
    dirs = _config(queue, tmp_path, {"personal": None, "work": 3})
    states = {
        "personal": _state(SupervisorState.DISPATCHING),
        "work": _state(SupervisorState.DISPATCHING),
    }
    _supervisor_json(queue, states, {"work": 3})
    _tasks(queue, new=1)
    supervisor()
    # The error is the loader's own message, which the summary relays as is.
    (broken,) = resolve_accounts(load_settings(queue / "claude_runner.toml")).broken
    assert (
        broken.path == str(dirs["personal"] / PER_ACCOUNT_TOML_NAME) and broken.path in broken.error
    )

    assert _summary(queue, bin_dir) == (
        RUNNING
        + _accounts(
            "personal: takes no tasks, because its settings file has an error",
            "work: taking tasks, 3 of 3 slots in use",
        )
        + "- **How many can run:** up to 3 at once (work 3; no queue-wide cap). Not counted: "
        "personal (settings file error). Right now 3 running, with no room for more.\n"
        "- **Tasks:** 3 running; 1 can start as soon as a slot is free; 0 finished.\n"
        + NO_QUESTIONS
        + _attention(
            f"personal takes no tasks because its settings file has an error: {broken.error}. "
            "Fix the file; personal takes tasks again within a minute."
        )
    )


def test_binding_queue_wide_cap(
    queue: Path, tmp_path: Path, bin_dir: Path, supervisor: Callable[..., int]
) -> None:
    """Also: an idle account takes tasks."""
    _two_accounts(
        queue,
        tmp_path,
        work=_state(SupervisorState.IDLE),
        running={"personal": 3, "work": 1},
        extra="[concurrency]\nmax_concurrency = 4\n",
    )
    _tasks(queue, new=6, done=1)
    supervisor()

    assert _summary(queue, bin_dir) == (
        RUNNING
        + _accounts(
            "personal: taking tasks, 3 of 5 slots in use", "work: taking tasks, 1 of 3 slots in use"
        )
        + "- **How many can run:** up to 4 at once (the queue-wide cap; the accounts' own "
        "limits add up to 8: personal 5 + work 3). Right now 4 running, with no room for more.\n"
        "- **Tasks:** 4 running; 6 can start as soon as a slot is free; 1 finished.\n"
        + NO_QUESTIONS
        + _attention(
            "The queue-wide cap of 4 ([concurrency].max_concurrency in claude_runner.toml) is "
            "lower than what the accounts allow together (8), so at most 4 tasks run at once. "
            "Raise or remove it to use every account slot."
        )
    )


def test_paused_account_with_a_task_still_running(
    queue: Path, tmp_path: Path, bin_dir: Path, supervisor: Callable[..., int]
) -> None:
    """A paused account's slots are not free: account list allows it 0."""
    _two_accounts(queue, tmp_path, running={"personal": 2, "work": 1})
    markers = queue / ".claude_task_runner" / "account_paused"
    markers.mkdir()
    (markers / "work").touch()
    _tasks(queue, new=4)
    supervisor()

    assert _summary(queue, bin_dir) == (
        RUNNING
        + _accounts("personal: taking tasks, 2 of 5 slots in use", "work: paused; 1 still running")
        + f"- **How many can run:** {EIGHT} Right now 3 running, with room for 3 more.\n"
        "- **Tasks:** 3 running; 4 can start as soon as a slot is free; 0 finished.\n"
        + NO_QUESTIONS
        + _attention(
            "work is paused, so it starts no new tasks. 1 task it started is still running. "
            f"Resume it with `claude-task-runner account resume work --queue {queue}`."
        )
    )


def test_weekly_throttle_resumes_when_use_falls_back_under_plan(
    queue: Path, tmp_path: Path, bin_dir: Path, supervisor: Callable[..., int]
) -> None:
    """The resume time is the weekly curve's catch-up from `account list`,
    not the supervisor's next re-check (scheduled_wakeup_at, much sooner).
    On the default curve 30% used is met 104 hours before the reset."""
    _two_accounts(
        queue,
        tmp_path,
        personal=_state(
            SupervisorState.THROTTLED_WEEKLY,
            last_weekly_util_pct=30,
            last_weekly_reset_at=WEEKLY_RESET,
            target_concurrency=0,
            scheduled_wakeup_at=SINCE + timedelta(hours=5),
        ),
        running={"work": 3},
    )
    _tasks(queue, new=2)
    supervisor()

    assert _summary(queue, bin_dir) == (
        RUNNING
        + _accounts(
            "personal: not taking new tasks, because it is using its weekly allowance faster "
            "than planned (30% used)",
            "work: taking tasks, 3 of 3 slots in use",
        )
        + f"- **How many can run:** {EIGHT} Right now 3 running, with no room for more.\n"
        "- **Tasks:** 3 running; 2 can start as soon as a slot is free; 0 finished.\n"
        + NO_QUESTIONS
        + _info(
            f"personal starts again about {_when(WEEKLY_RESET - timedelta(hours=104))}, when its "
            "weekly use falls back under plan. Nothing to do."
        )
    )


def test_weekly_throttle_without_a_reset_time(
    queue: Path, tmp_path: Path, bin_dir: Path, supervisor: Callable[..., int]
) -> None:
    """With no weekly reset time to anchor the curve, no resume time is
    given; the re-check time is named as such."""
    recheck = SINCE + timedelta(hours=5)
    _two_accounts(
        queue,
        tmp_path,
        personal=_state(
            SupervisorState.THROTTLED_WEEKLY,
            last_weekly_util_pct=30,
            target_concurrency=0,
            scheduled_wakeup_at=recheck,
        ),
        running={"work": 3},
    )
    supervisor()
    assert _summary(queue, bin_dir).endswith(
        _info(
            "personal starts again once its weekly use falls back under plan; the supervisor "
            f"re-checks at {_when(recheck)}. Nothing to do."
        )
    )


def test_five_hour_throttle_and_slowing_down(
    queue: Path, tmp_path: Path, bin_dir: Path, supervisor: Callable[..., int]
) -> None:
    wakeup = datetime(2026, 10, 16, 19, 5, tzinfo=UTC)
    _two_accounts(
        queue,
        tmp_path,
        personal=_state(
            SupervisorState.THROTTLED_5H,
            last_5h_util_pct=97,
            target_concurrency=0,
            scheduled_wakeup_at=wakeup,
        ),
        work=_state(SupervisorState.SLOWING_DOWN, last_5h_util_pct=70, target_concurrency=2),
        running={"work": 1},
    )
    _tasks(queue, new=2)
    supervisor()

    assert _summary(queue, bin_dir) == (
        RUNNING
        + _accounts(
            "personal: not taking new tasks, because its 5-hour allowance is used up (97% used)",
            "work: taking fewer tasks to save its 5-hour allowance, 1 of 2 slots in use "
            "(normally 3)",
        )
        + f"- **How many can run:** {EIGHT} Right now 1 running, with room for 1 more.\n"
        "- **Tasks:** 1 running; 2 can start as soon as a slot is free; 0 finished.\n"
        + NO_QUESTIONS
        + _info(
            f"personal starts again about {_when(wakeup)}, when its 5-hour usage window resets. "
            "Nothing to do."
        )
    )


def test_no_reading_and_both_kinds_of_usage_error(
    queue: Path, tmp_path: Path, bin_dir: Path, supervisor: Callable[..., int]
) -> None:
    """An expired login and a changed usage page both leave the account in
    error_drift; the message and the fix differ."""
    dirs = _config(queue, tmp_path, {"personal": 5, "work": 3, "spare": 1})
    _supervisor_json(
        queue,
        {
            "personal": _state(SupervisorState.NO_READING),
            "work": _state(
                SupervisorState.ERROR_DRIFT, last_drift_message=f"{AUTH_FAILURE_PREFIX}HTTP 401"
            ),
            "spare": _state(
                SupervisorState.ERROR_DRIFT, last_drift_message="5-hour usage block not found"
            ),
        },
    )
    _tasks(queue, new=3)
    supervisor()

    assert _summary(queue, bin_dir) == (
        RUNNING
        + _accounts(
            "personal: not taking tasks, because it has no recent usage reading",
            "work: not taking tasks, because its usage could not be read",
            "spare: not taking tasks, because its usage could not be read",
        )
        + "- **How many can run:** up to 9 at once (personal 5 + work 3 + spare 1; no "
        "queue-wide cap). Right now 0 running, with no room for more.\n"
        "- **Tasks:** 3 can start as soon as a slot is free; 0 finished.\n"
        + NO_QUESTIONS
        + _attention(
            "personal has no recent usage reading (it has never had one), so it takes no tasks. "
            "This clears by itself within a few minutes of a supervisor start; if it lasts, see "
            'the runbook section "An account stays in `no_reading`".',
            "work takes no tasks because its login was rejected when it read its usage (OAuth "
            f"bearer rejected: HTTP 401). Log it in again: `CLAUDE_CONFIG_DIR={dirs['work']} "
            "claude /login`, or renew its long-lived token with `claude setup-token`.",
            "spare takes no tasks because its usage page could not be read (5-hour usage block "
            "not found); the page's format may have changed. Run /runner-usage to check the "
            "usage reader.",
        )
    )


def test_account_allowed_none_and_one_above_its_limit(
    queue: Path, tmp_path: Path, bin_dir: Path, supervisor: Callable[..., int]
) -> None:
    _two_accounts(
        queue,
        tmp_path,
        personal=_state(SupervisorState.IDLE, target_concurrency=0),
        work=_state(SupervisorState.SLOWING_DOWN, target_concurrency=1),
        running={"work": 3},
    )
    _tasks(queue, new=1)
    supervisor()

    assert _summary(queue, bin_dir) == (
        RUNNING
        + _accounts(
            "personal: not taking new tasks, because its last usage reading allows none",
            "work: 3 running, above its current limit of 1; it starts no new tasks until fewer run",
        )
        + f"- **How many can run:** {EIGHT} Right now 3 running, with no room for more.\n"
        "- **Tasks:** 3 running; 1 can start as soon as a slot is free; 0 finished.\n"
        + NO_QUESTIONS
        + _info(
            "personal's last usage reading allows it no tasks; it takes tasks again at a later "
            "reading. Nothing to do.",
            "work has 3 running, above its current limit of 1, so it starts no new tasks until "
            "fewer run. Nothing to do.",
        )
    )


def test_tasks_that_need_the_operator(
    queue: Path, tmp_path: Path, bin_dir: Path, supervisor: Callable[..., int]
) -> None:
    """Also a drained queue: nothing can start, which is worth knowing."""
    _two_accounts(queue, tmp_path)
    add_task(queue, "effort", model="claude-sonnet-4-6", effort="max")
    for task_id, status in (("hung", "possibly_hung"), ("broken", "failed_circuit_breaker")):
        add_task(queue, task_id)
        add_state(queue, task_id, status)
    add_task(queue, "corrupt")
    state_path_for(queue, "corrupt").write_text("not yaml: ][[", encoding="utf-8")
    supervisor()

    assert _summary(queue, bin_dir) == (
        RUNNING
        + _accounts(
            "personal: taking tasks, 0 of 5 slots in use", "work: taking tasks, 0 of 3 slots in use"
        )
        + f"- **How many can run:** {EIGHT} Right now 0 running, with room for 8 more.\n"
        "- **Tasks:** 1 is set to a model or effort the queue does not allow; 1 looks stuck; 1 "
        "stopped after repeated failures; 1 has a state file that cannot be read; 0 finished.\n"
        + NO_QUESTIONS
        + _attention(
            "Tasks set to a model or effort the queue does not allow: 1. Fix each task's model or "
            "effort, or add the pair to [effort_levels] and send the supervisor SIGHUP.",
            "Tasks that have gone quiet while running and may be stuck: 1. See the runbook "
            'section "Task `possibly_hung` for hours".',
            "Tasks that stopped after failing repeatedly and will not be retried: 1. Look with "
            f"`claude-task-runner queue states --status failed_circuit_breaker --queue {queue}`.",
            "Tasks held because their state file cannot be read: 1. `claude-task-runner doctor "
            f"--queue {queue}` lists the files.",
        )
        + _info("No task can start right now: every unfinished task is held, as Tasks shows.")
    )


def test_nothing_running_although_tasks_can_start(
    queue: Path, tmp_path: Path, bin_dir: Path, supervisor: Callable[..., int]
) -> None:
    _two_accounts(queue, tmp_path)
    _tasks(queue, new=2)
    supervisor()

    assert _summary(queue, bin_dir) == (
        RUNNING
        + _accounts(
            "personal: taking tasks, 0 of 5 slots in use", "work: taking tasks, 0 of 3 slots in use"
        )
        + f"- **How many can run:** {EIGHT} Right now 0 running, with room for 8 more.\n"
        "- **Tasks:** 2 can start as soon as a slot is free; 0 finished.\n"
        + NO_QUESTIONS
        + _attention(
            "Nothing is running although 2 tasks can start and there is room for 8. If this "
            "lasts beyond the supervisor's next check, look in its log for `dispatch skipped "
            "task` lines."
        )
    )


def test_notice_and_unreadable_sidecar_request(
    queue: Path, tmp_path: Path, bin_dir: Path, supervisor: Callable[..., int]
) -> None:
    """A request with no questions is a notice to acknowledge; one that cannot
    be read needs repair. Neither is a question for you."""
    _two_accounts(queue, tmp_path, running={"personal": 1})
    for task_id in ("notice", "garbled"):
        add_task(queue, task_id)
        add_state(queue, task_id, "awaiting_sidecar")
    write_request(queue, make_request("notice", question_ids=()))
    (sidecar_dir_for(queue, "garbled") / "request-001.json").write_text("{not json")
    supervisor()

    assert _summary(queue, bin_dir) == (
        RUNNING
        + _accounts(
            "personal: taking tasks, 1 of 5 slots in use", "work: taking tasks, 0 of 3 slots in use"
        )
        + f"- **How many can run:** {EIGHT} Right now 1 running, with room for 7 more.\n"
        "- **Tasks:** 1 running; 2 wait for your answer; 0 finished.\n"
        "- **Questions for you:** none. Also 1 notice to acknowledge, asking nothing "
        "(/runner-answer-sidecar).\n"
        + _attention(
            "1 sidecar request cannot be read (garbled request 1), so what it asks is unknown and "
            "the task waits for nothing anyone can answer. Repair it; `claude-task-runner sidecar "
            f"list --queue {queue}` shows the error."
        )
    )


@pytest.mark.parametrize("pid", ["missing", "stale"])
def test_supervisor_not_running(queue: Path, tmp_path: Path, bin_dir: Path, pid: str) -> None:
    """The account states are its last record, so no usage item is raised on
    the strength of them, and nothing is said about room now."""
    _two_accounts(
        queue,
        tmp_path,
        personal=_state(SupervisorState.THROTTLED_5H, last_5h_util_pct=97, target_concurrency=0),
    )
    _tasks(queue, new=5)
    if pid == "stale":
        (queue / ".claude_task_runner" / "supervisor.pid").write_text("999999999\n")

    assert _summary(queue, bin_dir) == (
        "- **Supervisor:** not running.\n"
        + _accounts(
            "personal: not taking new tasks, because its 5-hour allowance is used up (97% used)",
            "work: taking tasks, 0 of 3 slots in use",
            heading="Accounts (as of the supervisor's last record)",
        )
        + f"- **How many can run:** {EIGHT}\n"
        "- **Tasks:** 5 can start as soon as a slot is free; 0 finished.\n"
        + NO_QUESTIONS
        + _attention(
            "The supervisor is not running, so no new tasks start and finished tasks are not "
            f"recorded. Start it with `claude-task-runner supervisor start --queue {queue}`; a "
            "watchdog, if installed, restarts it within a few minutes."
        )
    )


def test_supervisor_that_stopped_saving_its_state(
    queue: Path, tmp_path: Path, bin_dir: Path, supervisor: Callable[..., int]
) -> None:
    _config(queue, tmp_path, {"personal": 5, "work": 3})
    states = {
        "personal": _state(SupervisorState.DISPATCHING),
        "work": _state(SupervisorState.DISPATCHING),
    }
    _supervisor_json(queue, states, {"personal": 1}, saved_ago=timedelta(minutes=30, seconds=10))
    supervisor()

    assert _summary(queue, bin_dir) == (
        "- **Supervisor:** its process is running, but it last saved its state 30 minutes ago.\n"
        + _accounts(
            "personal: taking tasks, 1 of 5 slots in use",
            "work: taking tasks, 0 of 3 slots in use",
            heading="Accounts (as of the supervisor's last record)",
        )
        + f"- **How many can run:** {EIGHT}\n"
        "- **Tasks:** 1 running; 0 finished.\n"
        + NO_QUESTIONS
        + _attention(
            "The supervisor has not saved its state for 30 minutes (it normally does every 60 "
            "seconds), so it may be stuck. Look at its log; if it stays this way, restart it."
        )
    )


def test_poll_interval_is_the_supervisors_own(
    queue: Path, tmp_path: Path, bin_dir: Path, supervisor: Callable[..., int]
) -> None:
    """Started with another --config whose poll interval is 600 s, a
    supervisor that saved 25 minutes ago is on time: within three polls. Read
    with the queue's own file (60 s) it would look stuck."""
    dirs = _config(queue, tmp_path, {"personal": 5, "work": 3})
    other = tmp_path / "other.toml"
    # max_reading_age_s must cover two capture cycles of the accounts.
    usage = "[usage]\npoll_interval_s = 600\nmax_reading_age_s = 3600\n"
    other.write_text(_config_text(dirs, usage), encoding="utf-8")
    states = {
        "personal": _state(SupervisorState.DISPATCHING),
        "work": _state(SupervisorState.DISPATCHING),
    }
    _supervisor_json(queue, states, {"personal": 1}, saved_ago=timedelta(minutes=25))
    supervisor(config=other)

    summary = _summary(queue, bin_dir)
    assert summary.startswith(RUNNING), summary
    assert "may be stuck" not in summary


def test_two_polls_late_is_not_stuck(
    queue: Path, tmp_path: Path, bin_dir: Path, supervisor: Callable[..., int]
) -> None:
    """A tick can run past its interval while it captures usage."""
    _config(queue, tmp_path, {"personal": 5, "work": 3})
    states = {
        "personal": _state(SupervisorState.DISPATCHING),
        "work": _state(SupervisorState.DISPATCHING),
    }
    _supervisor_json(queue, states, {"personal": 1}, saved_ago=timedelta(seconds=150))
    supervisor()
    assert _summary(queue, bin_dir).startswith(RUNNING)


@pytest.mark.parametrize(
    ("initial", "capacity"),
    [
        pytest.param(
            2,
            f"{EIGHT} Until the first task finishes, at most 2 run at once (warm-up). Right now 0 "
            "running, with room for 2 more.",
            id="below-the-ceiling",
        ),
        pytest.param(9, f"{EIGHT} Right now 0 running, with room for 8 more.", id="not-below"),
    ],
)
def test_warm_up_cap(
    queue: Path,
    tmp_path: Path,
    bin_dir: Path,
    supervisor: Callable[..., int],
    initial: int,
    capacity: str,
) -> None:
    _two_accounts(queue, tmp_path, extra=f"[concurrency]\ninitial_concurrency = {initial}\n")
    _tasks(queue, new=1)
    supervisor()
    lines = _summary(queue, bin_dir).splitlines()
    assert lines[4] == f"- **How many can run:** {capacity}"


def test_every_listing_failing(queue: Path, bin_dir: Path) -> None:
    """With nothing from the CLI the summary says what is unknown and how to
    see why; the exit code still means only "sidecars unknown"."""
    failing = ("", CRASH, 3)
    _cli_with(bin_dir, account=failing, queue=failing, sidecar=failing)

    assert _summary(queue, bin_dir, rc=1) == (
        "- **Supervisor:** not running.\n"
        "- **Accounts:** unknown.\n"
        "- **Tasks:** unknown.\n"
        "- **Questions for you:** unknown.\n"
        + _attention(
            "The supervisor is not running, so no new tasks start and finished tasks are not "
            f"recorded. Start it with `claude-task-runner supervisor start --queue {queue}`; a "
            "watchdog, if installed, restarts it within a few minutes.",
            "The account list could not be read (`claude-task-runner account list --json` exited "
            "3: RuntimeError: boom), so what each account is doing and how many tasks may run at "
            f"once are unknown. Run `claude-task-runner account list --queue {queue}` to see the "
            "error.",
            "What holds the tasks could not be read (`claude-task-runner queue holds --json` "
            f"exited 3: RuntimeError: boom). Run `claude-task-runner queue holds --queue {queue}` "
            "to see the error.",
            "The open questions could not be listed (`claude-task-runner sidecar list --json` "
            "exited 3: RuntimeError: boom), so whether any are waiting for you is unknown.",
        )
    )


def test_a_failing_section_does_not_stop_the_report(queue: Path, bin_dir: Path) -> None:
    """A malformed account row breaks the summary; the details still print
    and the exit code is unchanged."""
    row = json.dumps({"accounts": [{"state": "dispatching"}], "queue_ceiling": {}})
    _cli_with(bin_dir, account=(row, "", 0))
    proc = _run(queue, bin_dir)
    assert (proc.returncode, proc.stderr) == (0, "")
    assert re.search(
        r"\n\*\*In plain words\*\*: unavailable: KeyError: 'name' \(report\.py line \d+\)\n",
        proc.stdout,
    ), proc.stdout
    for section in ("### Details", "**Queue counts**", "**Last idle report**", "**Open sidecars**"):
        assert section in proc.stdout


def test_cli_calls(queue: Path, bin_dir: Path) -> None:
    log = real_cli(bin_dir)
    proc = _run(queue, bin_dir)
    assert proc.returncode == 0, proc.stderr
    holds = f"queue\nholds\n--queue\n{queue}\n--json\n--\n"
    assert calls_in(log) == sorted([account_list_call(queue), holds, sidecar_list_call(queue)])


def test_cli_calls_carry_the_supervisors_config(
    queue: Path, tmp_path: Path, bin_dir: Path, supervisor: Callable[..., int]
) -> None:
    log = real_cli(bin_dir)
    other = tmp_path / "other.toml"
    other.write_text("", encoding="utf-8")
    supervisor(config=other)
    _run(queue, bin_dir)
    assert calls_in(log) == sorted(
        [
            f"account\nlist\n--queue\n{queue}\n--config\n{other}\n--json\n--\n",
            f"queue\nholds\n--queue\n{queue}\n--config\n{other}\n--json\n--\n",
            sidecar_list_call(queue),
        ]
    )


# ---------------------------------------------------------------------------
# The idle report in the details
# ---------------------------------------------------------------------------


def _idle_report(queue: Path, bin_dir: Path) -> str:
    proc = _run(queue, bin_dir)
    _, _, rest = proc.stdout.partition("**Last idle report**\n\n")
    return rest.split("\n\n")[1]


def test_newest_idle_line_wins_journal_or_log(
    queue: Path, tmp_path: Path, bin_dir: Path, supervisor: Callable[..., int]
) -> None:
    supervisor()
    now = datetime.now(UTC)
    stub_journalctl(bin_dir, _idle_line(now, "no eligible tasks: 1 unfinished held (depends_on 1)"))
    (queue / ".claude_task_runner" / "supervisor.log").write_text(
        _idle_line(now + timedelta(seconds=1), "eligible tasks available again: 3") + "\n"
    )
    assert _idle_report(queue, bin_dir) == (
        f"{_when(now + timedelta(seconds=1))} (supervisor.log): `eligible tasks available again: "
        "3`: tasks were ready to start then"
    )

    stub_journalctl(
        bin_dir,
        _idle_line(
            now + timedelta(seconds=2), "no eligible tasks: 1 unfinished held (depends_on 1)"
        ),
    )
    assert _idle_report(queue, bin_dir) == (
        f"{_when(now + timedelta(seconds=2))} (the journal): `no eligible tasks: 1 unfinished "
        "held (depends_on 1)`"
    )


def test_idle_line_from_before_the_supervisor_started(
    queue: Path, tmp_path: Path, bin_dir: Path, supervisor: Callable[..., int]
) -> None:
    """A cron supervisor's log keeps earlier runs' lines; one older than the
    running process says nothing about now."""
    old = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    (queue / ".claude_task_runner" / "supervisor.log").write_text(
        _idle_line(old, "no eligible tasks: 1 unfinished held (depends_on 1)") + "\n"
    )
    supervisor()
    assert _idle_report(queue, bin_dir) == (
        "none since the supervisor started; the last one, from before it started, is "
        f"{_when(old)} (supervisor.log): `no eligible tasks: 1 unfinished held (depends_on 1)`"
    )


def test_journal_with_entries_but_no_idle_line(
    queue: Path, bin_dir: Path, supervisor: Callable[..., int]
) -> None:
    supervisor()
    stub_journalctl(bin_dir, "2026-10-16T10:00:00.000000Z [info     ] dispatched task t1")
    assert _idle_report(queue, bin_dir) == "none since the supervisor started"


def test_no_log_to_read_raises_nothing(
    queue: Path, tmp_path: Path, bin_dir: Path, supervisor: Callable[..., int]
) -> None:
    """A supervisor started by hand logs to its terminal: the report says so,
    and the summary raises nothing about it."""
    _two_accounts(queue, tmp_path, running={"personal": 1})
    supervisor()
    exe = bin_dir / "journalctl"
    exe.write_text("#!/bin/sh\nexit 1\n")
    exe.chmod(0o755)
    assert _idle_report(queue, bin_dir) == (
        "not available: the supervisor's log is not in the journal for its PID and there is no "
        "supervisor.log in the queue (a supervisor started by hand logs to its terminal)"
    )
    assert _summary(queue, bin_dir).endswith(NORMAL)


def test_large_log_is_read_from_its_end(tmp_path: Path) -> None:
    """Only the tail is read: a 70 MB log is searched quickly, a line that
    straddles a block boundary is found whole, and a line beyond the window
    is not looked for."""
    report = _report_module()
    log = tmp_path / "supervisor.log"
    filler = b"x" * 120 + b"\n"
    old = _idle_line(datetime(2026, 1, 1, tzinfo=UTC), "eligible tasks available again: 1")
    new = _idle_line(datetime(2026, 2, 1, tzinfo=UTC), "eligible tasks available again: 2")
    with open(log, "wb") as fh:
        fh.write(old.encode() + b"\n")
        fh.write(filler * (70 * 1024 * 1024 // len(filler)))
        fh.write(new.encode() + b"\n")
        # Puts the new line across the boundary 1 MiB back from the end.
        fh.write(b"y" * (1024 * 1024 - 40) + b"\n")
    started = time.monotonic()
    found = report.newest_idle_line_in(log)
    assert time.monotonic() - started < 2.0
    assert found is not None and found.message == "eligible tasks available again: 2"
    assert report.newest_idle_line_in(log, max_bytes=10) is None

    log.write_bytes(old.encode() + b"\n" + filler * 100)
    assert report.newest_idle_line_in(log).message == "eligible tasks available again: 1"
    assert report.newest_idle_line_in(log, max_bytes=len(filler) * 50) is None


# ---------------------------------------------------------------------------
# Gates: report.py's words keep up with the runner
# ---------------------------------------------------------------------------


def test_every_held_category_has_plain_words() -> None:
    """``queue holds`` reports the selector's categories; report.py keeps
    words for them. One without would print as "held (<name>)"."""
    report = _report_module()
    categories = {
        value
        for name, value in vars(orchestrator_mod).items()
        if name.startswith("_SKIP_") and name != "_SKIP_READINESS_PREFIX"
    }
    # Statuses the selector reports under their own name: never pending or
    # failed, which it dispatches; awaiting_sidecar and deferred have their
    # own categories; completed is counted apart.
    categories |= set(get_args(TaskStatus)) - {
        "pending",
        "failed",
        "awaiting_sidecar",
        "deferred",
        "completed",
    }
    assert sorted(categories - set(report._HELD_WORDS)) == []

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


def test_every_account_state_has_its_own_words() -> None:
    report = _report_module()
    for state in SupervisorState:
        text, _ = report._account_line(
            {"name": "a", "state": state.value, "dispatch_cap": 1, "max_concurrency": 1}
        )
        assert "(state " not in text, (state, text)


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_idle_line_is_read_as_the_supervisor_writes_it(
    fmt: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The line comes from the orchestrator through the supervisor's log
    format, text or JSON; report.py must find it, message intact."""
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    _reset_logging_for_tests()
    try:
        configure_logging(level="INFO", fmt=fmt)  # type: ignore[arg-type]
        orchestrator_mod.IdleDispatchLog().observe(
            0, Counter({"block_dispatch": 2, "readiness_missing_file": 1, "completed": 9})
        )
    finally:
        _reset_logging_for_tests()
    report = _report_module()
    (raw,) = [line for line in buf.getvalue().splitlines() if "eligible tasks" in line]
    line = report.parse_idle_line(raw, "the journal")
    assert line is not None, raw
    assert line.message == (
        "no eligible tasks: 3 unfinished held (block_dispatch 2, readiness_missing_file 1)"
    )
    assert abs((datetime.now(UTC) - line.at).total_seconds()) < 60
