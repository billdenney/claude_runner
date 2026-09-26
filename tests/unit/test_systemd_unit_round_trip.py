"""Round-trip the generated systemd ``ExecStart`` / ``ExecStop`` lines
back through the ``supervisor`` CLI.

``build_unit_text`` writes two command lines into the unit. The
ExecStop line depends on ``[supervisor].adopt_workers`` (ADR-0025):

    ExecStart=<binary> supervisor start  --queue <q> [--config <c>]
    # adoption ON (default): fast stop
    ExecStop=-<binary> supervisor stop   --queue <q> [--config <c>]
    # adoption OFF: graceful drain
    ExecStop=-<binary> supervisor drain  --queue <q> [--config <c>] --no-wait

The ExecStop line is *derived* from ExecStart (swap ``start`` ->
``stop``/``drain``). If the ``stop`` or ``drain`` subcommand ever
stopped accepting a flag that ``start`` emits (the historical
``--config`` bug this test guards against), ``systemctl stop`` would die
with ``No such option: --config`` and the stop path would silently
break. These tests guard BOTH modes.

The leading ``-`` on ExecStop is systemd's prefix for "ignore this
command's exit status", not part of the path; systemd strips it before
it runs the command. After a clean exit the supervisor has removed its
PID file, so ExecStop exits 1, which is the runtime path these tests
drive, and the prefix keeps systemd from marking the unit failed.

These tests parse the generated lines and feed the argv (minus the
prefix, the binary and the ``supervisor`` group name) into
:data:`cli.supervisor_cmd.app` via :class:`typer.testing.CliRunner`,
asserting the parser accepts *every* flag the unit emits. A usage
error (unknown option) is Click exit code 2; we assert we never get
that.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from claude_task_runner.cli.supervisor_cmd import app as supervisor_app
from claude_task_runner.config.loader import load_settings
from claude_task_runner.cron.systemd_unit import build_unit_text

# A realistic full command line: an absolute pipx/venv binary path, the
# ``supervisor start`` subcommand, and BOTH --queue and --config (the
# flag combination the cron/systemd installer emits for a per-queue
# config). Spaces in neither path so shlex round-trips cleanly.
_BINARY = "/opt/venv/bin/claude-task-runner"
_QUEUE = "/srv/queue"
_CONFIG = "/srv/queue/claude_runner.toml"
_SUPERVISOR_COMMAND = f"{_BINARY} supervisor start --queue {_QUEUE} --config {_CONFIG}"

# The unit's restart policy does not affect its Exec lines.
_WATCHDOG = load_settings(None).watchdog

# Click/Typer usage-error exit code (unknown option, missing required
# arg, etc.). Distinct from runtime exit codes the commands raise
# (e.g. drain's exit-1 "no PID file"), so it cleanly isolates a
# flag-acceptance failure.
_USAGE_ERROR_EXIT = 2


def _exec_line(unit_text: str, key: str) -> str:
    """Return the single ``<key>=...`` line from the unit text."""
    matches = [ln for ln in unit_text.splitlines() if ln.startswith(f"{key}=")]
    assert len(matches) == 1, f"expected exactly one {key}= line, got {matches!r}"
    return matches[0]


def _split_exec_line(exec_line: str) -> tuple[str, list[str]]:
    """Split an ``ExecStart=``/``ExecStop=`` line into systemd's executable
    prefix and the argv that the ``supervisor`` sub-app should receive
    (subcommand + flags).

    The prefix is ``"-"`` (ignore the exit status) or ``""``. Any other
    prefix from systemd.service(5), "Command lines" (``@ : + !``), fails
    the test: the unit never emits one, and ``@`` would also change which
    token is argv[0], which this parser doesn't model.

    Drops the prefix, the binary path and the ``supervisor`` group name
    so the remainder can be fed straight to ``CliRunner.invoke(app, ...)``.
    """
    value = exec_line.split("=", 1)[1]
    prefix = "-" if value.startswith("-") else ""
    command = value[len(prefix) :]
    assert command, f"no command in {exec_line!r}"
    assert command[0] not in "@:+!-", f"unmodelled systemd prefix in {exec_line!r}"
    argv = shlex.split(command)
    # argv == [binary, "supervisor", <subcommand>, *flags]
    assert argv[0] == _BINARY, f"expected binary {_BINARY!r}, got {argv[0]!r}"
    assert argv[1] == "supervisor", f"expected 'supervisor' group, got {argv[1]!r}"
    return prefix, argv[2:]


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestExecStartRoundTrip:
    def test_argv_shape(self) -> None:
        """ExecStart parses to ``start`` + the emitted flags."""
        text = build_unit_text(
            supervisor_command=_SUPERVISOR_COMMAND, queue_dir=Path(_QUEUE), watchdog=_WATCHDOG
        )
        prefix, argv = _split_exec_line(_exec_line(text, "ExecStart"))
        # No prefix: a supervisor that fails must still count as failed.
        assert prefix == ""
        assert argv == ["start", "--queue", _QUEUE, "--config", _CONFIG]

    def test_start_subcommand_accepts_every_flag(self, runner: CliRunner) -> None:
        """The ``supervisor start`` parser accepts the unit's flags.

        ``start`` blocks on the daemon loop, so we patch ``start_daemon``
        (and the settings load it depends on) to no-ops. ``_QUEUE`` does
        not exist here, so the check that ``--queue`` is an existing
        directory is patched to accept it. A clean exit 0 proves Typer
        parsed AND routed every flag — a stray flag would have produced
        exit code 2 before the body ever ran.
        """
        text = build_unit_text(
            supervisor_command=_SUPERVISOR_COMMAND, queue_dir=Path(_QUEUE), watchdog=_WATCHDOG
        )
        _, argv = _split_exec_line(_exec_line(text, "ExecStart"))

        with (
            patch(
                "claude_task_runner.cli.supervisor_cmd.require_queue_option",
                return_value=Path(_QUEUE),
            ) as require_queue_option,
            patch("claude_task_runner.cli.supervisor_cmd.load_settings"),
            patch("claude_task_runner.cli.supervisor_cmd.configure_logging"),
            patch("claude_task_runner.cli.supervisor_cmd.queue_runtime_dir"),
            patch("claude_task_runner.cli.supervisor_cmd.start_daemon") as start_daemon,
        ):
            start_daemon.return_value = type(
                "H", (), {"state_path": Path("/x"), "pid_path": Path("/y")}
            )()
            result = runner.invoke(supervisor_app, argv)

        assert result.exit_code == 0, result.output
        # The daemon was actually invoked — proves routing, not just parsing.
        assert start_daemon.called
        assert require_queue_option.call_args.args[0] == Path(_QUEUE)


class TestExecStopDrainRoundTrip:
    """Adoption OFF: ExecStop is ``supervisor drain --no-wait``."""

    def test_argv_shape(self) -> None:
        """ExecStop is the ``start`` line with ``drain`` + ``--no-wait``."""
        text = build_unit_text(
            supervisor_command=_SUPERVISOR_COMMAND,
            queue_dir=Path(_QUEUE),
            watchdog=_WATCHDOG,
            adopt_workers=False,
        )
        prefix, argv = _split_exec_line(_exec_line(text, "ExecStop"))
        assert prefix == "-"
        assert argv == ["drain", "--queue", _QUEUE, "--config", _CONFIG, "--no-wait"]

    def test_drain_subcommand_accepts_every_flag(self, runner: CliRunner, tmp_path: Path) -> None:
        """The ``supervisor drain`` parser accepts the unit's flags —
        including ``--config``, the flag the historical drain bug dropped.

        We point ``--queue`` at an empty tmp dir (no running supervisor),
        so drain exits 1 ("no PID file"): a *runtime* outcome that still
        proves every flag parsed. A rejected flag would be exit code 2.
        We assert NOT exit 2 and that the failure is the expected
        no-PID-file runtime path, not a usage error.
        """
        # Re-derive the unit text with a real tmp queue so drain's pidfile
        # lookup resolves to a writable, definitely-empty directory.
        cmd = f"{_BINARY} supervisor start --queue {tmp_path} --config {tmp_path}/c.toml"
        text = build_unit_text(
            supervisor_command=cmd, queue_dir=tmp_path, watchdog=_WATCHDOG, adopt_workers=False
        )
        prefix, argv = _split_exec_line(_exec_line(text, "ExecStop"))
        assert "--config" in argv  # the regression guard
        assert "--no-wait" in argv

        result = runner.invoke(supervisor_app, argv)

        assert result.exit_code != _USAGE_ERROR_EXIT, result.output
        assert "No such option" not in result.output
        # Positive confirmation it reached the runtime path: no PID file.
        assert result.exit_code == 1
        assert "No PID file" in result.output
        # A clean exit leaves the same state behind (no PID file), so
        # systemd sees this exit 1 after every clean exit. The `-` prefix
        # is what keeps it from marking the unit failed.
        assert prefix == "-"

    def test_drain_rejects_a_genuinely_unknown_flag(self, runner: CliRunner) -> None:
        """Control: an actually-unknown flag DOES trip the usage error,
        proving the round-trip assertions above are meaningful (the
        parser isn't simply ignoring everything)."""
        result = runner.invoke(
            supervisor_app, ["drain", "--queue", "/q", "--definitely-not-a-flag"]
        )
        assert result.exit_code == _USAGE_ERROR_EXIT
        assert "No such option" in result.output


class TestExecStopFastStopRoundTrip:
    """Adoption ON (default): ExecStop is ``supervisor stop`` (ADR-0025)."""

    def test_argv_shape(self) -> None:
        """ExecStop is the ``start`` line with ``start`` swapped to ``stop``."""
        text = build_unit_text(
            supervisor_command=_SUPERVISOR_COMMAND, queue_dir=Path(_QUEUE), watchdog=_WATCHDOG
        )
        prefix, argv = _split_exec_line(_exec_line(text, "ExecStop"))
        assert prefix == "-"
        assert argv == ["stop", "--queue", _QUEUE, "--config", _CONFIG]

    def test_stop_subcommand_accepts_every_flag(self, runner: CliRunner, tmp_path: Path) -> None:
        """The ``supervisor stop`` parser accepts the unit's flags —
        including ``--config`` (added for ExecStop symmetry, ADR-0025).

        Empty tmp queue ⇒ no running supervisor ⇒ stop exits 1 ("no PID
        file"), a runtime outcome that still proves every flag parsed. A
        rejected flag would be exit code 2.
        """
        cmd = f"{_BINARY} supervisor start --queue {tmp_path} --config {tmp_path}/c.toml"
        text = build_unit_text(supervisor_command=cmd, queue_dir=tmp_path, watchdog=_WATCHDOG)
        prefix, argv = _split_exec_line(_exec_line(text, "ExecStop"))
        assert argv[0] == "stop"
        assert "--config" in argv  # the regression guard for the new flag

        result = runner.invoke(supervisor_app, argv)

        assert result.exit_code != _USAGE_ERROR_EXIT, result.output
        assert "No such option" not in result.output
        assert result.exit_code == 1
        assert "No PID file" in result.output
        # As for drain: this is what ExecStop hits after every clean exit,
        # and the `-` prefix keeps systemd from marking the unit failed.
        assert prefix == "-"


class TestExecStartWithoutConfig:
    """The installer can emit a command line with no --config (queue-only).
    The round-trip must still hold for both stop modes."""

    def test_start_and_stop_accept_queue_only(self, runner: CliRunner, tmp_path: Path) -> None:
        cmd = f"{_BINARY} supervisor start --queue {tmp_path}"
        text = build_unit_text(supervisor_command=cmd, queue_dir=tmp_path, watchdog=_WATCHDOG)

        start = _split_exec_line(_exec_line(text, "ExecStart"))
        assert start == ("", ["start", "--queue", str(tmp_path)])
        prefix, stop_argv = _split_exec_line(_exec_line(text, "ExecStop"))
        assert (prefix, stop_argv) == ("-", ["stop", "--queue", str(tmp_path)])

        # stop parses cleanly (runtime exit 1, no usage error).
        result = runner.invoke(supervisor_app, stop_argv)
        assert result.exit_code != _USAGE_ERROR_EXIT, result.output
        assert result.exit_code == 1

    def test_start_and_drain_accept_queue_only(self, runner: CliRunner, tmp_path: Path) -> None:
        cmd = f"{_BINARY} supervisor start --queue {tmp_path}"
        text = build_unit_text(
            supervisor_command=cmd, queue_dir=tmp_path, watchdog=_WATCHDOG, adopt_workers=False
        )

        prefix, stop_argv = _split_exec_line(_exec_line(text, "ExecStop"))
        assert (prefix, stop_argv) == ("-", ["drain", "--queue", str(tmp_path), "--no-wait"])

        # drain parses cleanly (runtime exit 1, no usage error).
        result = runner.invoke(supervisor_app, stop_argv)
        assert result.exit_code != _USAGE_ERROR_EXIT, result.output
        assert result.exit_code == 1


class TestSplitExecLine:
    """Known answers for the parser that every round-trip test above relies on."""

    def test_splits_off_the_ignore_status_prefix(self) -> None:
        line = f"ExecStop=-{_BINARY} supervisor stop --queue {_QUEUE}"
        assert _split_exec_line(line) == ("-", ["stop", "--queue", _QUEUE])

    def test_unprefixed_line(self) -> None:
        line = f"ExecStart={_BINARY} supervisor start --queue {_QUEUE}"
        assert _split_exec_line(line) == ("", ["start", "--queue", _QUEUE])

    @pytest.mark.parametrize("prefix", ["@", ":", "+", "!", "!!", "--", "-@"])
    def test_rejects_a_prefix_it_does_not_model(self, prefix: str) -> None:
        with pytest.raises(AssertionError, match="unmodelled systemd prefix"):
            _split_exec_line(f"ExecStop={prefix}{_BINARY} supervisor stop")

    def test_rejects_a_different_binary(self) -> None:
        with pytest.raises(AssertionError, match="expected binary"):
            _split_exec_line("ExecStop=-/usr/bin/other supervisor stop")

    def test_rejects_an_empty_command(self) -> None:
        with pytest.raises(AssertionError, match="no command"):
            _split_exec_line("ExecStop=-")
