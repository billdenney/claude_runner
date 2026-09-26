"""Smoke-test a non-editable install of claude-task-runner.

CI's other steps import the package from an editable install, whose ``.pth``
file serves ``src/``. A data file missing from the wheel, an
``importlib.resources`` lookup that works only in a source tree, or an
executable bit lost on the way into site-packages would pass all of them.
``tests/unit/test_packaging.py`` checks what the wheel contains; this checks
that the installed package runs. CI installs the package into a fresh venv
and runs this script with that venv's interpreter::

    python -m venv "$RUNNER_TEMP/wheel-venv"
    "$RUNNER_TEMP/wheel-venv/bin/pip" install .
    cd "$RUNNER_TEMP"
    "$RUNNER_TEMP/wheel-venv/bin/python" "$GITHUB_WORKSPACE/scripts/smoke_installed.py"

Run it from outside the checkout, so ``src/`` cannot shadow site-packages. It
runs every check, prints PASS or FAIL for each, and exits 1 if any failed.
Only the first check asks where the package is installed; the others pin
paths relative to the package that was imported. So from an editable install
exactly the first check fails, which ``tests/unit/test_smoke_installed.py``
pins.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import sysconfig
import tempfile
import traceback
from collections.abc import Callable, Mapping
from pathlib import Path

import claude_task_runner
from claude_task_runner.cli.install_cmd import _watchdog_script_path
from claude_task_runner.cli.install_skills_cmd import SKILL_NAMES, _packaged_skill_dir
from claude_task_runner.config.loader import load_settings

Check = Callable[[], list[str]]
"""A check returns what it found wrong. An empty list is a pass."""


def _run(argv: list[str], env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=120, check=False)


def check_site_packages(package_dir: Path, purelib: Path) -> list[str]:
    """The imported package is the one installed in ``purelib``."""
    expected = (purelib / "claude_task_runner").resolve()
    if package_dir == expected:
        return []
    return [f"imported from {package_dir}, not {expected}"]


def check_help(cli: Path) -> list[str]:
    """The console script starts and prints the root command's usage line."""
    if not cli.is_file():
        return [f"no console script at {cli}"]
    proc = _run([str(cli), "--help"])
    if proc.returncode != 0:
        return [f"exited {proc.returncode}", *proc.stderr.splitlines()]
    if not proc.stdout.startswith("Usage: claude-task-runner "):
        return ["printed no usage line", *proc.stdout.splitlines()]
    return []


def check_defaults() -> list[str]:
    """``load_settings`` reads ``settings.toml`` through ``importlib.resources``
    and raises if the file is missing or fails validation."""
    load_settings(None)
    return []


def check_skill_dirs(package_dir: Path) -> list[str]:
    """``_packaged_skill_dir``, which ``install-skills`` links to, finds every
    skill inside the imported package."""
    problems: list[str] = []
    for name in SKILL_NAMES:
        expected = package_dir / "skills" / name
        try:
            found = _packaged_skill_dir(name)
        except FileNotFoundError as exc:
            problems.append(str(exc))
            continue
        if found.resolve() != expected.resolve():
            problems.append(f"{name}: resolved to {found}, not {expected}")
        elif not (found / "SKILL.md").is_file():
            problems.append(f"{name}: no SKILL.md in {found}")
    return problems


def check_executable(path: Path) -> list[str]:
    """``path`` is a file this user can execute."""
    if not path.is_file():
        return [f"{path} is missing"]
    if not os.access(path, os.X_OK):
        return [f"{path} is not executable ({stat.filemode(path.stat().st_mode)})"]
    return []


def check_directly_run_scripts(package_dir: Path) -> list[str]:
    """The two shipped scripts that are run as commands are executable.

    Cron runs the ``watchdog.sh`` whose path a cron install writes into the
    crontab, and ``merge_branches.sh`` runs ``verify_branch_contributions.sh``
    from its own directory. Every other helper is run through ``bash``,
    ``python3`` or ``Rscript``.
    """
    problems: list[str] = []
    watchdog = _watchdog_script_path()
    if watchdog != package_dir / "cron" / "watchdog.sh":
        problems.append(f"_watchdog_script_path() is {watchdog}, not in {package_dir}")
    merge_skill = _packaged_skill_dir("runner-merge-claude-branches")
    for path in (watchdog, merge_skill / "verify_branch_contributions.sh"):
        problems += check_executable(path)
    return problems


def check_skill_links(skills_dir: Path, expected: Mapping[str, Path]) -> list[str]:
    """``skills_dir`` holds exactly one symlink per name in ``expected``, and
    each resolves to its directory, which holds a SKILL.md."""
    if not skills_dir.is_dir():
        return [f"{skills_dir} is not a directory"]
    problems: list[str] = []
    found = sorted(entry.name for entry in skills_dir.iterdir())
    if found != sorted(expected):
        problems.append(f"{skills_dir} holds {found}, not {sorted(expected)}")
    for name, target in expected.items():
        link = skills_dir / name
        if not link.is_symlink():
            problems.append(f"{name}: {link} is {'not a symlink' if link.exists() else 'missing'}")
            continue
        try:
            resolved = link.resolve(strict=True)
        except (OSError, RuntimeError):
            problems.append(f"{name}: {link} -> {os.readlink(link)} does not resolve")
            continue
        if resolved != target.resolve():
            problems.append(f"{name}: {link} resolves to {resolved}, not {target}")
        elif not (resolved / "SKILL.md").is_file():
            problems.append(f"{name}: no SKILL.md in {resolved}")
    return problems


def check_install_skills(cli: Path, package_dir: Path) -> list[str]:
    """``install-skills --yes`` with ``HOME`` set to an empty directory links
    every skill to its directory in the imported package."""
    with tempfile.TemporaryDirectory(prefix="smoke-home-") as home:
        proc = _run([str(cli), "install-skills", "--yes"], env={**os.environ, "HOME": home})
        if proc.returncode != 0:
            return [
                f"exited {proc.returncode}",
                *proc.stdout.splitlines(),
                *proc.stderr.splitlines(),
            ]
        expected = {name: package_dir / "skills" / name for name in SKILL_NAMES}
        return check_skill_links(Path(home) / ".claude" / "skills", expected)


def run_checks(checks: list[tuple[str, Check]]) -> int:
    """Run every check and print PASS or FAIL for each. Returns how many failed.

    A check that raises fails, and its traceback is printed as the reason.
    """
    failed = 0
    for claim, check in checks:
        try:
            problems = check()
        except Exception:
            problems = traceback.format_exc().splitlines()
        print(f"{'FAIL' if problems else 'PASS'}  {claim}")
        for line in problems:
            print(f"      {line}")
        failed += bool(problems)
    return failed


def main() -> int:
    package_dir = Path(claude_task_runner.__file__).resolve().parent
    purelib = Path(sysconfig.get_path("purelib"))
    # The console script installed beside this interpreter. A PATH lookup
    # would find the wrong one in CI, where the editable install's is on PATH.
    cli = Path(sysconfig.get_path("scripts")) / "claude-task-runner"
    print(f"interpreter:    {sys.executable}")
    print(f"package:        {package_dir}")
    print(f"site-packages:  {purelib}")
    print(f"console script: {cli}\n")

    checks: list[tuple[str, Check]] = [
        (
            "claude_task_runner imports from this interpreter's site-packages",
            lambda: check_site_packages(package_dir, purelib),
        ),
        ("claude-task-runner --help exits 0", lambda: check_help(cli)),
        ("load_settings(None) validates the shipped defaults", check_defaults),
        (
            "every skill in SKILL_NAMES resolves to its directory, which has a SKILL.md",
            lambda: check_skill_dirs(package_dir),
        ),
        (
            "watchdog.sh and verify_branch_contributions.sh are executable",
            lambda: check_directly_run_scripts(package_dir),
        ),
        (
            "install-skills --yes links every skill into an empty HOME",
            lambda: check_install_skills(cli, package_dir),
        ),
    ]
    failed = run_checks(checks)
    if failed:
        print(f"\n{failed} of {len(checks)} checks failed.")
        return 1
    print(f"\nAll {len(checks)} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
