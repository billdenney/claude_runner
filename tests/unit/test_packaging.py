"""Packaging gate: build the sdist and the wheel, then check what they ship.

CI installs the package editable, which serves ``src/`` through a ``.pth``
file and never builds the real wheel. So nothing noticed that
``[tool.hatch.build.targets.wheel.force-include]`` re-added two directories
that ``packages`` already ships. hatchling 1.24 to 1.29 wrote their 19 files
into the wheel twice, with only a zipfile warning. From 1.30 on hatchling
refuses, so ``uv build --wheel``, ``pip install .`` and a non-editable ``pipx
install`` all failed.

These tests build with the backend ``[build-system]`` declares, the way
``pip install .`` does (a wheel straight from the tree) and the way ``uv
build`` does (an sdist, then a wheel from the unpacked sdist). They pin what
a non-editable install needs at runtime.

The links in ``[project.urls]``, which every wheel's METADATA carries, are
checked against the GitHub repository ``origin`` points to.
"""

from __future__ import annotations

import collections
import configparser
import re
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from claude_task_runner.cli.install_skills_cmd import SKILL_NAMES

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
PACKAGE = "claude_task_runner"
SDIST_ROOT = f"{PACKAGE}-{PYPROJECT['project']['version']}"
DIST_INFO = f"{SDIST_ROOT}.dist-info"

# Calls one PEP 517 hook the way a frontend does: in a child process whose
# cwd is the source tree, clear of pytest's cwd and warning filters.
_RUN_HOOK = """
import importlib, sys
backend = importlib.import_module(sys.argv[1])
print(getattr(backend, sys.argv[2])(sys.argv[3]))
"""

# ``owner/repo`` in a GitHub page or clone URL: https://github.com/o/r/issues,
# git@github.com:o/r.git, ssh://git@github.com/o/r.
_GITHUB_REPO = re.compile(r"[/@]github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?(?:[/?#]|$)")


def _build(hook: str, source_dir: Path, out_dir: Path) -> Path:
    """Run the backend's ``hook`` on ``source_dir``; return the artifact it wrote."""
    backend = PYPROJECT["build-system"]["build-backend"]
    proc = subprocess.run(
        [sys.executable, "-c", _RUN_HOOK, backend, hook, str(out_dir)],
        cwd=source_dir,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if proc.returncode != 0:
        pytest.fail(f"{backend}.{hook} failed in {source_dir}:\n{proc.stderr}", pytrace=False)
    return out_dir / proc.stdout.splitlines()[-1]


def _duplicates(names: list[str]) -> list[str]:
    """Archive paths that appear more than once, sorted."""
    return sorted(name for name, count in collections.Counter(names).items() if count > 1)


def _github_repo(url: str) -> str | None:
    """The lowercased ``owner/repo`` a GitHub URL names, or None if it names none."""
    match = _GITHUB_REPO.search(url)
    return match.group(1).lower() if match else None


@pytest.fixture(scope="module")
def tracked() -> dict[str, bool]:
    """Every path git tracks, mapped to whether git records it as executable."""
    proc = subprocess.run(
        ["git", "ls-files", "--stage", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"git ls-files failed; this gate needs a git checkout:\n{proc.stderr}", pytrace=False
        )
    files = {}
    for record in filter(None, proc.stdout.split("\0")):
        meta, path = record.split("\t", 1)
        files[path] = meta.split()[0] == "100755"
    # An empty listing would pass every "ships each tracked file" check.
    assert f"src/{PACKAGE}/__init__.py" in files
    return files


@pytest.fixture(scope="module")
def tracked_package(tracked: dict[str, bool]) -> dict[str, bool]:
    """The tracked files under ``src/claude_task_runner/``, keyed by their wheel path."""
    return {
        path.removeprefix("src/"): executable
        for path, executable in tracked.items()
        if path.startswith(f"src/{PACKAGE}/")
    }


@pytest.fixture(scope="module")
def sdist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _build("build_sdist", REPO_ROOT, tmp_path_factory.mktemp("sdist"))


@pytest.fixture(scope="module")
def wheel_from_tree(tmp_path_factory: pytest.TempPathFactory) -> Iterator[zipfile.ZipFile]:
    path = _build("build_wheel", REPO_ROOT, tmp_path_factory.mktemp("wheel_from_tree"))
    with zipfile.ZipFile(path) as zf:
        yield zf


@pytest.fixture(scope="module")
def wheel_from_sdist(
    sdist: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[zipfile.ZipFile]:
    unpacked = tmp_path_factory.mktemp("unpacked_sdist")
    with tarfile.open(sdist) as tf:
        tf.extractall(unpacked, filter="data")
    out_dir = tmp_path_factory.mktemp("wheel_from_sdist")
    with zipfile.ZipFile(_build("build_wheel", unpacked / SDIST_ROOT, out_dir)) as zf:
        yield zf


@pytest.fixture(params=["wheel_from_tree", "wheel_from_sdist"])
def wheel(request: pytest.FixtureRequest) -> zipfile.ZipFile:
    """Each wheel in turn: ``pip install .`` builds the first, ``uv build`` the second."""
    built: zipfile.ZipFile = request.getfixturevalue(request.param)
    return built


def test_dev_extra_installs_the_build_backend() -> None:
    """The gate builds with the dev venv's backend, so it must be the declared one."""
    dev = PYPROJECT["project"]["optional-dependencies"]["dev"]
    assert [req for req in PYPROJECT["build-system"]["requires"] if req not in dev] == []


def test_duplicates_known_answers() -> None:
    # hatchling 1.30+ raises before it writes a second copy, so a build with
    # today's backend never reaches the duplicate checks below. These pin the
    # checker itself; hatchling 1.24-1.29 did write duplicates.
    assert _duplicates(["a", "b", "a", "c", "b", "a"]) == ["a", "b"]
    assert _duplicates(["a", "b", "c"]) == []
    assert _duplicates([]) == []


def test_github_repo_known_answers() -> None:
    for url in (
        "https://github.com/billdenney/claude_runner",
        "https://github.com/billdenney/claude_runner/issues",
        "https://github.com/BillDenney/Claude_Runner.git",
        "git@github.com:billdenney/claude_runner.git",
        "ssh://git@github.com/billdenney/claude_runner.git",
    ):
        assert _github_repo(url) == "billdenney/claude_runner", url
    assert _github_repo("https://github.com/billdenney/claude_task_runner") == (
        "billdenney/claude_task_runner"
    )
    assert _github_repo("https://github.com/billdenney") is None
    assert _github_repo("https://notgithub.com/billdenney/claude_runner") is None
    assert _github_repo("/home/bill/github/claude_task_runner") is None


def test_project_urls_name_the_origin_repository() -> None:
    """``[project.urls]`` must name the GitHub repository ``origin`` points to.

    Every wheel's METADATA carries these links. They named
    ``billdenney/claude_task_runner``, the package's name, which returns 404:
    the repository is ``billdenney/claude_runner``. A clone of a fork fails
    this test, since its ``origin`` names the fork.
    """
    proc = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 2:  # git-remote(1): no remote by that name
        pytest.skip("this checkout has no git remote named origin")
    assert proc.returncode == 0, proc.stderr
    origin_url = proc.stdout.strip()
    if "github.com" not in origin_url:
        pytest.skip(f"origin is not on GitHub: {origin_url}")
    origin = _github_repo(origin_url)
    assert origin is not None, origin_url
    links = {
        name: _github_repo(url)
        for name, url in PYPROJECT["project"]["urls"].items()
        if "github.com" in url
    }
    # With no GitHub link to compare, the check below would pass vacuously.
    assert links
    assert links == dict.fromkeys(links, origin)


@pytest.mark.slow
class TestWheel:
    def test_no_duplicate_archive_paths(self, wheel: zipfile.ZipFile) -> None:
        assert _duplicates(wheel.namelist()) == []

    def test_ships_every_tracked_package_file(
        self, wheel: zipfile.ZipFile, tracked_package: dict[str, bool]
    ) -> None:
        # Tracked files only: a build also ships untracked files that
        # .gitignore does not exclude, such as a new module not yet added.
        assert sorted(set(tracked_package) - set(wheel.namelist())) == []

    def test_ships_only_the_package_and_its_metadata(self, wheel: zipfile.ZipFile) -> None:
        assert {name.split("/", 1)[0] for name in wheel.namelist()} == {PACKAGE, DIST_INFO}

    def test_ships_the_files_a_non_editable_install_reads(self, wheel: zipfile.ZipFile) -> None:
        needed = [
            f"{PACKAGE}/py.typed",
            f"{PACKAGE}/config/defaults/settings.toml",  # config.loader.load_defaults
            f"{PACKAGE}/cron/watchdog.sh",  # the crontab line a cron install adds
            *(f"{PACKAGE}/skills/{name}/SKILL.md" for name in SKILL_NAMES),  # install-skills
        ]
        assert [name for name in needed if name not in wheel.namelist()] == []

    def test_keeps_executable_bits(
        self, wheel: zipfile.ZipFile, tracked_package: dict[str, bool]
    ) -> None:
        shipped = set(wheel.namelist())
        executable = {
            name: bool(wheel.getinfo(name).external_attr >> 16 & 0o111)
            for name in tracked_package
            if name in shipped
        }
        assert executable == {name: tracked_package[name] for name in executable}
        # Cron runs watchdog.sh itself and merge_branches.sh runs
        # verify_branch_contributions.sh itself, so pin both outright too.
        assert executable[f"{PACKAGE}/cron/watchdog.sh"] is True
        merge_skill = f"{PACKAGE}/skills/runner-merge-claude-branches"
        assert executable[f"{merge_skill}/verify_branch_contributions.sh"] is True

    def test_declares_the_console_script(self, wheel: zipfile.ZipFile) -> None:
        parser = configparser.ConfigParser(delimiters=("=",), interpolation=None)
        parser.optionxform = str
        parser.read_string(wheel.read(f"{DIST_INFO}/entry_points.txt").decode())
        assert {section: dict(parser[section]) for section in parser.sections()} == {
            "console_scripts": {"claude-task-runner": "claude_task_runner.cli:main"}
        }


@pytest.mark.slow
def test_wheel_from_sdist_matches_wheel_from_tree(
    wheel_from_tree: zipfile.ZipFile, wheel_from_sdist: zipfile.ZipFile
) -> None:
    assert sorted(wheel_from_sdist.namelist()) == sorted(wheel_from_tree.namelist())


@pytest.mark.slow
def test_sdist_ships_every_tracked_file_once(sdist: Path, tracked: dict[str, bool]) -> None:
    with tarfile.open(sdist) as tf:
        names = [m.name.removeprefix(f"{SDIST_ROOT}/") for m in tf.getmembers() if m.isfile()]
    assert _duplicates(names) == []
    assert sorted(set(tracked) - set(names)) == []
    assert "PKG-INFO" in names
