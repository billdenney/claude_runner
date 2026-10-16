"""The CLI loads the queue TOML only through ``load_settings_or_exit``.

That helper prints a configuration error without a traceback and exits as
``supervisor start`` does: 78 (``EX_CONFIG``) when the content is wrong, 1
when a file could not be read and may come back. A command that called
``load_settings`` itself would pick its own status, or let the error
escape as a traceback. So this test fails on any reference to
``load_settings`` under ``cli/`` outside :data:`ALLOWED`, and
``test_fail_fast_account_config.py`` runs every command that loads the
queue TOML against both kinds of error.
"""

from __future__ import annotations

import textwrap
from collections import Counter
from pathlib import Path

from ._reference_gate import references_by_scope

REPO_ROOT = Path(__file__).parent.parent.parent
CLI = REPO_ROOT / "src" / "claude_task_runner" / "cli"

ALLOWED: dict[tuple[str, str], tuple[int, str]] = {
    ("_helpers.py", "load_settings_or_exit"): (1, "the helper itself"),
    ("watchdog_cmd.py", "tick"): (
        2,
        "a cron tick logs a configuration error to watchdog.log instead of "
        "exiting with it, and reports one that may clear up as a WARNING "
        "until it has lasted [watchdog].transient_config_escalation_s; the "
        "second call loads the packaged defaults for that setting, which "
        "cannot fail",
    ),
}
"""``(path under cli/, qualified scope)`` → (how many references to
``load_settings`` it may make, why it does not use the helper)."""


def all_references() -> dict[tuple[str, str], int]:
    found: dict[tuple[str, str], int] = {}
    for path in sorted(CLI.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for scope, n in references_by_scope(source, "load_settings").items():
            found[(path.relative_to(CLI).as_posix(), scope)] = n
    return found


def test_only_the_allowed_sites_call_load_settings() -> None:
    found = all_references()
    allowed = {key: n for key, (n, _reason) in ALLOWED.items()}
    unexpected = {key: n for key, n in found.items() if allowed.get(key) != n}
    assert not unexpected, (
        f"load_settings under cli/, unexpected here: {sorted(unexpected.items())}. "
        "Load the queue TOML with cli._helpers.load_settings_or_exit."
    )
    # A removed site leaves a stale entry; the allowlist must not outlive it.
    assert sorted(found) == sorted(allowed)


def test_the_scanner_finds_each_form() -> None:
    source = textwrap.dedent(
        """
        from claude_task_runner.config import loader
        from claude_task_runner.config.loader import load_settings
        from claude_task_runner.config.loader import load_settings as ls

        def command(config):
            return load_settings(config)

        def aliased(config):
            return ls(config), loader.load_settings(config)

        def passed_on(config):
            return run(load_settings, config)

        class Group:
            async def method(self, config):
                return load_settings(config)

        def uses_the_helper(config, console):
            return load_settings_or_exit(config, console)
        """
    )
    assert references_by_scope(source, "load_settings") == Counter(
        {"command": 1, "aliased": 2, "passed_on": 1, "Group.method": 1}
    )
