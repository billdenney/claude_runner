"""Behavioral tests for the runner-status snapshot.sh per-account block.

Exercises the bundled ``snapshot.sh`` against a fixture queue dir whose
``supervisor.json`` carries a v3 ``accounts`` map. This file pins the
per-account state section's columns + rows + escape behaviour explicitly
so a future schema change to ``accounts`` doesn't silently drift the table
format. ``test_snapshot_script.py`` pins the queue check, the whole report
for a queue whose supervisor has never run, and the open-sidecars section.
Nothing pins the supervisor liveness lines for a live or stale PID file,
or the supervisor.json fields table.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from claude_task_runner.cli.install_skills_cmd import _packaged_skill_dir


@pytest.fixture
def snapshot_script() -> Path:
    """Resolve the on-disk path to the bundled ``snapshot.sh``."""
    return _packaged_skill_dir("runner-status") / "snapshot.sh"


def _make_queue(tmp_path: Path) -> Path:
    """Create a queue directory, with the ``todo/`` that marks it as one."""
    queue = tmp_path / "q"
    (queue / "todo").mkdir(parents=True)
    return queue


def _seed_supervisor_json(queue: Path, payload: dict) -> Path:
    runtime = queue / ".claude_task_runner"
    runtime.mkdir(parents=True, exist_ok=True)
    sj = runtime / "supervisor.json"
    sj.write_text(json.dumps(payload), encoding="utf-8")
    return sj


def _run_snapshot(script: Path, queue: Path) -> str:
    """Run snapshot.sh against ``queue`` and return its stdout.

    stderr is folded in so a failed step shows up in test-failure output
    rather than being silently swallowed. We don't ``check=True``: the
    script exits 1 after the report when it cannot list the open sidecars,
    which depends on the ``claude-task-runner`` on PATH, and this file pins
    only the per-account table. ``test_snapshot_script.py`` pins the exit
    codes.
    """
    proc = subprocess.run(
        ["bash", str(script), "--queue", str(queue)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.stdout + ("\n[stderr]\n" + proc.stderr if proc.stderr else "")


def _v3_accounts_payload(
    *,
    accounts: dict[str, dict],
    in_flight: list[dict] | None = None,
) -> dict:
    """Build a minimal-but-valid v3 supervisor.json payload."""
    return {
        "schema_version": 3,
        "state": "dispatching",
        "since": "2026-06-09T11:00:00+00:00",
        "last_5h_util_pct": 25,
        "last_weekly_util_pct": 30,
        "in_flight_task_ids": [r["task_id"] for r in (in_flight or [])],
        "in_flight": in_flight or [],
        "accounts": accounts,
    }


# ---------------------------------------------------------------------------
# Happy path — multi-account
# ---------------------------------------------------------------------------


def test_per_account_table_renders_each_account(
    tmp_path: Path,
    snapshot_script: Path,
) -> None:
    queue = _make_queue(tmp_path)
    _seed_supervisor_json(
        queue,
        _v3_accounts_payload(
            accounts={
                "personal": {
                    "state": "dispatching",
                    "since": "2026-06-09T10:00:00+00:00",
                    "last_5h_util_pct": 15,
                    "last_weekly_util_pct": 20,
                    "last_5h_reset_at": "2026-06-09T15:00:00+00:00",
                    "last_weekly_reset_at": "2026-06-15T00:00:00+00:00",
                    "last_capture_at": "2026-06-09T11:55:00+00:00",
                    "paused": False,
                    "scheduled_wakeup_at": None,
                    "consecutive_clean_polls": 12,
                    "last_drift_message": "",
                },
                "work": {
                    "state": "throttled_5h",
                    "since": "2026-06-09T11:30:00+00:00",
                    "last_5h_util_pct": 88,
                    "last_weekly_util_pct": 45,
                    "last_5h_reset_at": "2026-06-09T16:00:00+00:00",
                    "last_weekly_reset_at": "2026-06-15T00:00:00+00:00",
                    "last_capture_at": "2026-06-09T11:55:30+00:00",
                    "paused": False,
                    "scheduled_wakeup_at": "2026-06-09T16:00:30+00:00",
                    "consecutive_clean_polls": 8,
                    "last_drift_message": "",
                },
            },
            in_flight=[
                {
                    "task_id": "t-001",
                    "account": "personal",
                    "started_at": "2026-06-09T11:00:00+00:00",
                },
                {
                    "task_id": "t-002",
                    "account": "personal",
                    "started_at": "2026-06-09T11:30:00+00:00",
                },
                {
                    "task_id": "t-003",
                    "account": "work",
                    "started_at": "2026-06-09T11:45:00+00:00",
                },
            ],
        ),
    )

    out = _run_snapshot(snapshot_script, queue)

    # Section header present.
    assert "**Per-account state**" in out
    # Both accounts rendered, in sorted order (personal before work).
    personal_pos = out.find("| personal | ")
    work_pos = out.find("| work | ")
    assert personal_pos != -1, out
    assert work_pos != -1, out
    assert personal_pos < work_pos, "accounts must render in sorted order"
    # Throttled state surfaced as the rendered state value for work.
    assert "`throttled_5h`" in out
    # Per-account in-flight counts derived from the `in_flight` list.
    # personal=2, work=1.
    assert "| personal | `dispatching` | 15% | 20% |  | 2 |" in out
    assert "| work | `throttled_5h` | 88% | 45% |  | 1 |" in out
    # Scheduled wakeup column carries through for the throttled account.
    assert "2026-06-09T16:00:30+00:00" in out


def test_target_column_shows_the_throttle_target(tmp_path: Path, snapshot_script: Path) -> None:
    """The column after in-flight is ``target_concurrency``: the ramp for
    a slowing-down account, "—" where no decision has set one (a v5 file,
    or an idle account)."""
    queue = _make_queue(tmp_path)
    payload = _v3_accounts_payload(
        accounts={
            "personal": {
                "state": "slowing_down",
                "since": "2026-06-09T10:00:00+00:00",
                "last_5h_util_pct": 55,
                "last_weekly_util_pct": 5,
                "target_concurrency": 2,
            },
            "work": {
                "state": "idle",
                "since": "2026-06-09T10:00:00+00:00",
                "last_5h_util_pct": 10,
                "last_weekly_util_pct": 5,
            },
        },
        in_flight=[
            {"task_id": "t-001", "account": "personal", "started_at": "2026-06-09T11:00:00+00:00"},
            {"task_id": "t-002", "account": "personal", "started_at": "2026-06-09T11:30:00+00:00"},
        ],
    )
    payload["schema_version"] = 6
    _seed_supervisor_json(queue, payload)

    out = _run_snapshot(snapshot_script, queue)

    assert "| in-flight | target |" in out
    assert "| personal | `slowing_down` | 55% | 5% |  | 2 | 2 |" in out
    assert "| work | `idle` | 10% | 5% |  | 0 | — |" in out


# ---------------------------------------------------------------------------
# Paused account
# ---------------------------------------------------------------------------


def test_paused_account_shows_yes_marker(tmp_path: Path, snapshot_script: Path) -> None:
    queue = _make_queue(tmp_path)
    _seed_supervisor_json(
        queue,
        _v3_accounts_payload(
            accounts={
                "personal": {
                    "state": "idle",
                    "since": "2026-06-09T10:00:00+00:00",
                    "last_5h_util_pct": 0,
                    "last_weekly_util_pct": 0,
                    "paused": True,
                    "consecutive_clean_polls": 0,
                    "last_drift_message": "",
                },
            },
        ),
    )

    out = _run_snapshot(snapshot_script, queue)
    # Paused marker in the paused column (between weekly% and in-flight).
    assert "| personal | `idle` | 0% | 0% | yes |" in out


# ---------------------------------------------------------------------------
# Drift surfacing
# ---------------------------------------------------------------------------


def test_drift_message_surfaced_as_list_below_table(tmp_path: Path, snapshot_script: Path) -> None:
    """A non-empty ``last_drift_message`` shows up as a bulleted entry
    below the table rather than inlined as a column — keeps the table
    readable when drift strings are long or contain pipes."""
    queue = _make_queue(tmp_path)
    drift_msg = "parse failure: unexpected token '|' in window header on line 3"
    _seed_supervisor_json(
        queue,
        _v3_accounts_payload(
            accounts={
                "personal": {
                    "state": "error_drift",
                    "since": "2026-06-09T10:00:00+00:00",
                    "last_5h_util_pct": 0,
                    "last_weekly_util_pct": 0,
                    "paused": False,
                    "consecutive_clean_polls": 0,
                    "last_drift_message": drift_msg,
                },
            },
        ),
    )

    out = _run_snapshot(snapshot_script, queue)
    assert "_Per-account drift messages:_" in out
    # Pipe inside the drift string must be backslash-escaped so the
    # markdown bullet isn't truncated by the renderer.
    assert r"unexpected token '\|'" in out
    # Account name surfaces on the bullet.
    assert "- `personal`:" in out


# ---------------------------------------------------------------------------
# Empty accounts map — soft-fail rather than hard exit
# ---------------------------------------------------------------------------


def test_missing_accounts_map_soft_fails(tmp_path: Path, snapshot_script: Path) -> None:
    """A v2-shaped (or pre-tick v3) supervisor.json with no `accounts`
    key must render a soft marker, not abort the entire script."""
    queue = _make_queue(tmp_path)
    _seed_supervisor_json(
        queue,
        {
            "schema_version": 2,
            "state": "dispatching",
            "since": "2026-06-09T11:00:00+00:00",
            "last_5h_util_pct": 25,
            "last_weekly_util_pct": 30,
            "in_flight_task_ids": [],
        },
    )

    out = _run_snapshot(snapshot_script, queue)
    assert "**Per-account state**" in out
    # The exact prose isn't important; the marker phrase is.
    assert "no `accounts` map in supervisor.json" in out
    # And earlier sections still rendered — proves the soft-fail
    # didn't abort the script.
    assert "## Queue status" in out
    assert "supervisor.json" in out


# ---------------------------------------------------------------------------
# Missing supervisor.json
# ---------------------------------------------------------------------------


def test_missing_supervisor_json_skips_per_account_section_gracefully(
    tmp_path: Path, snapshot_script: Path
) -> None:
    queue = _make_queue(tmp_path)
    # Ensure the runtime dir exists but supervisor.json does NOT.
    (queue / ".claude_task_runner").mkdir()
    out = _run_snapshot(snapshot_script, queue)
    # The earlier section reports it as missing.
    assert "supervisor.json**: missing" in out
    # And the per-account block is just absent — no traceback, no
    # "**Per-account state**" header (we guard the whole block on the
    # supervisor.json existing).
    assert "**Per-account state**" not in out
