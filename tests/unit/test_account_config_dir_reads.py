"""Usage is read from the account's own ``config_dir``, never ``[claude].config_dir``.

``[claude].config_dir`` is a legacy alias: with no ``[[accounts]]`` the
loader makes a ``default`` account from it, and once ``[[accounts]]`` is
declared it is ignored. Pinned here: the supervisor's usage source and
every ``usage`` subcommand read the account's ``config_dir``, whether
``[claude].config_dir`` is empty or set alongside it, because reading the
legacy field would poll another account's usage (``~/.claude`` when it is
empty) and throttle on the wrong numbers. ``usage`` reads the queue's only
account, or the one ``--account`` names, and refuses to guess among several.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from claude_task_runner.cli import app
from claude_task_runner.cli.supervisor_cmd import _build_usage_source
from claude_task_runner.config.loader import load_settings
from claude_task_runner.usage.api_source import ApiUsageSource
from claude_task_runner.usage.drift import UsageCaptureTimeout
from claude_task_runner.usage.multi_account_source import MultiAccountUsageSource
from claude_task_runner.usage.source import ApiThenTtyUsageSource, ClaudeUsageSource


def _toml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "claude_runner.toml"
    path.write_text(body, encoding="utf-8")
    return path


def _accounts_toml(accounts: dict[str, Path], claude_config_dir: str = "") -> str:
    body = f'[claude]\nconfig_dir = "{claude_config_dir}"\n\n' if claude_config_dir else ""
    return body + "".join(
        f'[[accounts]]\nname = "{name}"\nconfig_dir = "{path}"\n\n'
        for name, path in accounts.items()
    )


def _read_dirs(source: object) -> list[str]:
    """The ``config_dir`` each inner source reads, API first."""
    if isinstance(source, ApiThenTtyUsageSource):
        return _read_dirs(source._api) + _read_dirs(source._tty)
    if isinstance(source, ApiUsageSource):
        return [source._config_dir]
    if isinstance(source, ClaudeUsageSource):
        return [source._claude_config_dir]
    raise AssertionError(f"unexpected source {source!r}")


def _with_mode(settings_path: Path, mode: str) -> object:
    settings = load_settings(settings_path)
    return settings.model_copy(update={"usage": settings.usage.model_copy(update={"source": mode})})


MODES = {"tty": 1, "api": 1, "api_then_tty": 2}
"""``[usage].source`` → how many inner sources read a ``config_dir``."""


class TestTheSupervisorsUsageSource:
    @pytest.mark.parametrize("claude_config_dir", ["", "/legacy/ignored"])
    @pytest.mark.parametrize("mode", list(MODES))
    def test_one_account_reads_its_own_config_dir(
        self, tmp_path: Path, mode: str, claude_config_dir: str
    ) -> None:
        """``[claude].config_dir`` plays no part, empty or set: the account
        names its own."""
        work = tmp_path / "work"
        work.mkdir()
        path = _toml(tmp_path, _accounts_toml({"work": work}, claude_config_dir))
        source = _build_usage_source(_with_mode(path, mode), tmp_path, lambda: None)
        assert _read_dirs(source) == [str(work)] * MODES[mode]

    @pytest.mark.parametrize("mode", list(MODES))
    def test_a_legacy_toml_still_reads_claude_config_dir(self, tmp_path: Path, mode: str) -> None:
        """No ``[[accounts]]``: the ``default`` account the loader makes from
        ``[claude].config_dir`` is the one read."""
        legacy = tmp_path / "legacy"
        legacy.mkdir()
        path = _toml(tmp_path, f'[claude]\nconfig_dir = "{legacy}"\n')
        source = _build_usage_source(_with_mode(path, mode), tmp_path, lambda: None)
        assert _read_dirs(source) == [str(legacy)] * MODES[mode]

    def test_one_account_on_a_long_lived_token_uses_the_api_alone(self, tmp_path: Path) -> None:
        """The same rule as for each account of several: the TTY fallback
        cannot recover a revoked long-lived token."""
        work = tmp_path / "work"
        work.mkdir()
        (work / "oauth-token").write_text("sk-ant-oat01-x\n", encoding="utf-8")
        path = _toml(tmp_path, _accounts_toml({"work": work}))
        source = _build_usage_source(_with_mode(path, "api_then_tty"), tmp_path, lambda: None)
        assert isinstance(source, ApiUsageSource)
        assert _read_dirs(source) == [str(work)]

    def test_several_accounts_each_read_their_own(self, tmp_path: Path) -> None:
        dirs = {name: tmp_path / name for name in ("personal", "work")}
        for path in dirs.values():
            path.mkdir()
        path = _toml(tmp_path, _accounts_toml(dirs, "/legacy/ignored"))
        source = _build_usage_source(_with_mode(path, "api"), tmp_path, lambda: None)
        assert isinstance(source, MultiAccountUsageSource)
        assert {name: _read_dirs(inner) for name, inner in source._per_account.items()} == {
            "personal": [str(dirs["personal"])],
            "work": [str(dirs["work"])],
        }


# ---------------------------------------------------------------------------
# claude-task-runner usage
# ---------------------------------------------------------------------------


def _whoami(path: Path, *account: str) -> object:
    # Wide enough that Rich prints each path on its label's line.
    return CliRunner().invoke(
        app,
        ["usage", "--config", str(path), *account, "whoami", "--quick"],
        env={"COLUMNS": "400"},
    )


def _config_dir_line(result: object) -> str:
    (line,) = [ln for ln in result.stdout.splitlines() if ln.startswith("Config dir:")]
    return line.split(":", 1)[1].strip()


class TestTheUsageCommand:
    @pytest.mark.parametrize("claude_config_dir", ["", "/legacy/ignored"])
    def test_one_account_is_read_without_account(
        self, tmp_path: Path, claude_config_dir: str
    ) -> None:
        work = tmp_path / "work"
        path = _toml(tmp_path, _accounts_toml({"work": work}, claude_config_dir))
        result = _whoami(path)
        assert result.exit_code == 0, result.output
        assert _config_dir_line(result) == str(work)

    def test_a_legacy_toml_reads_claude_config_dir(self, tmp_path: Path) -> None:
        legacy = tmp_path / "legacy"
        path = _toml(tmp_path, f'[claude]\nconfig_dir = "{legacy}"\n')
        result = _whoami(path)
        assert result.exit_code == 0, result.output
        assert _config_dir_line(result) == str(legacy)

    def test_account_names_one_of_several(self, tmp_path: Path) -> None:
        dirs = {name: tmp_path / name for name in ("personal", "work")}
        path = _toml(tmp_path, _accounts_toml(dirs, "/legacy/ignored"))
        result = _whoami(path, "--account", "work")
        assert result.exit_code == 0, result.output
        assert _config_dir_line(result) == str(dirs["work"])

    @pytest.mark.parametrize(
        "subcommand",
        [["whoami", "--quick"], ["render"], ["json"], ["healthcheck"], ["capture", "-o", "x"]],
        ids=lambda argv: argv[0],
    )
    def test_several_accounts_need_account(self, tmp_path: Path, subcommand: list[str]) -> None:
        """A usage error naming the accounts, before anything is captured;
        never a guess at one of them."""
        dirs = {name: tmp_path / name for name in ("personal", "work")}
        path = _toml(tmp_path, _accounts_toml(dirs))
        with patch(
            "claude_task_runner.usage.capture.capture",
            side_effect=AssertionError("captured"),
        ):
            result = CliRunner().invoke(app, ["usage", "--config", str(path), *subcommand])
        assert result.exit_code == 2
        assert (
            "Invalid value for '--account': the queue declares 2 accounts (personal, "
            "work); name the one to read with --account"
        ) in " ".join(result.output.replace("│", " ").split())

    @pytest.mark.parametrize("several", [False, True], ids=["one-account", "account-option"])
    @pytest.mark.parametrize(
        "subcommand",
        [["render"], ["json"], ["healthcheck"], ["capture", "-o", "x.cap"], ["whoami"]],
        ids=lambda argv: argv[0],
    )
    def test_every_capture_reads_the_selected_account(
        self, tmp_path: Path, subcommand: list[str], several: bool
    ) -> None:
        """The ``config_dir`` each subcommand hands the capture is the account's."""
        dirs = {name: tmp_path / name for name in ("personal", "work")}
        path = _toml(tmp_path, _accounts_toml(dirs if several else {"work": dirs["work"]}))
        account = ["--account", "work"] if several else []
        seen: list[object] = []

        def fake_capture(*_args: object, claude_config_dir: str, **_kwargs: object) -> object:
            seen.append(claude_config_dir)
            raise UsageCaptureTimeout("stopped here")

        with patch("claude_task_runner.usage.capture.capture", fake_capture):
            CliRunner().invoke(app, ["usage", "--config", str(path), *account, *subcommand])
        assert seen == [str(dirs["work"])]

    @pytest.mark.parametrize(
        "subcommand", [["whoami", "--quick"], ["refresh"]], ids=lambda argv: argv[0]
    )
    def test_an_unknown_account_is_refused(self, tmp_path: Path, subcommand: list[str]) -> None:
        """Before any subcommand runs, refresh included, which would otherwise
        refresh nothing and report success."""
        path = _toml(tmp_path, _accounts_toml({"work": tmp_path / "work"}))
        with patch(
            "claude_task_runner.usage.oauth_refresh.refresh_all_accounts",
            side_effect=AssertionError("refreshed"),
        ):
            result = CliRunner().invoke(
                app, ["usage", "--config", str(path), "--account", "personal", *subcommand]
            )
        assert result.exit_code == 2
        assert (
            "Invalid value for '--account': no account 'personal' in [[accounts]]; configured: work"
        ) in " ".join(result.output.replace("│", " ").split())

    @pytest.mark.parametrize(
        ("account", "refreshed"), [([], ["personal", "work"]), (["--account", "work"], ["work"])]
    )
    def test_refresh_takes_every_account_or_the_one_named(
        self, tmp_path: Path, account: list[str], refreshed: list[str]
    ) -> None:
        dirs = {name: tmp_path / name for name in ("personal", "work")}
        path = _toml(tmp_path, _accounts_toml(dirs))
        seen: list[list[str]] = []

        def fake_refresh(*, accounts: list[object], **_: object) -> list[object]:
            seen.append([a.name for a in accounts])
            return []

        with patch("claude_task_runner.usage.oauth_refresh.refresh_all_accounts", fake_refresh):
            result = CliRunner().invoke(app, ["usage", "--config", str(path), *account, "refresh"])
        assert result.exit_code == 0, result.output
        assert seen == [refreshed]
