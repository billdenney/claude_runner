"""Known answers for ``scripts/smoke_installed.py``, CI's non-editable install gate.

CI runs the script only against a wheel install, where every check should
pass, so a check that could never fail would go unnoticed there. These pin
what the script reports where the answer is known. Run from this editable
install, exactly the site-packages check fails. Fed a stand-in package or
console script with one defect, each check names that defect.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path
from types import ModuleType

import pytest

import claude_task_runner
from claude_task_runner.cli.install_skills_cmd import SKILL_NAMES
from claude_task_runner.config.loader import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "smoke_installed.py"
VERIFIER = "skills/runner-merge-claude-branches/verify_branch_contributions.sh"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("smoke_installed", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = _load()


def _write_executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


@pytest.fixture
def package_dir(tmp_path: Path) -> Path:
    """A stand-in installed package: every skill with its SKILL.md, and both
    directly-run scripts, executable."""
    package = tmp_path.resolve() / "site-packages" / "claude_task_runner"
    for name in SKILL_NAMES:
        (package / "skills" / name).mkdir(parents=True)
        (package / "skills" / name / "SKILL.md").write_text(f"# {name}\n")
    for script in ("cron/watchdog.sh", VERIFIER):
        _write_executable(package / script, "#!/bin/sh\n")
    return package


@pytest.fixture
def installed(package_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the script's package lookups at ``package_dir``."""

    def packaged_skill_dir(name: str) -> Path:
        path = package_dir / "skills" / name
        if not path.exists():
            raise FileNotFoundError(f"packaged skill {name!r} not found at expected path {path}")
        return path

    monkeypatch.setattr(smoke, "_packaged_skill_dir", packaged_skill_dir)
    monkeypatch.setattr(
        smoke, "_watchdog_script_path", lambda: package_dir / "cron" / "watchdog.sh"
    )
    return package_dir


@pytest.mark.slow
def test_editable_install_fails_only_the_site_packages_check(tmp_path: Path) -> None:
    source_package = REPO_ROOT / "src" / "claude_task_runner"
    # The known answer below holds only for an editable install of this tree.
    assert Path(claude_task_runner.__file__).resolve().parent == source_package
    site = (Path(sysconfig.get_path("purelib")) / "claude_task_runner").resolve()

    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    lines = proc.stdout.splitlines()
    report = [line for line in lines if line.startswith(("PASS  ", "FAIL  ", "      "))]
    assert report == [
        "FAIL  claude_task_runner imports from this interpreter's site-packages",
        f"      imported from {source_package}, not {site}",
        "PASS  claude-task-runner --help exits 0",
        "PASS  load_settings(None) validates the shipped defaults",
        "PASS  every skill in SKILL_NAMES resolves to its directory, which has a SKILL.md",
        "PASS  watchdog.sh and verify_branch_contributions.sh are executable",
        "PASS  install-skills --yes links every skill into an empty HOME",
    ], proc.stdout + proc.stderr
    assert lines[-1] == "1 of 6 checks failed."
    assert proc.returncode == 1


class TestRunChecks:
    def test_reports_each_verdict_and_counts_failures(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def raises() -> list[str]:
            raise ValueError("kaboom")

        failed = smoke.run_checks(
            [("passes", lambda: []), ("finds two", lambda: ["one", "two"]), ("raises", raises)]
        )

        out = capsys.readouterr().out.splitlines()
        assert failed == 2
        assert out[:5] == [
            "PASS  passes",
            "FAIL  finds two",
            "      one",
            "      two",
            "FAIL  raises",
        ]
        assert out[5] == "      Traceback (most recent call last):"
        assert out[-1] == "      ValueError: kaboom"


class TestCheckSitePackages:
    def test_the_installed_copy_passes(self, package_dir: Path) -> None:
        assert smoke.check_site_packages(package_dir, package_dir.parent) == []

    def test_a_source_tree_fails(self, package_dir: Path, tmp_path: Path) -> None:
        source = tmp_path.resolve() / "src" / "claude_task_runner"
        assert smoke.check_site_packages(source, package_dir.parent) == [
            f"imported from {source}, not {package_dir}"
        ]


class TestCheckHelp:
    def test_a_usage_line_passes(self, tmp_path: Path) -> None:
        cli = _write_executable(
            tmp_path / "cli",
            '#!/bin/sh\n[ "$*" = "--help" ] || exit 9\n'
            'echo "Usage: claude-task-runner [OPTIONS] COMMAND [ARGS]..."\n',
        )
        assert smoke.check_help(cli) == []

    def test_a_missing_console_script_fails(self, tmp_path: Path) -> None:
        assert smoke.check_help(tmp_path / "cli") == [f"no console script at {tmp_path / 'cli'}"]

    def test_a_non_zero_exit_fails(self, tmp_path: Path) -> None:
        cli = _write_executable(tmp_path / "cli", "#!/bin/sh\necho boom >&2\nexit 3\n")
        assert smoke.check_help(cli) == ["exited 3", "boom"]

    def test_output_without_the_usage_line_fails(self, tmp_path: Path) -> None:
        cli = _write_executable(tmp_path / "cli", "#!/bin/sh\necho hello\n")
        assert smoke.check_help(cli) == ["printed no usage line", "hello"]


def test_check_defaults_raises_what_load_settings_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def load_settings(per_queue_toml: Path | None) -> None:
        raise ConfigError("the defaults are missing")

    monkeypatch.setattr(smoke, "load_settings", load_settings)
    with pytest.raises(ConfigError, match="the defaults are missing"):
        smoke.check_defaults()


class TestCheckSkillDirs:
    def test_every_skill_in_the_package_passes(self, installed: Path) -> None:
        assert smoke.check_skill_dirs(installed) == []

    def test_a_missing_skill_fails(self, installed: Path) -> None:
        shutil.rmtree(installed / "skills" / "runner-status")
        assert smoke.check_skill_dirs(installed) == [
            "packaged skill 'runner-status' not found at expected path "
            f"{installed / 'skills' / 'runner-status'}"
        ]

    def test_a_skill_without_skill_md_fails(self, installed: Path) -> None:
        (installed / "skills" / "runner-status" / "SKILL.md").unlink()
        assert smoke.check_skill_dirs(installed) == [
            f"runner-status: no SKILL.md in {installed / 'skills' / 'runner-status'}"
        ]

    def test_a_skill_outside_the_package_fails(
        self, installed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        elsewhere = tmp_path.resolve() / "src" / "skills"
        monkeypatch.setattr(smoke, "_packaged_skill_dir", lambda name: elsewhere / name)
        assert smoke.check_skill_dirs(installed) == [
            f"{name}: resolved to {elsewhere / name}, not {installed / 'skills' / name}"
            for name in SKILL_NAMES
        ]


class TestCheckExecutable:
    def test_an_executable_file_passes(self, tmp_path: Path) -> None:
        assert smoke.check_executable(_write_executable(tmp_path / "run.sh", "#!/bin/sh\n")) == []

    def test_a_file_without_its_executable_bit_fails(self, tmp_path: Path) -> None:
        script = tmp_path / "run.sh"
        script.write_text("#!/bin/sh\n")
        script.chmod(0o644)
        assert smoke.check_executable(script) == [f"{script} is not executable (-rw-r--r--)"]

    def test_a_missing_file_fails(self, tmp_path: Path) -> None:
        assert smoke.check_executable(tmp_path / "run.sh") == [f"{tmp_path / 'run.sh'} is missing"]


class TestCheckDirectlyRunScripts:
    def test_both_executable_in_the_package_passes(self, installed: Path) -> None:
        assert smoke.check_directly_run_scripts(installed) == []

    def test_each_defect_is_named(self, installed: Path) -> None:
        (installed / "cron" / "watchdog.sh").chmod(0o644)
        (installed / VERIFIER).unlink()
        assert smoke.check_directly_run_scripts(installed) == [
            f"{installed / 'cron' / 'watchdog.sh'} is not executable (-rw-r--r--)",
            f"{installed / VERIFIER} is missing",
        ]

    def test_a_watchdog_outside_the_package_fails(
        self, installed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        elsewhere = _write_executable(tmp_path / "src" / "watchdog.sh", "#!/bin/sh\n")
        monkeypatch.setattr(smoke, "_watchdog_script_path", lambda: elsewhere)
        assert smoke.check_directly_run_scripts(installed) == [
            f"_watchdog_script_path() is {elsewhere}, not in {installed}"
        ]


class TestCheckSkillLinks:
    @pytest.fixture
    def targets(self, tmp_path: Path) -> dict[str, Path]:
        targets = {}
        for name in ("alpha", "beta"):
            target = tmp_path.resolve() / "site-packages" / "skills" / name
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text(f"# {name}\n")
            targets[name] = target
        return targets

    @pytest.fixture
    def skills_dir(self, tmp_path: Path, targets: dict[str, Path]) -> Path:
        skills_dir = tmp_path.resolve() / "home" / ".claude" / "skills"
        skills_dir.mkdir(parents=True)
        for name, target in targets.items():
            (skills_dir / name).symlink_to(target)
        return skills_dir

    def test_a_link_per_skill_passes(self, skills_dir: Path, targets: dict[str, Path]) -> None:
        assert smoke.check_skill_links(skills_dir, targets) == []

    def test_no_skills_directory_fails(self, tmp_path: Path, targets: dict[str, Path]) -> None:
        absent = tmp_path / "absent"
        assert smoke.check_skill_links(absent, targets) == [f"{absent} is not a directory"]

    def test_a_missing_link_fails(self, skills_dir: Path, targets: dict[str, Path]) -> None:
        (skills_dir / "beta").unlink()
        assert smoke.check_skill_links(skills_dir, targets) == [
            f"{skills_dir} holds ['alpha'], not ['alpha', 'beta']",
            f"beta: {skills_dir / 'beta'} is missing",
        ]

    def test_an_extra_entry_fails(self, skills_dir: Path, targets: dict[str, Path]) -> None:
        (skills_dir / "gamma").mkdir()
        assert smoke.check_skill_links(skills_dir, targets) == [
            f"{skills_dir} holds ['alpha', 'beta', 'gamma'], not ['alpha', 'beta']"
        ]

    def test_a_copy_instead_of_a_link_fails(
        self, skills_dir: Path, targets: dict[str, Path]
    ) -> None:
        (skills_dir / "beta").unlink()
        shutil.copytree(targets["beta"], skills_dir / "beta")
        assert smoke.check_skill_links(skills_dir, targets) == [
            f"beta: {skills_dir / 'beta'} is not a symlink"
        ]

    def test_a_dangling_link_fails(self, skills_dir: Path, targets: dict[str, Path]) -> None:
        shutil.rmtree(targets["beta"])
        assert smoke.check_skill_links(skills_dir, targets) == [
            f"beta: {skills_dir / 'beta'} -> {targets['beta']} does not resolve"
        ]

    def test_a_link_to_another_directory_fails(
        self, skills_dir: Path, targets: dict[str, Path], tmp_path: Path
    ) -> None:
        elsewhere = tmp_path.resolve() / "src" / "beta"
        elsewhere.mkdir(parents=True)
        (elsewhere / "SKILL.md").write_text("# beta\n")
        (skills_dir / "beta").unlink()
        (skills_dir / "beta").symlink_to(elsewhere)
        assert smoke.check_skill_links(skills_dir, targets) == [
            f"beta: {skills_dir / 'beta'} resolves to {elsewhere}, not {targets['beta']}"
        ]

    def test_a_target_without_skill_md_fails(
        self, skills_dir: Path, targets: dict[str, Path]
    ) -> None:
        (targets["beta"] / "SKILL.md").unlink()
        assert smoke.check_skill_links(skills_dir, targets) == [
            f"beta: no SKILL.md in {targets['beta']}"
        ]


class TestCheckInstallSkills:
    """``check_install_skills`` against stand-in console scripts."""

    def test_linking_every_skill_under_the_given_home_passes(
        self, package_dir: Path, tmp_path: Path
    ) -> None:
        cli = _write_executable(
            tmp_path / "cli",
            '#!/bin/sh\n[ "$*" = "install-skills --yes" ] || exit 9\n'
            'mkdir -p "$HOME/.claude/skills"\n'
            f"for name in {' '.join(SKILL_NAMES)}; do\n"
            f'  ln -s "{package_dir}/skills/$name" "$HOME/.claude/skills/$name"\n'
            "done\n",
        )
        assert smoke.check_install_skills(cli, package_dir) == []

    def test_a_non_zero_exit_fails(self, package_dir: Path, tmp_path: Path) -> None:
        cli = _write_executable(
            tmp_path / "cli", "#!/bin/sh\necho 'missing skill'\necho boom >&2\nexit 2\n"
        )
        assert smoke.check_install_skills(cli, package_dir) == ["exited 2", "missing skill", "boom"]

    def test_exiting_zero_without_linking_fails(self, package_dir: Path, tmp_path: Path) -> None:
        cli = _write_executable(tmp_path / "cli", "#!/bin/sh\nexit 0\n")
        problems = smoke.check_install_skills(cli, package_dir)
        assert len(problems) == 1
        assert re.fullmatch(r".+/smoke-home-[^/]+/\.claude/skills is not a directory", problems[0])
