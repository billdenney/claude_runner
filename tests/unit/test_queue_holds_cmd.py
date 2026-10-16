"""``claude-task-runner queue holds``: the selector's view, read-only.

The command runs the supervisor's own candidate selection
(``orchestrator.preview_selection``) and reports which tasks would start
and what holds the rest. These tests pin its JSON on a queue with one task
per hold category, the in-flight tasks it takes from supervisor.json, which
eligible tasks an account can take now and which wait for the account their
session runs on, the warm-up cap, its text output, and that it calls no
state writer and leaves every state file as it was.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_task_runner.queue.store import queue_runtime_dir, state_path_for, todo_dir
from claude_task_runner.supervisor import persistence as persist_mod
from claude_task_runner.supervisor.states import (
    AccountState,
    InFlightRecord,
    SupervisorSnapshot,
    SupervisorState,
)

from ._concurrency_fixtures import invoke_cli, write_account_cap
from ._mixed_queue import (
    BLOCK_FILE,
    STALE_HOLDS,
    add_state,
    add_task,
    build_mixed_queue,
    forbid_selector_writes,
)

SINCE = datetime(2026, 10, 16, tzinfo=UTC)

HELD = {
    "block_dispatch": 1,
    "deferred_cooldown": 1,
    "depends_on": 1,
    "failed_circuit_breaker": 1,
    "in_flight": 1,
    "invalid_effort": 1,
    "possibly_hung": 1,
    "readiness_awaiting_sidecar_response": 1,
    "readiness_awaiting_sidecar_response+missing_file": 1,
    "readiness_missing_file": 1,
    "running": 1,
    "unanswered_sidecar": 1,
    "unparseable_state": 1,
    "unparseable_task": 1,
    "weekly_paused": 1,
}


@pytest.fixture
def queue(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


def _config(queue: Path, extra: str = "") -> None:
    (queue / "claude_runner.toml").write_text(
        f'[dispatch]\ndispatch_block_file = "{BLOCK_FILE}"\n{extra}', encoding="utf-8"
    )


def _in_flight(queue: Path, *task_ids: str) -> None:
    records = [InFlightRecord(task_id=t, account="default", started_at=SINCE) for t in task_ids]
    snapshot = SupervisorSnapshot(
        state=SupervisorState.DISPATCHING,
        since=SINCE,
        in_flight=records,
        in_flight_task_ids=list(task_ids),
    )
    persist_mod.write_atomic(snapshot, persist_mod.supervisor_state_path(queue))


def _holds(queue: Path) -> dict[str, object]:
    result = invoke_cli(["queue", "holds", "--queue", str(queue), "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _states(queue: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in state_path_for(queue, "x").parent.glob("*.yaml")}


def test_every_task_lands_in_one_place(queue: Path, tmp_path: Path) -> None:
    """supervisor.json records no account state, so no account can take a
    task yet (a cold start): every eligible task waits for a slot."""
    build_mixed_queue(queue, tmp_path / "absent.md")
    _config(queue)
    _in_flight(queue, "inflight")

    assert _holds(queue) == {
        "eligible": {
            "count": 5,
            "task_ids": [
                "cooldown-over",
                "effort-fixed",
                "file-arrived",
                "ready",
                "sidecar-answered",
            ],
            "by_status": {"awaiting_sidecar": 1, "deferred": 3, "new": 1},
            "can_start_now": 0,
            "waiting_for_their_account": 0,
        },
        "held": HELD,
        "completed": 2,
        "todo_tasks": 22,
        "poll_interval_s": 60.0,
        "affinity_ttl_s": 5400.0,
        "queue_cap": None,
        "warming_up": False,
        "parked_workers": [],
        "retrying": [
            {
                "task_id": "cooldown",
                "deferred_reason": "pre-dispatch hook deferred (exit 1): waiting",
                "next_eligible_at": "2027-10-14T12:00:00+00:00",
            }
        ],
        "supervisor_json_error": None,
    }


def test_writes_no_state_file(queue: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A supervisor tick would park needs-file and bad-effort as deferred
    and un-park the two stale holds; the command calls no state writer and
    leaves every state file as it was."""
    build_mixed_queue(queue, tmp_path / "absent.md")
    _config(queue)
    _in_flight(queue, "inflight")
    before = _states(queue)
    writes = forbid_selector_writes(monkeypatch)
    _holds(queue)
    assert writes == []
    assert _states(queue) == before
    for task_id in STALE_HOLDS:
        assert b"status: deferred" in before[f"{task_id}.yaml"]


def _two_accounts(queue: Path, tmp_path: Path) -> None:
    """personal (2 slots) has used up its 5-hour allowance; work (2 slots)
    takes tasks."""
    dirs = {name: tmp_path / f"cfg-{name}" for name in ("personal", "work")}
    for config_dir in dirs.values():
        write_account_cap(config_dir, 2)
    accounts = "".join(f'[[accounts]]\nname = "{n}"\nconfig_dir = "{d}"\n' for n, d in dirs.items())
    _config(queue, accounts)
    snapshot = SupervisorSnapshot(
        state=SupervisorState.DISPATCHING,
        since=SINCE,
        accounts={
            "personal": AccountState(state=SupervisorState.THROTTLED_5H, since=SINCE),
            "work": AccountState(state=SupervisorState.DISPATCHING, since=SINCE),
        },
    )
    persist_mod.write_atomic(snapshot, persist_mod.supervisor_state_path(queue))


def _session_on(queue: Path, task_id: str, account: str, idle: timedelta) -> None:
    add_task(queue, task_id)
    add_state(
        queue,
        task_id,
        "pending",
        session_id=f"session-{task_id}",
        session_account=account,
        last_finished_at=datetime.now(UTC) - idle,
    )


def test_session_affinity(queue: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A task whose session runs on the throttled account waits for it,
    until it has gone affinity_ttl_s without running; then any account may
    take it. The supervisor clears such a session as it dispatches; the
    command only reports where the task would go."""
    _two_accounts(queue, tmp_path)
    add_task(queue, "fresh")
    _session_on(queue, "affined-recent", "personal", timedelta(minutes=10))
    _session_on(queue, "affined-expired", "personal", timedelta(days=2))
    before = _states(queue)
    writes = forbid_selector_writes(monkeypatch)

    holds = _holds(queue)
    assert holds["eligible"] == {
        "count": 3,
        "task_ids": ["affined-expired", "affined-recent", "fresh"],
        "by_status": {"new": 1, "pending": 2},
        "can_start_now": 2,
        "waiting_for_their_account": 1,
    }
    assert writes == []
    assert _states(queue) == before


def test_text_output(queue: Path, tmp_path: Path) -> None:
    _two_accounts(queue, tmp_path)
    add_task(queue, "fresh")
    _session_on(queue, "affined-recent", "personal", timedelta(minutes=10))
    add_task(queue, "later", depends_on=["fresh"])

    result = invoke_cli(["queue", "holds", "--queue", str(queue)])
    assert result.exit_code == 0, result.output
    assert result.stdout == (
        "eligible: 2 (1 can start now, 1 waiting for the account their session runs on)\n"
        "  affined-recent (pending; no account now)\n"
        "  fresh (new; work)\n"
        "held, depends_on: 1\n"
        "completed: 0\n"
        "queue-wide cap: none\n"
        "selection runs every: 60 s\n"
    )


def test_without_supervisor_json_nothing_is_in_flight(queue: Path) -> None:
    add_task(queue, "a")
    add_task(queue, "b")
    add_state(queue, "b", "running")
    _config(queue)

    holds = _holds(queue)
    assert (holds["eligible"], holds["held"], holds["supervisor_json_error"]) == (
        {
            "count": 1,
            "task_ids": ["a"],
            "by_status": {"new": 1},
            "can_start_now": 0,
            "waiting_for_their_account": 0,
        },
        {"running": 1},
        None,
    )


def test_unreadable_supervisor_json_is_reported(queue: Path) -> None:
    add_task(queue, "a")
    _config(queue)
    path = persist_mod.supervisor_state_path(queue)
    path.write_text("{not json", encoding="utf-8")

    holds = _holds(queue)
    assert holds["eligible"] == {
        "count": 1,
        "task_ids": ["a"],
        "by_status": {"new": 1},
        "can_start_now": 0,
        "waiting_for_their_account": 0,
    }
    assert holds["supervisor_json_error"] == (
        f"{path}: invalid JSON: Expecting property name enclosed in double quotes: "
        "line 1 column 2 (char 1)"
    )


def test_warm_up_cap_until_a_task_completes(queue: Path) -> None:
    add_task(queue, "a")
    _config(queue, "[concurrency]\ninitial_concurrency = 2\nmax_concurrency = 6\n")
    assert (_holds(queue)["queue_cap"], _holds(queue)["warming_up"]) == (2, True)

    add_task(queue, "b")
    add_state(queue, "b", "completed")
    assert (_holds(queue)["queue_cap"], _holds(queue)["warming_up"]) == (6, False)


def test_poll_interval_comes_from_the_settings(queue: Path) -> None:
    _config(queue, "[usage]\npoll_interval_s = 300\n")
    assert _holds(queue)["poll_interval_s"] == 300.0


@pytest.fixture
def worker(tmp_path: Path) -> Iterator[Callable[[Path], int]]:
    """Start a stand-in worker writing to ``log``, as the dispatcher's
    workers write to their attempt's log. Killed at teardown."""
    children: list[subprocess.Popen[bytes]] = []

    def start(log: Path) -> int:
        with log.open("wb") as out:
            child = subprocess.Popen(["sleep", "600"], stdout=out, start_new_session=True)
        children.append(child)
        return child.pid

    yield start
    for child in children:
        child.kill()
        child.wait(timeout=10)


def _exited_pid() -> int:
    child = subprocess.Popen(["true"])
    child.wait(timeout=10)
    return child.pid


def _parked(queue: Path, task_id: str, pid: int, log: Path | None, started: datetime) -> None:
    add_state(
        queue,
        task_id,
        "possibly_hung",
        stop_reason="worker_still_running",
        pid=pid,
        log_path=None if log is None else str(log),
        last_started_at=started,
        error=f"worker pid={pid} is still running at startup; parked",
    )


def test_parked_workers_and_retry_reasons(
    queue: Path, tmp_path: Path, worker: Callable[[Path], int]
) -> None:
    """Of the possibly_hung tasks, those parked while an unwatched worker
    ran carry the worker: still running or not, and past the task's time
    limit or not (here a 5-minute override, and the 4-hour default). The
    deferred_cooldown tasks carry their reasons."""
    _config(queue)
    started = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10)
    logs = {name: tmp_path / f"{name}.log" for name in ("within", "past")}
    pids = {name: worker(log) for name, log in logs.items()}
    add_task(queue, "within")
    _parked(queue, "within", pids["within"], logs["within"], started)
    add_task(queue, "past", max_duration_s_override=300)
    _parked(queue, "past", pids["past"], logs["past"], started)
    add_task(queue, "exited")
    gone = _exited_pid()
    _parked(queue, "exited", gone, None, started)
    add_task(queue, "silent")
    add_state(queue, "silent", "possibly_hung", stop_reason="silent_on_restart")
    due = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=15)
    add_task(queue, "no-config-dir")
    add_state(
        queue,
        "no-config-dir",
        "deferred",
        stop_reason="worker_start_deferred",
        deferred_reason="cannot start the worker: config_dir /gone does not exist",
        next_eligible_at=due,
    )

    holds = _holds(queue)
    assert holds["held"] == {"deferred_cooldown": 1, "possibly_hung": 4}
    at = started.isoformat()
    assert holds["parked_workers"] == [
        {
            "task_id": "exited",
            "pid": gone,
            "started_at": at,
            "running": False,
            "duration_cap_s": 14400.0,
            "past_cap": False,
        },
        {
            "task_id": "past",
            "pid": pids["past"],
            "started_at": at,
            "running": True,
            "duration_cap_s": 300.0,
            "past_cap": True,
        },
        {
            "task_id": "within",
            "pid": pids["within"],
            "started_at": at,
            "running": True,
            "duration_cap_s": 14400.0,
            "past_cap": False,
        },
    ]
    assert holds["retrying"] == [
        {
            "task_id": "no-config-dir",
            "deferred_reason": "cannot start the worker: config_dir /gone does not exist",
            "next_eligible_at": due.isoformat(),
        }
    ]


def test_a_parked_worker_supervisor_json_lists_is_held_by_its_status(
    queue: Path, tmp_path: Path, worker: Callable[[Path], int]
) -> None:
    """A supervisor tick persists each parked live worker's record in
    supervisor.json. The task is held ``possibly_hung`` and reported among
    the parked workers, as the tick's selector holds it, not ``in_flight``;
    its record still takes its account's one slot, so the eligible task
    cannot start now."""
    config_dir = tmp_path / "cfg-solo"
    write_account_cap(config_dir, 1)
    _config(queue, f'[[accounts]]\nname = "solo"\nconfig_dir = "{config_dir}"\n')
    started = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10)
    log = tmp_path / "parked.log"
    pid = worker(log)
    add_task(queue, "parked")
    _parked(queue, "parked", pid, log, started)
    add_task(queue, "fresh")
    snapshot = SupervisorSnapshot(
        state=SupervisorState.DISPATCHING,
        since=SINCE,
        accounts={"solo": AccountState(state=SupervisorState.DISPATCHING, since=SINCE)},
        in_flight=[InFlightRecord(task_id="parked", account="solo", started_at=started)],
        in_flight_task_ids=["parked"],
    )
    persist_mod.write_atomic(snapshot, persist_mod.supervisor_state_path(queue))

    holds = _holds(queue)

    assert holds["held"] == {"possibly_hung": 1}
    assert holds["parked_workers"] == [
        {
            "task_id": "parked",
            "pid": pid,
            "started_at": started.isoformat(),
            "running": True,
            "duration_cap_s": 14400.0,
            "past_cap": False,
        }
    ]
    assert holds["eligible"] == {
        "count": 1,
        "task_ids": ["fresh"],
        "by_status": {"new": 1},
        "can_start_now": 0,
        "waiting_for_their_account": 0,
    }


def test_an_unreadable_in_flight_state_is_held_in_flight(queue: Path) -> None:
    """A supervisor.json record whose state file does not parse cannot be
    told apart from a monitored task: it stays held ``in_flight``."""
    _config(queue)
    add_task(queue, "a")
    state_path_for(queue, "a").write_text("{not: [yaml", encoding="utf-8")
    _in_flight(queue, "a")

    assert _holds(queue)["held"] == {"in_flight": 1}
