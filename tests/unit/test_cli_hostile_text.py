"""Every command prints ids, paths, reasons and errors exactly as written.

Rich reads ``[...]`` as markup and ``:b:`` as an emoji code in a ``str`` it
prints. Each test here puts brackets that read as markup (``[x]``, ``[/]``)
and an emoji code into what one command prints, through a task id, a path
(the queue's own directory is named ``q[x]:b:``), a title, a reason, a
sidecar question or an error, and checks the lines as printed.
``tests/unit/test_cli_markup_gate.py`` keeps new prints from regressing.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_task_runner.queue.schema import SidecarOption, SidecarQuestion
from claude_task_runner.queue.store import queue_runtime_dir, state_path_for, todo_dir
from claude_task_runner.usage.drift import UsageCaptureSpawnError
from claude_task_runner.usage.whoami import IdentitySnapshot

from ._concurrency_fixtures import invoke_cli
from ._mixed_queue import add_state, add_task
from ._sidecar_files import make_request, write_request

HOSTILE = "[x] [/] :b:"
EMOJI_B = "\N{NEGATIVE SQUARED LATIN CAPITAL LETTER B}"
"""What Rich prints for ``:b:`` with emoji codes on."""


@pytest.fixture
def queue(tmp_path: Path) -> Path:
    qd = tmp_path / "q[x]:b:"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


def _out(argv: list[str], code: int = 0) -> list[str]:
    result = invoke_cli(argv)
    assert result.exit_code == code, result.output
    assert EMOJI_B not in result.output
    return result.output.splitlines()


def test_queue_list(queue: Path) -> None:
    add_task(queue, "h[x]:b:", title=f"T {HOSTILE}", model="m[x]:b:")
    (todo_dir(queue) / "u[x]:b:.yaml").write_text("not: valid: yaml: [\n", encoding="utf-8")

    lines = _out(["queue", "list", "--queue", str(queue)])

    assert lines[0] == "h[x]:b:  (m[x]:b:, medium, priority=normal)"
    assert lines[1] == f"  T {HOSTILE}"
    assert lines[2].startswith("  invalid effort: model 'm[x]:b:' has no effort levels configured")
    assert "[effort_levels] in claude_runner.toml" in lines[2]
    assert lines[3].startswith(f"u[x]:b:: {queue}/todo/u[x]:b:.yaml")


def test_queue_states(queue: Path) -> None:
    add_task(queue, "h[x]:b:")
    add_state(queue, "h[x]:b:", "deferred", deferred_reason=HOSTILE)
    state_path_for(queue, "c[x]:b:").write_text("not yaml: ][[", encoding="utf-8")

    lines = _out(["queue", "states", "--queue", str(queue)])

    assert lines == [
        "c[x]:b:  ?",
        "  error: unparseable",
        "h[x]:b:  deferred",
        f"  deferred_reason: {HOSTILE}",
    ]


def test_queue_show(queue: Path) -> None:
    add_task(
        queue,
        "h[x]:b:",
        title=f"T {HOSTILE}",
        requires=[{"kind": "file", "path": "in[x]:b:.md"}],
    )
    add_state(queue, "h[x]:b:", "pending", session_id="s[x]:b:")

    lines = _out(["queue", "show", "h[x]:b:", "--queue", str(queue)])

    assert lines[:5] == [
        "h[x]:b:",
        f"  title: T {HOSTILE}",
        "  model: claude-opus-5-5  effort: medium",
        "  status: pending  attempts: 0",
        "  session_id: s[x]:b:",
    ]
    assert lines[5] == "  requires: 0/1 satisfied"
    assert lines[6] == f"    ✗ missing file: {queue}/in[x]:b:.md"


def test_queue_add(queue: Path) -> None:
    bad = _out(
        ["queue", "add", "--queue", str(queue), "--id", "bad[/]", "--title", "t", "--prompt", "p"],
        2,
    )
    assert bad == ["invalid task id: 'bad[/]' (allowed: ^[A-Za-z0-9._-]+$)"]

    lines = _out(
        [
            "queue",
            "add",
            "--queue",
            str(queue),
            "--id",
            "ok",
            "--title",
            "t",
            "--prompt",
            "p",
            "--add-dir",
            "/nope[x]:b:",
            "--no-working-dir",
        ]
    )
    assert lines[0] == "warning: --add-dir /nope[x]:b: is not an existing directory"


def test_queue_restart_fresh(queue: Path) -> None:
    lines = _out(["queue", "restart-fresh", "z[x]:b:", "--queue", str(queue)], 2)
    assert lines == [
        f"no state YAML for task 'z[x]:b:': {queue}/.claude_task_runner/state/z[x]:b:.yaml"
    ]


def test_queue_backfill_working_dir(queue: Path) -> None:
    (queue / "claude_runner.toml").write_text(
        '[queue]\nworking_dir_template = "/w/{task_id}"\n', encoding="utf-8"
    )
    add_task(queue, "h[x]:b:")

    lines = _out(["queue", "backfill-working-dir", "--queue", str(queue), "--dry-run"])

    assert "  h[x]:b:  -> /w/h[x]:b:" in lines


def test_sidecar_list_and_show(queue: Path) -> None:
    request = make_request("h[x]:b:").model_copy(
        update={
            "summary": f"S {HOSTILE}",
            "context": f"C {HOSTILE}",
            "questions": [
                SidecarQuestion(
                    id="q[x]",
                    prompt=f"P {HOSTILE}?",
                    options=[
                        SidecarOption(value="A", label=f"L {HOSTILE}", description=f"D {HOSTILE}")
                    ],
                    recommended="A",
                )
            ],
        }
    )
    write_request(queue, request)

    listed = _out(["sidecar", "list", "--queue", str(queue)])
    assert listed[0] == f"h[x]:b: #001 q[x] S {HOSTILE}"

    shown = _out(["sidecar", "show", "h[x]:b:", "1", "--queue", str(queue)])
    assert shown[:5] == [
        "task: h[x]:b:  seq: 1",
        f"summary: S {HOSTILE}",
        "context:",
        f"  C {HOSTILE}",
        "",
    ]
    assert shown[5:8] == [f"Q q[x]: P {HOSTILE}?", f"  * A: L {HOSTILE}", f"      D {HOSTILE}"]


def test_supervisor_status_and_doctor(queue: Path) -> None:
    status = _out(["supervisor", "status", "--queue", str(queue)])
    assert status[0] == f"Queue:            {queue}"

    doctor = invoke_cli(["doctor", "--queue", str(queue)])
    assert EMOJI_B not in doctor.output
    assert doctor.output.splitlines()[0] == f"Queue: {queue}"


def test_account_list(queue: Path, tmp_path: Path) -> None:
    broken = tmp_path / "cfg[x]:b:"
    broken.mkdir()
    (broken / "runner-account.toml").write_text("max_concurrency 5\n", encoding="utf-8")
    (queue / "claude_runner.toml").write_text(
        f'[[accounts]]\nname = "work"\nconfig_dir = "{broken}"\n', encoding="utf-8"
    )

    result = invoke_cli(["account", "list", "--queue", str(queue)])

    assert EMOJI_B not in result.output
    lines = result.output.splitlines()
    assert f"  config_dir:      {broken}" in lines
    errors = [line for line in lines if line.startswith("  policy error:")]
    assert len(errors) == 1
    assert f"{broken}/runner-account.toml" in errors[0]


def test_install(queue: Path, tmp_path: Path) -> None:
    unit = tmp_path / "u[x]:b:.service"
    plan = MagicMock(
        block_existed=False,
        unit_path=unit,
        unit_text="[Unit]\nDescription=x [/] :b:\n",
        enable_command=["systemctl", "--user", "enable", "--now", "u[x]:b:.service"],
        existing_text=None,
    )
    with (
        patch("claude_task_runner.cli.install_cmd._detect_init_system", return_value="systemd"),
        patch(
            "claude_task_runner.cli.install_cmd.systemd_mod.build_install_plan", return_value=plan
        ),
        patch("claude_task_runner.cli.install_cmd.systemd_mod.apply_plan"),
        patch(
            "claude_task_runner.cli.install_cmd.shutil.which",
            return_value="/usr/local/bin/claude-task-runner",
        ),
    ):
        lines = _out(["install", "--yes", "--queue", str(queue)])

    at = lines.index("Will create systemd user unit at:")
    assert lines[at + 1 : at + 8] == [
        f"  {unit}",
        "",
        "Unit text:",
        "  [Unit]",
        "  Description=x [/] :b:",
        "",
        "Then run: systemctl --user enable --now u[x]:b:.service",
    ]


def test_install_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home[x]:b:"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    lines = _out(["install-skills", "--yes", "--copy"])

    assert lines[0] == f"Skills target: {home}/.claude/skills    mode: copy"


def test_usage_render_error() -> None:
    with patch(
        "claude_task_runner.cli.usage_cmd.capture_mod.capture",
        side_effect=UsageCaptureSpawnError(HOSTILE),
    ):
        result = invoke_cli(["usage", "render"])

    assert EMOJI_B not in result.output
    assert result.output.splitlines() == [f"spawn error: {HOSTILE}"]


def test_usage_whoami() -> None:
    snap = IdentitySnapshot(
        config_dir="/home/u[x]:b:/.claude",
        subscription_type="t[x]:b:",
        rate_limit_tier="",
        scopes=("s[x]", ":b:"),
        welcome_label=f"W {HOSTILE}",
    )
    with patch(
        "claude_task_runner.cli.usage_cmd.whoami_mod.from_credentials_only", return_value=snap
    ):
        lines = _out(["usage", "whoami", "--quick"])

    assert lines == [
        "Config dir:      /home/u[x]:b:/.claude",
        "Subscription:    t[x]:b:",
        "Rate limit:      (unknown)",
        f"Org / account:   W {HOSTILE}",
        "Scopes:          s[x], :b:",
        "Account class:   unknown — verify before relying on usage numbers",
    ]
